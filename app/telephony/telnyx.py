"""Telnyx (everywhere except India). Call Control v2: place the call, and when
`call.answered` arrives start bidirectional media streaming (PCMU) to our
websocket. `call.hangup` closes the loop."""
import base64
import logging
from typing import Optional

import httpx

from app.config import get_settings
from app.telephony.base import Carrier, HangupInfo, ws_url

log = logging.getLogger(__name__)
TELNYX_BASE = "https://api.telnyx.com/v2"


class Telnyx(Carrier):
    name = "telnyx"
    required_fields = ("api_key", "connection_id", "from_number")

    def _client(self, creds: dict) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=TELNYX_BASE, timeout=20.0, headers={
            "Authorization": f"Bearer {creds['api_key']}", "Content-Type": "application/json"})

    async def place_call(self, creds, to_e164, call_id, token, base_url) -> Optional[str]:
        payload = {
            "connection_id": creds["connection_id"], "to": to_e164, "from": creds["from_number"],
            "webhook_url": f"{base_url.rstrip('/')}/webhooks/telnyx/{token}", "webhook_url_method": "POST",
            "timeout_secs": get_settings().RING_TIMEOUT_SECONDS,
            "client_state": base64.b64encode(call_id.encode()).decode(),
        }
        try:
            async with self._client(creds) as c:
                r = await c.post("/calls", json=payload)
                r.raise_for_status()
                return r.json()["data"]["call_control_id"]
        except Exception as e:
            log.error("Telnyx place_call failed: %s", e)
            return None

    async def _action(self, creds, cid: str, action: str, payload: dict) -> bool:
        try:
            async with self._client(creds) as c:
                r = await c.post(f"/calls/{cid}/actions/{action}", json=payload)
                r.raise_for_status()
                return True
        except Exception as e:
            log.error("Telnyx %s failed: %s", action, e)
            return False

    async def answer(self, creds, cid: str) -> bool:
        """Pick up an INCOMING call (Call Control `answer`)."""
        return await self._action(creds, cid, "answer", {})

    async def start_streaming(self, creds, cid: str, token: str, base_url: str) -> bool:
        return await self._action(creds, cid, "streaming_start", {
            "stream_url": ws_url(base_url, "/ws/telnyx/" + token),
            "stream_track": "inbound_track", "stream_codec": "PCMU",
            "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU",
        })

    async def hangup(self, creds, provider_call_id) -> bool:
        return await self._action(creds, provider_call_id, "hangup", {})

    @staticmethod
    def parse_event(body: dict) -> tuple[str, dict]:
        data = (body or {}).get("data") or {}
        return str(data.get("event_type") or ""), (data.get("payload") or {})

    @staticmethod
    def hangup_info(payload: dict) -> HangupInfo:
        cause = str(payload.get("hangup_cause") or "").lower()
        norm = {"user_busy": "busy", "timeout": "no_answer", "no_answer": "no_answer", "originator_cancel": "failed",
                "call_rejected": "failed", "unallocated_number": "failed", "normal_clearing": "completed"}.get(cause, "")
        return HangupInfo(provider_call_id=str(payload.get("call_control_id") or ""), duration_seconds=None,
                          answered=None, cause=norm)


CARRIERS = {"vobiz": None, "telnyx": None}


def get_carrier(name: str) -> Carrier:
    from app.telephony.vobiz import Vobiz
    if name == "vobiz": return Vobiz()
    if name == "telnyx": return Telnyx()
    raise ValueError(f"unknown carrier {name}")
