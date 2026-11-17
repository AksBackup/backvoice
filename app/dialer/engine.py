"""The dialer: batch lifecycle, per-batch work queue (Redis ZSET), dialing with
every safety check, and call finalisation.

Everything takes `fs` (Firestore abstraction) and `r` (Redis) explicitly so it
is fully testable with in-memory fakes. Celery tasks (dialer/tasks.py) are thin
wrappers around these functions.

Data it owns (additive — nothing existing is rewritten):
  lead_batches/{id}.voice   status, productId, scheduledAt, stats, pausedReason ...
  leads/{id}.voice          state, attempts, nextCallAt, lastOutcome, dnc ...
  voice_calls/{callId}      the call record (transcript, summary, minutes ...)
  voice_active_batches/*    tiny registry so the tick never scans every tenant
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from app import analysis, phone as phonelib, prompts, purposes, rds, tokens, wallet
from app.config import get_settings
from app.crypto import decrypt_json, encrypt_json
from app.dialer import leads as leadlib
from app.dispositions import RETRYABLE, apply_outcome, plan_outcome
from app.fs import (DELETE_FIELD, INCREMENT, P_ACTIVE_BATCHES, P_active_batch, P_batch, P_call, P_info, P_lead,
                    P_leads, P_product, P_secrets, P_settings, P_user, SERVER_TS)
from app.telephony.base import HangupInfo
from app.telephony.telnyx import get_carrier
from app.voice import greeting as greeting_mod
from app.windows import in_window, next_window_start

log = logging.getLogger(__name__)
TERMINAL_STATES = {"done", "dnc", "exhausted"}
LIVE_STATUSES = ("dialing", "ringing", "in_progress", "wrapping_up")
DIAL_PER_TICK = 5


class DialerError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _as_dt(v) -> Optional[datetime]:
    if v is None: return None
    if isinstance(v, datetime): return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if hasattr(v, "timestamp"):  # firestore DatetimeWithNanoseconds is a datetime already; be lenient
        return datetime.fromtimestamp(v.timestamp(), tz=timezone.utc)
    try: return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError: return None


# ───────────────────────────── configuration ─────────────────────────────
@dataclass
class VoiceConfig:
    settings: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)
    company_name: str = ""
    creds: dict = field(default_factory=dict)       # {provider: {...decrypted}}


async def load_secrets(fs, owner: str) -> dict:
    d = await fs.get(P_secrets(owner)) or {}
    out = {}
    for prov in ("vobiz", "telnyx"):
        if d.get(prov):
            try: out[prov] = decrypt_json(d[prov])
            except ValueError: log.error("cannot decrypt %s creds for %s", prov, owner)
    return out


async def load_config(fs, owner: str) -> VoiceConfig:
    user = await fs.get(P_user(owner)) or {}
    company = (user.get("companyInfo") or {}).get("name") or user.get("organizationName") or ""
    return VoiceConfig(settings=await fs.get(P_settings(owner)) or {}, info=await fs.get(P_info(owner)) or {},
                       company_name=company, creds=await load_secrets(fs, owner))


# ───────────────────────────── eligibility ───────────────────────────────
def lead_block_reason(lead: Optional[dict], allow_finished: bool = False, purpose: Optional[str] = None) -> Optional[str]:
    if not lead: return "missing"
    v = lead.get("voice") or {}
    if v.get("dnc"): return "dnc"
    if v.get("invalidNumber"): return "invalid_number"
    if v.get("state") in TERMINAL_STATES and not allow_finished: return "finished"
    if lead.get("stage") in ("won", "lost") and not purposes.closed_ok(purpose, lead.get("stage")): return "closed"
    if not phonelib.to_e164(lead.get("phoneNorm") or lead.get("phone") or ""): return "bad_number"
    return None


def _phone_norm(lead: dict) -> str:
    return phonelib.digits(lead.get("phoneNorm") or lead.get("phone") or "")


# ───────────────────────────── queue ─────────────────────────────────────
async def populate_queue(fs, r, owner: str, batch_id: str) -> int:
    leads = await fs.query(P_leads(owner), [("batchId", "==", batch_id)], limit=5000)
    bp = ((await fs.get(P_batch(owner, batch_id))) or {}).get("voice", {}).get("purpose")
    n = 0
    for lid, lead in leads:
        if lead_block_reason(lead, purpose=bp):
            continue
        v = lead.get("voice") or {}
        nxt = _as_dt(v.get("nextCallAt")) if v.get("state") in ("retry_scheduled", "callback_scheduled") else None
        await r.zadd(rds.k_queue(owner, batch_id), {lid: _ts(nxt) if nxt else 0}, nx=True)
        n += 1
    return n


async def _pop_ready(r, key: str, now: datetime) -> Optional[str]:
    while True:
        ids = await r.zrangebyscore(key, "-inf", _ts(now), start=0, num=1)
        if not ids:
            return None
        if await r.zrem(key, ids[0]) == 1:   # only one worker wins the claim
            return ids[0]


# ───────────────────────────── batch lifecycle ───────────────────────────
def _can_manage_batch(batch: dict, uid: str, is_admin: bool) -> bool:
    return is_admin or uid in (batch.get("assignedTo"), batch.get("createdBy"))


async def schedule_batch(fs, r, *, owner: str, batch_id: str, uid: str, is_admin: bool, product_id: str,
                         scheduled_at: Optional[datetime] = None, now: Optional[datetime] = None,
                         purpose: Optional[str] = None, provider: Optional[str] = None) -> dict:
    now = now or now_utc()
    provider = (provider or "auto").strip().lower()
    if provider != "auto" and provider not in phonelib.PROVIDERS:
        raise DialerError("bad_provider", "Unknown phone provider.")
    batch = await fs.get(P_batch(owner, batch_id))
    if not batch: raise DialerError("batch_not_found", "Batch not found.", 404)
    if not _can_manage_batch(batch, uid, is_admin): raise DialerError("forbidden", "You can't run this batch.", 403)
    if batch.get("mode") != "ai": raise DialerError("not_ai_batch", "Switch this batch to AI calling first.")
    cur = (batch.get("voice") or {}).get("status")
    if cur == "running": raise DialerError("already_running", "This batch is already running.", 409)
    product = await fs.get(P_product(owner, product_id)) if product_id else None
    if not product or product.get("active") is False:
        raise DialerError("product_missing", "Choose an active product for this campaign.")
    if purpose and not purposes.allowed_for(purpose, purposes.OUT):
        raise DialerError("bad_purpose", "That call type is for incoming calls only.")
    creds = await load_secrets(fs, owner)
    if not creds:
        raise DialerError("telephony_missing", "Add your Vobiz or Telnyx details in Voice settings first.")
    if provider != "auto" and provider not in creds:
        raise DialerError("telephony_missing", f"{provider.title()} is not set up. Add its details in Voice settings or pick another provider.")
    ok, why = await wallet.gate(fs, owner, await rds.inflight(r, owner))
    if not ok:
        raise DialerError(why, "You have no call minutes left. Buy minutes to start." if why != "low_balance"
                          else "Your call minutes are almost finished. Top up to start.", 402)
    leads = await fs.query(P_leads(owner), [("batchId", "==", batch_id)], limit=5000)
    ready = [1 for _, l in leads if not lead_block_reason(l, purpose=purpose or (batch.get("voice") or {}).get("purpose"))]
    if not ready:
        raise DialerError("no_leads", "This batch has no callable leads.")

    start_now = scheduled_at is None or scheduled_at <= now
    status = "running" if start_now else "scheduled"
    await fs.update(P_batch(owner, batch_id), {
        "voice.status": status, "voice.productId": product_id, "voice.productName": product.get("name", ""),
        **({"voice.purpose": purpose} if purpose else {}),
        "voice.provider": provider,
        "voice.scheduledAt": scheduled_at or now, "voice.pausedReason": DELETE_FIELD,
        "voice.finishedAt": DELETE_FIELD, "voice.updatedAt": now,
        **({"voice.startedAt": now} if start_now else {}),
        "voice.stats.total": len(ready),
    })
    await fs.set(P_active_batch(owner, batch_id), {"owner": owner, "batchId": batch_id, "status": status,
                                                    "scheduledAt": scheduled_at or now})
    if start_now:
        await populate_queue(fs, r, owner, batch_id)
    return {"status": status, "callableLeads": len(ready)}


async def set_batch_status(fs, r, *, owner: str, batch_id: str, uid: str, is_admin: bool, action: str,
                           reason: str = "", now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    batch = await fs.get(P_batch(owner, batch_id))
    if not batch: raise DialerError("batch_not_found", "Batch not found.", 404)
    if not _can_manage_batch(batch, uid, is_admin): raise DialerError("forbidden", "You can't change this batch.", 403)
    cur = (batch.get("voice") or {}).get("status")
    if action == "pause":
        if cur not in ("running", "scheduled"): raise DialerError("bad_state", "Only a running batch can be paused.", 409)
        new = "paused"
    elif action == "resume":
        if cur != "paused": raise DialerError("bad_state", "Only a paused batch can be resumed.", 409)
        ok, why = await wallet.gate(fs, owner, await rds.inflight(r, owner))
        if not ok: raise DialerError(why, "Add call minutes before resuming.", 402)
        new = "running"
    elif action == "stop":
        if cur in (None, "completed", "stopped"): raise DialerError("bad_state", "This batch is not active.", 409)
        new = "stopped"
    else:
        raise DialerError("bad_action", "Unknown action.")
    upd = {"voice.status": new, "voice.updatedAt": now, "voice.pausedReason": reason or ("manual" if new == "paused" else DELETE_FIELD)}
    if new == "stopped": upd["voice.finishedAt"] = now
    await fs.update(P_batch(owner, batch_id), upd)
    if new == "stopped":
        await fs.delete(P_active_batch(owner, batch_id))
        await r.delete(rds.k_queue(owner, batch_id))
    else:
        await fs.set(P_active_batch(owner, batch_id), {"status": new}, merge=True)
        if new == "running":
            await populate_queue(fs, r, owner, batch_id)
    return {"status": new}


async def _complete_batch(fs, r, owner, batch_id, now):
    await fs.update(P_batch(owner, batch_id), {"voice.status": "completed", "voice.finishedAt": now, "voice.updatedAt": now})
    await fs.delete(P_active_batch(owner, batch_id))
    await r.delete(rds.k_queue(owner, batch_id))


async def _pause_for(fs, owner, batch_id, reason, now):
    await fs.update(P_batch(owner, batch_id), {"voice.status": "paused", "voice.pausedReason": reason, "voice.updatedAt": now})
    await fs.set(P_active_batch(owner, batch_id), {"status": "paused"}, merge=True)


# ───────────────────────────── tick ──────────────────────────────────────
async def tick(fs, r, now: Optional[datetime] = None, carrier_factory: Optional[Callable] = None) -> dict:
    now = now or now_utc()
    carrier_factory = carrier_factory or get_carrier
    regs = await fs.query(P_ACTIVE_BATCHES, [("status", "in", ["scheduled", "running"])], limit=500)
    out = {"batches": 0, "dialed": 0}
    for _, reg in regs:
        owner, batch_id = reg.get("owner"), reg.get("batchId")
        if not owner or not batch_id:
            continue
        try:
            if reg["status"] == "scheduled":
                at = _as_dt(reg.get("scheduledAt"))
                if at and at > now:
                    continue
                batch = await fs.get(P_batch(owner, batch_id))
                if not batch or (batch.get("voice") or {}).get("status") != "scheduled":
                    await fs.delete(P_active_batch(owner, batch_id)); continue
                await fs.update(P_batch(owner, batch_id), {"voice.status": "running", "voice.startedAt": now, "voice.updatedAt": now})
                await fs.set(P_active_batch(owner, batch_id), {"status": "running"}, merge=True)
                await populate_queue(fs, r, owner, batch_id)
            out["batches"] += 1
            out["dialed"] += await process_batch(fs, r, owner, batch_id, now, carrier_factory)
        except Exception:
            log.exception("tick failed for %s/%s", owner, batch_id)
    return out


async def process_batch(fs, r, owner: str, batch_id: str, now: datetime, carrier_factory: Optional[Callable] = None) -> int:
    cfg_s = get_settings()
    carrier_factory = carrier_factory or get_carrier
    batch = await fs.get(P_batch(owner, batch_id))
    v = (batch or {}).get("voice") or {}
    if not batch or batch.get("mode") != "ai" or v.get("status") != "running":
        if batch and batch.get("mode") != "ai" and v.get("status") == "running":
            await _pause_for(fs, owner, batch_id, "switched_to_manual", now)
        elif not batch:
            await fs.delete(P_active_batch(owner, batch_id))
        return 0
    cfg = await load_config(fs, owner)
    product = await fs.get(P_product(owner, v.get("productId", "")))
    if not product:
        await _pause_for(fs, owner, batch_id, "product_missing", now); return 0
    tenant_cap = min(int(cfg.settings.get("maxConcurrent") or cfg_s.MAX_CONCURRENT_PER_TENANT), cfg_s.MAX_CONCURRENT_PER_TENANT)
    key = rds.k_queue(owner, batch_id)
    dialed = 0
    while dialed < DIAL_PER_TICK:
        if await rds.inflight(r, owner) >= tenant_cap or await rds.inflight_global(r) >= cfg_s.MAX_CONCURRENT_GLOBAL:
            break
        lead_id = await _pop_ready(r, key, now)
        if not lead_id:
            break
        res = await dial_one(fs, r, owner=owner, batch_id=batch_id, lead_id=lead_id, product_id=v.get("productId"),
                             cfg=cfg, product=product, now=now, carrier_factory=carrier_factory)
        if res == "dialed": dialed += 1
        elif res == "paused": return dialed
    if await r.zcard(key) == 0 and await rds.inflight_batch(r, owner, batch_id) == 0:
        await _complete_batch(fs, r, owner, batch_id, now)
    return dialed


# ───────────────────────────── dialing ───────────────────────────────────
def _language_for(lead: dict, cfg: VoiceConfig, phone_norm: str) -> str:
    return (lead.get("voice") or {}).get("language") or phonelib.default_language(phone_norm, cfg.settings.get("language", "hi"))


async def dial_one(fs, r, *, owner: str, batch_id: Optional[str], lead_id: str, product_id: Optional[str],
                   cfg: Optional[VoiceConfig] = None, product: Optional[dict] = None, now: Optional[datetime] = None,
                   carrier_factory: Optional[Callable] = None, allow_finished: bool = False,
                   provider_pref: Optional[str] = None) -> str:
    """Returns: dialed | skipped | deferred | paused | failed
    provider_pref: explicit carrier for this call (one-off dial). Otherwise the batch's choice, then the account default."""
    now = now or now_utc()
    carrier_factory = carrier_factory or get_carrier
    cfg = cfg or await load_config(fs, owner)
    if not await rds.lock_lead(r, owner, lead_id):
        return "skipped"
    unlock = True
    try:
        lead = await fs.get(P_lead(owner, lead_id))
        batch = await fs.get(P_batch(owner, batch_id)) if batch_id else None
        if product is None and product_id:
            product = await fs.get(P_product(owner, product_id))
        purpose = purposes.resolve(batch_purpose=((batch or {}).get("voice") or {}).get("purpose"),
                                   product=product, settings=cfg.settings, direction=purposes.OUT)
        if lead_block_reason(lead, allow_finished, purpose=purpose):
            return "skipped"
        if batch_id:
            if (not batch or batch.get("mode") != "ai" or lead.get("batchId") != batch_id
                    or (batch.get("voice") or {}).get("status") != "running"):
                return "skipped"
        pn = _phone_norm(lead)
        window = cfg.settings.get("window")
        tz = phonelib.timezone_for(pn, (window or {}).get("tz", "Asia/Kolkata"))
        if not in_window(now, window, tz):
            if batch_id:
                await r.zadd(rds.k_queue(owner, batch_id), {lead_id: _ts(next_window_start(now, window, tz))})
            return "deferred"
        # first explicit choice wins: this call > this campaign > account default ("auto" = pick by country)
        pref = next((c for c in (provider_pref, ((batch or {}).get("voice") or {}).get("provider"), cfg.settings.get("preferredProvider"))
                     if c in phonelib.PROVIDERS), "auto")
        provider = phonelib.route_provider(pn, cfg.creds.keys(), pref)
        if not provider:
            if batch_id:
                await fs.update(P_lead(owner, lead_id), {"voice.state": "exhausted", "voice.lastOutcome": "failed",
                                                          "voice.note": ("The phone provider you chose is not set up" if pref in phonelib.PROVIDERS else "No carrier configured for this number's country")})
            return "skipped"
        ok, why = await wallet.gate(fs, owner, await rds.inflight(r, owner))
        if not ok:
            if batch_id:
                await r.zadd(rds.k_queue(owner, batch_id), {lead_id: 0})
                await _pause_for(fs, owner, batch_id, why, now)
            return "paused"

        attempt = int((lead.get("voice") or {}).get("attempts") or 0) + 1
        language = _language_for(lead, cfg, pn)
        call_id = uuid.uuid4().hex
        token = tokens.sign_call_id(call_id)
        pack = build_pack(cfg=cfg, lead=lead, product=product, language=language, attempt=attempt, purpose=purpose)
        creds = cfg.creds[provider]
        meta = {"call_id": call_id, "owner": owner, "batch_id": batch_id, "lead_id": lead_id, "product_id": product_id,
                "provider": provider, "language": language, "attempt": attempt, "to": phonelib.to_e164(pn),
                "pack": pack, "creds_enc": encrypt_json(creds), "provider_call_id": None, "dialed_at": now.isoformat(),
                "tz": tz, "purpose": purpose, "direction": "out"}
        await rds.save_call_meta(r, call_id, meta)
        await rds.mark_active(r, owner, batch_id, call_id)
        await fs.set(P_call(owner, call_id), {
            "callId": call_id, "leadId": lead_id, "leadName": lead.get("name", ""), "batchId": batch_id,
            "productId": product_id, "provider": provider, "to": meta["to"], "language": language, "attempt": attempt,
            "purpose": purpose, "direction": "out",
            "status": "dialing", "transcript": [], "createdAt": now,
            # copied from the lead so Firestore rules can show an employee only the calls on leads they can see
            "visibleTo": list(lead.get("visibleTo") or []), "ownerId": lead.get("ownerId", "")})
        await fs.update(P_lead(owner, lead_id), {"voice.state": "calling", "voice.attempts": attempt,
                                                  "voice.currentCallId": call_id, "voice.nextCallAt": DELETE_FIELD})
        unlock = False   # lock now held until finalisation
        carrier = carrier_factory(provider)
        pcid = await carrier.place_call(creds, meta["to"], call_id, token, get_settings().PUBLIC_BASE_URL)
        if not pcid:
            await finalize_call(fs, r, call_id, HangupInfo(cause="failed"), now=now)
            return "failed"
        await rds.patch_call_meta(r, call_id, provider_call_id=pcid)
        await fs.update(P_call(owner, call_id), {"providerCallId": pcid})
        # Pre-render the greeting WHILE IT RINGS (Hindi/Sarvam only; falls back silently).
        if language != "en" and pack.get("greeting"):
            audio = await greeting_mod.prerender_sarvam(pack["greeting"], voice=pack.get("ttsVoice") or
                                                        ("priya" if pack.get("voiceGender") != "male" else "rahul"))
            if audio:
                m = await rds.load_call_meta(r, call_id)
                if m:
                    m["pack"]["greetingAudio"] = audio
                    await rds.save_call_meta(r, call_id, m)
        return "dialed"
    except Exception:
        log.exception("dial_one crashed for lead %s", lead_id)
        return "failed"
    finally:
        if unlock:
            await rds.unlock_lead(r, owner, lead_id)


def build_pack(*, cfg: VoiceConfig, lead: dict, product: Optional[dict], language: str, attempt: int,
               purpose: Optional[str] = None, direction: str = purposes.OUT) -> dict:
    s = cfg.settings
    return {
        "language": language, "voiceGender": s.get("voiceGender", "female"), "ttsVoice": s.get("ttsVoice", ""),
        "sttProvider": s.get("sttProvider", "deepgram"), "englishTts": s.get("englishTts", "deepgram"),
        "purpose": purposes.get(purpose, direction).key, "direction": direction,
        "greeting": prompts.build_greeting(settings=s, lead=lead, company_name=cfg.company_name, language=language,
                                           product=product, purpose=purpose, direction=direction),
        "system_prompt": prompts.build_system_prompt(settings=s, info=cfg.info, company_name=cfg.company_name,
                                                     product=product, lead=lead, language=language, attempt=attempt,
                                                     purpose=purpose, direction=direction),
        "faqs": (cfg.info or {}).get("faq") or [],
    }


# ───────────────────────────── finalisation ──────────────────────────────
def _status_for(disposition: str, answered: bool) -> str:
    if answered: return "completed"
    return {"busy": "busy", "no_answer": "no_answer"}.get(disposition, "failed")


async def finalize_call(fs, r, call_id: str, hangup: Optional[HangupInfo] = None, now: Optional[datetime] = None,
                        analyze=analysis.analyze_call) -> dict:
    """Settles a call exactly once: minutes, disposition, lead update, retry,
    batch stats, locks. Safe to call from the hangup webhook, the reaper and
    the dialer's own failure path — whoever gets there first wins."""
    now = now or now_utc()
    if not await r.set(rds.k_final(call_id), "1", nx=True, ex=3600):
        return {"result": "already_finalised"}
    meta = await rds.load_call_meta(r, call_id)
    if not meta:
        log.error("finalize_call: no meta for %s", call_id)
        return {"result": "no_meta"}
    owner, batch_id = meta["owner"], meta.get("batch_id")
    lead_id = meta.get("lead_id") or ""                       # "" = an inbound caller who is not a lead (yet)
    lock_key = meta.get("lock_key") or lead_id
    settled = False
    try:
        call = await fs.get(P_call(owner, call_id)) or {}
        lead = (await fs.get(P_lead(owner, lead_id)) or {}) if lead_id else {}
        settings = await fs.get(P_settings(owner)) or {}
        hangup = hangup or HangupInfo()
        answered = bool(call.get("answeredAt"))
        connected = float(call.get("connectedSeconds") or 0)
        if answered and not connected and hangup.duration_seconds:
            connected = float(hangup.duration_seconds)
        if answered and connected < get_settings().MIN_BILLABLE_SECONDS:
            answered = False   # picked up and dropped instantly: treat as unanswered

        summary, temperature, callback_at, lang_spoken, appointment_at = "", None, None, None, None
        intent = caller_name = interest = None
        lead_created = False
        if answered:
            res = await analyze(transcript=call.get("transcript") or [], now=now, tz=meta.get("tz", "Asia/Kolkata"),
                                purpose=meta.get("purpose") or "")
            disposition, summary = res["disposition"], res["summary"]
            temperature, callback_at, lang_spoken = res["temperature"], res["callback_at"], res["language"]
            appointment_at = res.get("appointment_at")
            intent, caller_name, interest = res.get("intent"), res.get("caller_name"), res.get("interest")
        else:
            disposition = hangup.cause if hangup.cause in ("busy", "no_answer", "failed") else "no_answer"

        # ── inbound caller who is not a lead yet: only a real ENQUIRY becomes one ──────────────────────
        # (support calls, follow-ups on earlier dealings, existing customers, spam and wrong numbers do not)
        if meta.get("direction") == "in" and not lead_id and answered and intent == "enquiry" \
                and settings.get("inboundCreateLeads", True) and disposition not in ("wrong_number", "dnc"):
            caller_norm = phonelib.digits(meta.get("from") or "")
            if caller_norm and not await leadlib.is_existing_customer(fs, owner, caller_norm):
                lead_id = await leadlib.create_enquiry_lead(fs, owner, caller_norm, now, name=caller_name or "", summary=summary,
                                                            interest=interest or "", temperature=temperature) or ""
                if lead_id:
                    lead = await fs.get(P_lead(owner, lead_id)) or {}
                    lead_created = True

        minutes = await wallet.charge(fs, owner, connected) if answered else 0.0
        attempts = int(meta["attempt"]) if meta.get("attempt") is not None else 1
        inbound = meta.get("direction") == "in"
        plan = plan_outcome(disposition=disposition, lead=lead, voice_settings=settings, now=now, attempts=attempts,
                            summary=summary, temperature=temperature, callback_at=callback_at,
                            appointment_at=appointment_at)
        if inbound:
            # The caller rang US. Never schedule an outbound retry for a dropped inbound call, and don't
            # touch the lead's outbound campaign state (unless the caller asked for a callback).
            if plan.disposition in RETRYABLE:
                plan.retry_at, plan.state, plan.terminal = None, "done", True
            plan.lead_updates.pop("voice.state", None)
            plan.lead_updates.pop("voice.attempts", None)      # inbound is not an outbound attempt
            if not plan.retry_at:
                plan.lead_updates.pop("voice.nextCallAt", None)
        if lead_id:
            await apply_outcome(fs, owner, lead_id, plan, now)
            await fs.update(P_lead(owner, lead_id), {"voice.currentCallId": DELETE_FIELD,
                                                      **({"voice.language": lang_spoken} if lang_spoken in ("hi", "en") else {})})
        await fs.update(P_call(owner, call_id), {
            **({"intent": intent} if intent else {}),
            **({"leadId": lead_id, "leadName": lead.get("name", ""), "leadCreated": True} if lead_created else {}),
            "status": _status_for(plan.disposition, answered), "disposition": plan.disposition, "summary": summary,
            "temperature": temperature, "connectedSeconds": round(connected, 1), "billedMinutes": minutes,
            "endedAt": call.get("sessionEndedAt") or now, "finalizedAt": now, "endReason": call.get("endReason", ""),
            "languageSpoken": lang_spoken, "nextCallAt": plan.retry_at or DELETE_FIELD})
        if plan.retry_at and batch_id:
            await r.zadd(rds.k_queue(owner, batch_id), {lead_id: _ts(plan.retry_at)})
        if batch_id:
            stats = {"voice.stats.calls": INCREMENT(1), f"voice.stats.{plan.disposition}": INCREMENT(1),
                     "voice.stats.minutes": INCREMENT(minutes), "voice.updatedAt": now}
            if answered: stats["voice.stats.answered"] = INCREMENT(1)
            await fs.update(P_batch(owner, batch_id), stats)
        settled = True
        return {"result": "finalised", "disposition": plan.disposition, "minutes": minutes, "answered": answered}
    except Exception:
        # Something failed half way (Firestore hiccup, analysis crash...). Release the "done" flag so the
        # retry / reaper can settle it again, and keep the call registered as active so the reaper still sees it.
        # Without this the call stayed "On call" in the app forever.
        await r.delete(rds.k_final(call_id))
        log.exception("finalize_call failed for %s - will be retried", call_id)
        raise
    finally:
        if settled:
            await rds.mark_inactive(r, owner, batch_id, call_id)
            await rds.unlock_lead(r, owner, lock_key)


async def reap_stuck(fs, r, now: Optional[datetime] = None, carrier_factory: Optional[Callable] = None) -> int:
    """Calls that never got a hangup webhook (carrier hiccup, crashed worker)."""
    now = now or now_utc()
    carrier_factory = carrier_factory or get_carrier
    n = 0
    for call_id in await r.smembers(rds.k_active_all()):
        meta = await rds.load_call_meta(r, call_id)
        if not meta:
            await r.srem(rds.k_active_all(), call_id); continue
        age = (now - _as_dt(meta["dialed_at"])).total_seconds()
        if age < get_settings().CALL_STUCK_SECONDS:
            continue
        try:
            if meta.get("provider_call_id") and meta.get("creds_enc"):
                await carrier_factory(meta["provider"]).hangup(decrypt_json(meta["creds_enc"]), meta["provider_call_id"])
        except Exception:
            log.exception("reaper hangup failed for %s", call_id)
        try:
            await finalize_call(fs, r, call_id, HangupInfo(cause="failed"), now=now)
            n += 1
        except Exception:
            log.exception("reaper could not settle %s", call_id)
    return n


async def end_call_now(fs, r, *, owner: str, call_id: str, uid: str, is_admin: bool, now: Optional[datetime] = None,
                       carrier_factory: Optional[Callable] = None) -> dict:
    """Manual \"this call is over\" for a call the app still shows as live (lost webhook, crashed worker...).
    Hangs up at the carrier (best effort) and settles the call exactly like a normal hangup. If the call's working
    data is already gone, it just closes the call record and frees the lead."""
    now = now or now_utc()
    carrier_factory = carrier_factory or get_carrier
    call = await fs.get(P_call(owner, call_id))
    if not call: raise DialerError("call_not_found", "Call not found.", 404)
    if not (is_admin or call.get("ownerId") == uid or uid in (call.get("visibleTo") or [])):
        raise DialerError("forbidden", "You can't end this call.", 403)
    if call.get("status") not in LIVE_STATUSES:
        return {"result": "already_ended"}
    meta = await rds.load_call_meta(r, call_id)
    if meta and meta.get("provider_call_id") and meta.get("creds_enc"):
        try: await carrier_factory(meta["provider"]).hangup(decrypt_json(meta["creds_enc"]), meta["provider_call_id"])
        except Exception: log.warning("manual hangup failed for %s", call_id)
    if meta:
        await r.delete(rds.k_final(call_id))          # allow a re-settle if an earlier attempt died half way
        res = await finalize_call(fs, r, call_id, HangupInfo(cause="completed" if call.get("answeredAt") else "failed"), now=now)
        return {"result": res.get("result", "finalised")}
    # No working data left: close the record by hand.
    answered = bool(call.get("answeredAt"))
    await fs.update(P_call(owner, call_id), {"status": "completed" if answered else "failed", "endedAt": now, "finalizedAt": now,
                                              "endReason": "closed_manually"})
    lead_id = call.get("leadId")
    if lead_id:
        lead = await fs.get(P_lead(owner, lead_id)) or {}
        v = lead.get("voice") or {}
        if v.get("currentCallId") == call_id:
            upd = {"voice.currentCallId": DELETE_FIELD}
            if v.get("state") == "calling": upd["voice.state"] = "done" if answered else "retry_scheduled"
            if upd.get("voice.state") == "retry_scheduled": upd["voice.nextCallAt"] = now
            await fs.update(P_lead(owner, lead_id), upd)
    key = (meta or {}).get("lock_key") or lead_id
    if key:
        await rds.unlock_lead(r, owner, key)
    await rds.mark_inactive(r, owner, call.get("batchId"), call_id)
    return {"result": "closed"}


async def finalize_when_ready(fs, r, call_id: str, hangup: Optional[dict], tries: int = 0, now: Optional[datetime] = None) -> str:
    """Hangup webhooks can arrive a moment BEFORE the media websocket has
    written its transcript. Wait (up to ~16 s) for the session to be saved so
    the analysis sees the whole conversation; then settle regardless."""
    meta = await rds.load_call_meta(r, call_id)
    if not meta:
        return "no_meta"
    call = await fs.get(P_call(meta["owner"], call_id)) or {}
    if call.get("answeredAt") and not call.get("sessionEndedAt") and tries < 8:
        return "wait"
    res = await finalize_call(fs, r, call_id, HangupInfo(**hangup) if hangup else None, now=now)
    return res["result"]
