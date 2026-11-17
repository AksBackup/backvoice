"""Inbound calls: a customer rings the company's own carrier number.

Tenancy: the webhook URL the company pastes into its carrier contains a signed
owner token (tokens.sign_owner) — no login is possible on a carrier callback,
and the token proves which company the call belongs to.

An accepted inbound call produces exactly the same artefacts as an outbound one
(Redis call meta, voice_calls/{id}, a lead) so the media websocket, billing,
analysis and finalisation code are shared unchanged.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app import phone as phonelib, purposes, rds, tokens, wallet
from app.config import get_settings
from app.crypto import encrypt_json
from app.dialer import engine
from app.dialer.leads import find_existing_lead
from app.fs import P_call, P_lead

log = logging.getLogger(__name__)


@dataclass
class InboundResult:
    reason: str                       # ok | disabled | no_carrier | no_minutes | busy | bad_number | error
    call_id: str = ""
    token: str = ""
    creds: dict = field(default_factory=dict)


async def accept(fs, r, *, owner: str, provider: str, caller: str, called: str, provider_call_id: str,
                 now: Optional[datetime] = None) -> InboundResult:
    now = now or engine.now_utc()
    cfg = await engine.load_config(fs, owner)
    if not cfg.settings.get("inboundEnabled"):
        return InboundResult("disabled")
    creds = cfg.creds.get(provider)
    if not creds:
        return InboundResult("no_carrier")
    caller_norm = phonelib.digits(caller)
    if not (8 <= len(caller_norm) <= 15):
        return InboundResult("bad_number", creds=creds)
    ok, _why = await wallet.gate(fs, owner, await rds.inflight(r, owner))
    if not ok:
        return InboundResult("no_minutes", creds=creds)
    s = get_settings()
    cap = min(int(cfg.settings.get("maxConcurrent") or s.MAX_CONCURRENT_PER_TENANT), s.MAX_CONCURRENT_PER_TENANT)
    if await rds.inflight(r, owner) >= cap or await rds.inflight_global(r) >= s.MAX_CONCURRENT_GLOBAL:
        return InboundResult("busy", creds=creds)

    # A caller is NOT made a lead here. Only an enquiry becomes one, and that is only known once the call has been
    # analysed (engine.finalize_call). A caller who is already a lead is matched by phone number as before.
    lead_id, lead = await find_existing_lead(fs, owner, caller_norm)
    lock_key = lead_id or f"caller-{caller_norm}"
    if not await rds.lock_lead(r, owner, lock_key):
        return InboundResult("busy", creds=creds)
    try:
        purpose = purposes.resolve(settings=cfg.settings, direction=purposes.IN)
        language = (lead.get("voice") or {}).get("language") or phonelib.default_language(
            caller_norm, cfg.settings.get("language", "hi"))
        call_id = uuid.uuid4().hex
        token = tokens.sign_call_id(call_id)
        lead = lead or {}
        attempts = int((lead.get("voice") or {}).get("attempts") or 0)   # an inbound call is not an outbound attempt
        pack = engine.build_pack(cfg=cfg, lead=lead, product=None, language=language, attempt=attempts,
                                 purpose=purpose, direction=purposes.IN)
        tz = phonelib.timezone_for(caller_norm, (cfg.settings.get("window") or {}).get("tz", "Asia/Kolkata"))
        meta = {"call_id": call_id, "owner": owner, "batch_id": None, "lead_id": lead_id, "lock_key": lock_key, "product_id": None,
                "provider": provider, "language": language, "attempt": attempts, "to": phonelib.to_e164(caller_norm),
                "from": phonelib.to_e164(caller_norm), "called": called, "pack": pack, "creds_enc": encrypt_json(creds),
                "provider_call_id": provider_call_id, "dialed_at": now.isoformat(), "tz": tz,
                "purpose": purpose, "direction": "in"}
        await rds.save_call_meta(r, call_id, meta)
        await rds.mark_active(r, owner, None, call_id)
        await fs.set(P_call(owner, call_id), {
            "callId": call_id, "leadId": lead_id or "", "leadName": lead.get("name") or phonelib.to_e164(caller_norm), "batchId": None, "productId": None,
            "provider": provider, "to": called, "from": meta["from"], "language": language, "attempt": attempts,
            "purpose": purpose, "direction": "in", "providerCallId": provider_call_id,
            "status": "ringing", "transcript": [], "createdAt": now,
            "visibleTo": list(lead.get("visibleTo") or [owner]), "ownerId": lead.get("ownerId", owner)})
        if lead_id:
            await fs.update(P_lead(owner, lead_id), {"voice.currentCallId": call_id})
        return InboundResult("ok", call_id=call_id, token=token, creds=creds)
    except Exception:
        log.exception("accept inbound failed for %s", owner)
        await rds.unlock_lead(r, owner, lock_key)
        return InboundResult("error", creds=creds)
