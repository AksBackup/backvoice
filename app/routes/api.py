"""The authenticated API the Flutter app calls (same Firebase login).

The app READS everything live from Firestore. It calls this API only for
things that must be validated server-side or involve secrets:
telephony credentials, scheduling/pausing campaigns, a one-off AI call, and a
text-only prompt test.
"""
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import purposes, rds, testchat, tokens, verify, wallet
from app.auth import Principal, get_principal, require_admin
from app.crypto import decrypt_json, encrypt_json, mask
from app.config import get_settings
from app.dialer import engine
from app.fs import DELETE_FIELD, P_lead, P_product, P_secrets, P_settings, SERVER_TS, get_fs
from app.telephony.telnyx import Telnyx
from app.telephony.vobiz import Vobiz

router = APIRouter(prefix="/v1")
CARRIERS = {"vobiz": Vobiz, "telnyx": Telnyx}
SECRET_FIELD = {"vobiz": "auth_token", "telnyx": "api_key"}


class CarrierBody(BaseModel):
    # Fields not used by the chosen carrier are simply ignored.
    auth_id: str = ""
    auth_token: str = ""
    api_key: str = ""
    connection_id: str = ""
    from_number: str = ""


class ScheduleBody(BaseModel):
    productId: str
    scheduledAt: Optional[datetime] = None
    purpose: Optional[str] = None          # one of GET /v1/purposes (outbound-capable)
    provider: Optional[str] = None         # auto | vobiz | telnyx


class DialBody(BaseModel):
    leadId: str
    productId: str
    provider: Optional[str] = None         # auto | vobiz | telnyx


class ChatBody(BaseModel):
    message: str = ""
    history: list[dict] = Field(default_factory=list)
    productId: Optional[str] = None
    language: Optional[str] = None
    purpose: Optional[str] = None
    direction: str = "out"                 # "in" previews the inbound greeting/behaviour



def _carrier(name: str):
    if name not in CARRIERS:
        raise HTTPException(404, "Unknown carrier.")
    return CARRIERS[name]


def _e164(num: str) -> str:
    d = re.sub(r"[^\d]", "", num or "")
    if not (8 <= len(d) <= 15):
        raise HTTPException(422, "Enter the carrier number with country code, e.g. +919876543210.")
    return "+" + d


async def _telephony_status(owner: str) -> dict:
    d = await get_fs().get(P_secrets(owner)) or {}
    meta = d.get("meta") or {}
    return {p: {"configured": bool(d.get(p)), "fromNumber": (meta.get(p) or {}).get("fromNumber", ""),
                "hint": (meta.get(p) or {}).get("hint", "")} for p in CARRIERS}


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/me")
async def me(p: Principal = Depends(get_principal)):
    fs, r = get_fs(), rds.get_redis()
    w = await wallet.get_wallet(fs, p.owner_id)
    ok, why = await wallet.gate(fs, p.owner_id, await rds.inflight(r, p.owner_id))
    tel = await _telephony_status(p.owner_id)
    return {"ownerId": p.owner_id, "role": p.role,
            "wallet": {"purchased": w.purchased, "used": w.used, "remaining": w.remaining},
            "canCall": ok, "blockedReason": None if ok else why,
            "telephony": {k: v["configured"] for k, v in tel.items()}}


# ── telephony credentials (admin only; stored encrypted, never returned) ─────
@router.get("/telephony")
async def get_telephony(p: Principal = Depends(get_principal)):
    return await _telephony_status(p.owner_id)


@router.put("/telephony/{provider}")
async def put_telephony(provider: str, body: CarrierBody, p: Principal = Depends(require_admin)):
    cls = _carrier(provider)
    fs = get_fs()
    existing = {}
    d = await fs.get(P_secrets(p.owner_id)) or {}
    if d.get(provider):
        existing = decrypt_json(d[provider])
    merged = {}
    for f in cls.required_fields:
        new = (getattr(body, f, "") or "").strip()
        merged[f] = new or existing.get(f, "")      # blank = keep what is stored
    missing = cls.validate_creds(merged)
    if missing:
        raise HTTPException(422, f"Missing: {', '.join(missing)}")
    merged["from_number"] = _e164(merged["from_number"])
    await fs.set(P_secrets(p.owner_id), {
        provider: encrypt_json(merged),
        "meta": {provider: {"fromNumber": merged["from_number"], "hint": mask(merged[SECRET_FIELD[provider]])}},
        "updatedAt": SERVER_TS}, merge=True)
    return (await _telephony_status(p.owner_id))[provider]


@router.delete("/telephony/{provider}")
async def delete_telephony(provider: str, p: Principal = Depends(require_admin)):
    _carrier(provider)
    await get_fs().update(P_secrets(p.owner_id), {provider: DELETE_FIELD, f"meta.{provider}": DELETE_FIELD}) \
        if await get_fs().get(P_secrets(p.owner_id)) else None
    return {"ok": True}


# ── campaigns ────────────────────────────────────────────────────────────────
@router.post("/batches/{batch_id}/schedule")
async def schedule(batch_id: str, body: ScheduleBody, p: Principal = Depends(get_principal)):
    at = body.scheduledAt
    if at and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return await engine.schedule_batch(get_fs(), rds.get_redis(), owner=p.owner_id, batch_id=batch_id, uid=p.uid,
                                       is_admin=p.is_admin, product_id=body.productId, scheduled_at=at,
                                       purpose=body.purpose, provider=body.provider)


@router.post("/batches/{batch_id}/{action}")
async def batch_action(batch_id: str, action: str, p: Principal = Depends(get_principal)):
    if action not in ("pause", "resume", "stop"):
        raise HTTPException(404, "Unknown action.")
    return await engine.set_batch_status(get_fs(), rds.get_redis(), owner=p.owner_id, batch_id=batch_id, uid=p.uid,
                                         is_admin=p.is_admin, action=action)


# ── one-off AI call to a single lead ─────────────────────────────────────────
@router.post("/calls/dial")
async def dial(body: DialBody, p: Principal = Depends(get_principal)):
    fs, r = get_fs(), rds.get_redis()
    lead = await fs.get(P_lead(p.owner_id, body.leadId))
    if not lead:
        raise HTTPException(404, "Lead not found.")
    if not (p.is_admin or lead.get("ownerId") == p.uid or p.uid in (lead.get("visibleTo") or [])):
        raise HTTPException(403, "You can't call this lead.")
    product = await fs.get(P_product(p.owner_id, body.productId))
    if not product or product.get("active") is False:
        raise HTTPException(400, "Choose an active product.")
    cfg = await engine.load_config(fs, p.owner_id)
    if not cfg.creds:
        raise HTTPException(400, "Add your Vobiz or Telnyx details in Voice settings first.")
    pref = (body.provider or "").strip().lower()
    if pref in ("", "auto"):
        pref = ""
    elif pref not in CARRIERS:
        raise HTTPException(422, "Unknown phone provider.")
    elif pref not in cfg.creds:
        raise HTTPException(400, f"{pref.title()} is not set up. Add its details in Voice settings or pick another provider.")
    res = await engine.dial_one(fs, r, owner=p.owner_id, batch_id=None, lead_id=body.leadId, product_id=body.productId,
                                cfg=cfg, product=product, allow_finished=True, provider_pref=pref or None)
    if res == "dialed":
        fresh = await fs.get(P_lead(p.owner_id, body.leadId))
        return {"result": "dialed", "callId": ((fresh or {}).get("voice") or {}).get("currentCallId")}
    msg = {"deferred": (409, "This number is outside permitted calling hours right now."),
           "skipped": (409, "This lead can't be called (do-not-call, invalid number, closed, or already on a call)."),
           "paused": (402, "You have no call minutes left. Buy minutes to start."),
           "failed": (502, "The call could not be placed. Check your carrier details.")}[res]
    raise HTTPException(msg[0], msg[1])


# ── end a call the app still shows as live ───────────────────────────────────
@router.post("/calls/{call_id}/end")
async def end_call(call_id: str, p: Principal = Depends(get_principal)):
    return await engine.end_call_now(get_fs(), rds.get_redis(), owner=p.owner_id, call_id=call_id, uid=p.uid, is_admin=p.is_admin)


# ── text-only prompt test ────────────────────────────────────────────────────
@router.post("/test-chat")
async def test_chat(body: ChatBody, p: Principal = Depends(get_principal)):
    fs, r = get_fs(), rds.get_redis()
    if not await rds.rate_hit(r, f"tc:{p.uid}", 30, 60):
        raise HTTPException(429, "Slow down a little — try again in a minute.")
    cfg = await engine.load_config(fs, p.owner_id)
    product = await fs.get(P_product(p.owner_id, body.productId)) if body.productId else None
    language = body.language or cfg.settings.get("language", "hi")
    try:
        direction = purposes.IN if body.direction == "in" else purposes.OUT
        purpose = purposes.resolve(batch_purpose=body.purpose, product=product, settings=cfg.settings, direction=direction)
        return await testchat.test_reply(cfg, product, body.history, body.message, language, chat=testchat.groq_chat,
                                         purpose=purpose, direction=direction)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception:
        raise HTTPException(502, "The AI model didn't respond. Try again.")


# ── call types (the use cases) ───────────────────────────────────────────────
@router.get("/purposes")
async def list_purposes(p: Principal = Depends(get_principal)):
    return {"purposes": purposes.catalog(), "defaultOutbound": purposes.DEFAULT_OUTBOUND, "defaultInbound": purposes.DEFAULT_INBOUND}


# ── test a carrier connection without placing a call ─────────────────────────
@router.post("/telephony/{provider}/test")
async def test_telephony(provider: str, p: Principal = Depends(require_admin)):
    _carrier(provider)
    creds = (await engine.load_secrets(get_fs(), p.owner_id)).get(provider)
    if not creds:
        raise HTTPException(400, "Save your details for this carrier first.")
    if not await rds.rate_hit(rds.get_redis(), f"tt:{p.uid}", 10, 60):
        raise HTTPException(429, "Slow down a little — try again in a minute.")
    return await (verify.check_telnyx(creds) if provider == "telnyx" else verify.check_vobiz(creds))


# ── "which URL goes where" — everything the admin must paste into a carrier ──
@router.get("/setup")
async def setup(p: Principal = Depends(require_admin)):
    s = get_settings()
    base = s.PUBLIC_BASE_URL.rstrip("/")
    local = any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0"))
    tok = tokens.sign_owner(p.owner_id)
    settings = await get_fs().get(P_settings(p.owner_id)) or {}
    warnings = []
    if local:
        warnings.append("PUBLIC_BASE_URL points at localhost. Carriers cannot reach it — start a tunnel (e.g. ngrok http 8000) "
                        "and put the https URL in PUBLIC_BASE_URL.")
    elif not base.startswith("https://"):
        warnings.append("PUBLIC_BASE_URL is not https. Carriers need https (and wss for the audio stream).")
    if not settings.get("inboundEnabled"):
        warnings.append("Inbound calls are switched off. Turn on 'Answer incoming calls' in Voice → Settings.")
    return {
        "publicBaseUrl": base, "isLocalhost": local, "httpsOk": base.startswith("https://"),
        "adminPanel": {"key": "voice_api_base_url", "value": base,
                       "note": "Set this once in the Astric admin panel so the app knows where this service lives."},
        "outbound": {"note": "Nothing to configure for outgoing calls: the call-back URLs are generated per call and signed."},
        "inbound": {
            "enabled": bool(settings.get("inboundEnabled")),
            "telnyx": {"webhookUrl": f"{base}/webhooks/telnyx/inbound/{tok}",
                       "where": "Telnyx portal → Voice → Call Control Applications → your app → Webhook URL (API V2). "
                                "Then assign your Telnyx number to that application."},
            "vobiz": {"answerUrl": f"{base}/webhooks/vobiz/inbound/{tok}",
                      "hangupUrl": f"{base}/webhooks/vobiz/inbound-hangup/{tok}",
                      "where": "Vobiz console → Applications → create/edit an application: Answer URL + Hangup URL (method POST). "
                               "Then attach your Vobiz number to that application."},
        },
        "warnings": warnings,
    }
