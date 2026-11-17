"""Carrier-facing endpoints. No login: every URL carries a signed per-call token."""
import logging
from datetime import datetime

from fastapi import APIRouter, Request, Response, WebSocket

from app import rds
from app.config import get_settings
from app.crypto import decrypt_json
from app.fs import get_fs
from app.telephony import telnyx as telnyx_mod
from app.telephony.base import HangupInfo
from app.telephony.vobiz import Vobiz
from app.dialer import engine, inbound
from app.tokens import sign_call_id, verify_call_token, verify_owner_token
from app.voice import pipeline
from app.voice.hooks import make_hooks
from app.voice.session import CallSession

log = logging.getLogger(__name__)
router = APIRouter()

# Replaced in tests; production uses Celery (dialer/tasks.py).
def _enqueue(call_id, info, countdown=3):
    from app.dialer.tasks import enqueue_finalize
    enqueue_finalize(call_id, info, countdown)


enqueue_finalize = _enqueue
get_carrier = telnyx_mod.get_carrier

XML = "application/xml"
HANGUP_XML = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


async def _meta_for(token: str):
    call_id = verify_call_token(token)
    if not call_id:
        return None, None
    return call_id, await rds.load_call_meta(rds.get_redis(), call_id)


def k_txin(cid: str) -> str: return f"txin:{cid}"
def k_vbin(uuid: str) -> str: return f"vbin:{uuid}"


# ── Vobiz ──────────────────────────────────────────────────────────────────
@router.post("/webhooks/vobiz/answer/{token}")
async def vobiz_answer(token: str):
    _, meta = await _meta_for(token)
    if not meta:
        return Response(HANGUP_XML, media_type=XML)
    return Response(Vobiz.answer_xml(get_settings().PUBLIC_BASE_URL, token), media_type=XML)


@router.post("/webhooks/vobiz/hangup/{token}")
async def vobiz_hangup(token: str, request: Request):
    call_id, meta = await _meta_for(token)
    if not meta:
        return {"ok": False}
    form = dict(await request.form())
    info = Vobiz.parse_hangup(form)
    enqueue_finalize(call_id, info, 3)
    return {"ok": True}


# ── Telnyx ─────────────────────────────────────────────────────────────────
@router.post("/webhooks/telnyx/{token}")
async def telnyx_events(token: str, request: Request):
    call_id, meta = await _meta_for(token)
    if not meta:
        return {"ok": False}
    try:
        body = await request.json()
    except Exception:
        return {"ok": False}
    event, payload = telnyx_mod.Telnyx.parse_event(body)
    cid = payload.get("call_control_id") or ""
    if event == "call.answered":
        await rds.patch_call_meta(rds.get_redis(), call_id, provider_call_id=cid or meta.get("provider_call_id"))
        try:
            await get_carrier("telnyx").start_streaming(decrypt_json(meta["creds_enc"]), cid, token, get_settings().PUBLIC_BASE_URL)
        except Exception:
            log.exception("telnyx streaming_start failed for %s", call_id)
    elif event == "call.hangup":
        info = telnyx_mod.Telnyx.hangup_info(payload)
        enqueue_finalize(call_id, info, 3)
    return {"ok": True}


# ── media websockets ───────────────────────────────────────────────────────
async def _ws_common(websocket: WebSocket, token: str, provider: str):
    call_id, meta = await _meta_for(token)
    if not meta or meta.get("provider") != provider:
        await websocket.close(code=4403)
        return None
    await websocket.accept()
    session = CallSession(call_id, meta["owner"], meta["lead_id"], meta.get("language", "hi"))
    hooks = make_hooks(get_fs(), rds.get_redis(), meta, get_carrier, enqueue_finalize)
    return meta, session, hooks


@router.websocket("/ws/vobiz/{token}")
async def ws_vobiz(websocket: WebSocket, token: str):
    got = await _ws_common(websocket, token, "vobiz")
    if not got:
        return
    meta, session, hooks = got
    try:
        await pipeline.run_vobiz(websocket, pack=meta["pack"], creds=decrypt_json(meta["creds_enc"]),
                                 provider_call_id=meta.get("provider_call_id") or "", session=session, hooks=hooks)
    except Exception:
        log.exception("vobiz pipeline crashed for %s", meta["call_id"])
        enqueue_finalize(meta["call_id"], HangupInfo(cause="failed"), 3)


@router.websocket("/ws/telnyx/{token}")
async def ws_telnyx(websocket: WebSocket, token: str):
    got = await _ws_common(websocket, token, "telnyx")
    if not got:
        return
    meta, session, hooks = got
    try:
        await pipeline.run_telnyx(websocket, pack=meta["pack"], creds=decrypt_json(meta["creds_enc"]), session=session, hooks=hooks)
    except Exception:
        log.exception("telnyx pipeline crashed for %s", meta["call_id"])
        enqueue_finalize(meta["call_id"], HangupInfo(cause="failed"), 3)


# ── INBOUND ────────────────────────────────────────────────────────────────
# The company pastes these URLs into its OWN carrier account (the app shows them in
# Voice → Settings → "Connect your numbers"). The owner token in the URL is the tenant.
async def _form_get(form, *keys) -> str:
    for k in keys:
        v = form.get(k)
        if v:
            return str(v)
    return ""


@router.post("/webhooks/vobiz/inbound/{owner_token}")
async def vobiz_inbound(owner_token: str, request: Request):
    owner = verify_owner_token(owner_token)
    if not owner:
        return Response(HANGUP_XML, media_type=XML)
    form = await request.form()
    caller = await _form_get(form, "From", "from", "Caller", "CallerID")
    called = await _form_get(form, "To", "to", "Called")
    uuid_ = await _form_get(form, "CallUUID", "call_uuid", "RequestUUID")
    r = rds.get_redis()
    res = await inbound.accept(get_fs(), r, owner=owner, provider="vobiz", caller=caller, called=called, provider_call_id=uuid_)
    if res.reason != "ok":
        log.info("vobiz inbound rejected for %s: %s", owner, res.reason)
        return Response(HANGUP_XML, media_type=XML)
    if uuid_:
        await r.set(k_vbin(uuid_), res.call_id, ex=rds.CALL_TTL)
    return Response(Vobiz.answer_xml(get_settings().PUBLIC_BASE_URL, res.token), media_type=XML)


@router.post("/webhooks/vobiz/inbound-hangup/{owner_token}")
async def vobiz_inbound_hangup(owner_token: str, request: Request):
    if not verify_owner_token(owner_token):
        return {"ok": False}
    info = Vobiz.parse_hangup(dict(await request.form()))
    call_id = await rds.get_redis().get(k_vbin(info.provider_call_id)) if info.provider_call_id else None
    if not call_id:
        return {"ok": False}
    enqueue_finalize(call_id, info, 3)
    return {"ok": True}


@router.post("/webhooks/telnyx/inbound/{owner_token}")
async def telnyx_inbound(owner_token: str, request: Request):
    owner = verify_owner_token(owner_token)
    if not owner:
        return {"ok": False}
    try:
        body = await request.json()
    except Exception:
        return {"ok": False}
    event, payload = telnyx_mod.Telnyx.parse_event(body)
    cid = payload.get("call_control_id") or ""
    if not cid:
        return {"ok": True}
    fs, r = get_fs(), rds.get_redis()
    carrier = get_carrier("telnyx")

    if event == "call.initiated":
        if str(payload.get("direction") or "").lower() != "incoming":
            return {"ok": True}          # our own outbound legs are handled by /webhooks/telnyx/{token}
        creds = (await engine.load_secrets(fs, owner)).get("telnyx")
        if not creds:
            return {"ok": True}
        res = await inbound.accept(fs, r, owner=owner, provider="telnyx", caller=str(payload.get("from") or ""),
                                   called=str(payload.get("to") or ""), provider_call_id=cid)
        if res.reason != "ok":
            log.info("telnyx inbound rejected for %s: %s", owner, res.reason)
            await carrier.hangup(creds, cid)
            return {"ok": True}
        await r.set(k_txin(cid), res.call_id, ex=rds.CALL_TTL)
        await carrier.answer(creds, cid)
        return {"ok": True}

    call_id = await r.get(k_txin(cid))
    if not call_id:
        return {"ok": True}
    meta = await rds.load_call_meta(r, call_id)
    if not meta:
        return {"ok": True}
    if event == "call.answered":
        try:
            await carrier.start_streaming(decrypt_json(meta["creds_enc"]), cid, sign_call_id(call_id), get_settings().PUBLIC_BASE_URL)
        except Exception:
            log.exception("telnyx inbound streaming_start failed for %s", call_id)
    elif event == "call.hangup":
        enqueue_finalize(call_id, telnyx_mod.Telnyx.hangup_info(payload), 3)
    return {"ok": True}
