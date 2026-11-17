"""Vobiz (India). Plivo-style REST + XML. Call setup is: we POST /Call/ with an
answer_url; when the callee answers Vobiz fetches that URL and we reply with
<Stream bidirectional> pointing at our media websocket."""
import logging
from typing import Optional

import httpx

from app.telephony.base import Carrier, HangupInfo, ws_url


def _bare(num: str) -> str:
    """Vobiz (Plivo-compatible) wants plain digits for `from`/`to` in the Call
    create API — no leading '+', no spaces. The app stores/collects numbers
    as "+91 80 1234 5678", so clean it here rather than changing storage
    everywhere else."""
    import re
    return re.sub(r"\D", "", num or "")

log = logging.getLogger(__name__)
VOBIZ_BASE = "https://api.vobiz.ai/api/v1"


class Vobiz(Carrier):
    name = "vobiz"
    required_fields = ("auth_id", "auth_token", "from_number")

    def _client(self, creds: dict) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=VOBIZ_BASE, timeout=20.0, headers={
            "X-Auth-ID": creds["auth_id"], "X-Auth-Token": creds["auth_token"], "Content-Type": "application/json"})

    async def place_call(self, creds, to_e164, call_id, token, base_url) -> Optional[str]:
        payload = {
            "from": _bare(creds["from_number"]), "to": _bare(to_e164),
            "answer_url": f"{base_url.rstrip('/')}/webhooks/vobiz/answer/{token}", "answer_method": "POST",
            "hangup_url": f"{base_url.rstrip('/')}/webhooks/vobiz/hangup/{token}", "hangup_method": "POST",
        }
        try:
            async with self._client(creds) as c:
                r = await c.post(f"/Account/{creds['auth_id']}/Call/", json=payload)
                r.raise_for_status()
                d = r.json()
                return d.get("request_uuid") or d.get("call_uuid") or d.get("api_id")
        except httpx.HTTPStatusError as e:
            # Vobiz's response BODY says exactly what's wrong (bad number, bad url, etc);
            # the status code alone (what was logged before) hides that.
            log.error("Vobiz place_call failed: HTTP %s %s | payload=%s", e.response.status_code, e.response.text[:500], payload)
            return None
        except Exception as e:
            log.error("Vobiz place_call failed: %s | payload=%s", e, payload)
            return None

    async def hangup(self, creds, provider_call_id) -> bool:
        try:
            async with self._client(creds) as c:
                r = await c.delete(f"/Account/{creds['auth_id']}/Call/{provider_call_id}/")
                r.raise_for_status()
                return True
        except Exception as e:
            log.error("Vobiz hangup failed: %s", e)
            return False

    @staticmethod
    def answer_xml(base_url: str, token: str) -> str:
        # audioTrack="inbound" is REQUIRED with bidirectional="true" (confirmed in the old project).
        return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
                '<Stream bidirectional="true" audioTrack="inbound" contentType="audio/x-mulaw;rate=8000" '
                f'keepCallAlive="true">{ws_url(base_url, "/ws/vobiz/" + token)}</Stream></Response>')

    @staticmethod
    def parse_hangup(form: dict) -> HangupInfo:
        def num(*keys):
            for k in keys:
                v = form.get(k)
                if v not in (None, ""):
                    try: return float(v)
                    except ValueError: pass
            return None
        status = str(form.get("CallStatus") or form.get("Status") or "").lower()
        cause = str(form.get("HangupCause") or form.get("hangup_cause") or "").lower()
        dur = num("Duration", "BillDuration", "CallDuration", "BillingDuration")
        text = f"{status} {cause}"
        norm = ""
        if "busy" in text: norm = "busy"
        elif any(x in text for x in ("no-answer", "no_answer", "noanswer", "timeout", "unanswered")): norm = "no_answer"
        elif any(x in text for x in ("fail", "reject", "cancel", "invalid", "unreachable")): norm = "failed"
        elif "complete" in text or "normal" in text: norm = "completed"
        return HangupInfo(provider_call_id=str(form.get("CallUUID") or form.get("RequestUUID") or ""),
                          duration_seconds=dur, answered=None, cause=norm)
