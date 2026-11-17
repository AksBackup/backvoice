"""Glue between a live call (pipeline) and Firestore / the carrier."""
from __future__ import annotations

import logging
from typing import Callable

from app import rds
from app.crypto import decrypt_json
from app.fs import P_call, P_lead
from app.telephony.base import HangupInfo
from app.voice.pipeline import CallHooks
from app.voice.session import CallSession

log = logging.getLogger(__name__)


def make_hooks(fs, r, meta: dict, carrier_factory: Callable, enqueue_finalize: Callable) -> CallHooks:
    owner, call_id, lead_id = meta["owner"], meta["call_id"], meta["lead_id"]

    async def on_answered(s: CallSession):
        await fs.update(P_call(owner, call_id), {"status": "in_progress", "answeredAt": s.answered_at, "transcript": s.transcript})

    async def on_progress(s: CallSession):
        await fs.update(P_call(owner, call_id), {"transcript": s.transcript})

    async def on_ended(s: CallSession):
        await fs.update(P_call(owner, call_id), {
            "status": "wrapping_up",   # the app stops showing "On call" the moment the line closes; finalisation sets the final status
            "transcript": s.transcript, "sessionEndedAt": s.ended_at, "connectedSeconds": round(s.connected_seconds(), 1),
            "endReason": s.end_reason or "caller_hung_up",
            "usage": {"ttsChars": s.tts_chars, "llmPromptTokens": s.llm_prompt_tokens, "llmCompletionTokens": s.llm_completion_tokens}})
        # Backstop: normally the carrier's hangup webhook settles the call first (finalisation is idempotent).
        enqueue_finalize(call_id, None, 15)

    async def hangup() -> bool:
        m = await rds.load_call_meta(r, call_id) or meta
        if not m.get("provider_call_id"):
            return False
        return await carrier_factory(m["provider"]).hangup(decrypt_json(m["creds_enc"]), m["provider_call_id"])

    return CallHooks(on_answered=on_answered, on_progress=on_progress, on_ended=on_ended, hangup=hangup)
