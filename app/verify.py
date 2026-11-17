"""Credential checks ("Test connection" buttons).

Each returns {"ok": True|False|None, "detail": str}:
  True   the provider accepted the credentials
  False  the provider rejected them (401/403 / not found)
  None   we could not tell (network error, unexpected status) — never reported as a pass

They only make cheap, read-only (or tiny) provider calls; no phone call is placed.
"""
from __future__ import annotations

from typing import Optional

import httpx

TELNYX = "https://api.telnyx.com/v2"
VOBIZ = "https://api.vobiz.ai/api/v1"


def _res(ok, detail): return {"ok": ok, "detail": detail}


async def check_telnyx(creds: dict, transport: Optional[httpx.AsyncBaseTransport] = None) -> dict:
    h = {"Authorization": f"Bearer {creds.get('api_key', '')}"}
    try:
        async with httpx.AsyncClient(timeout=12.0, transport=transport, headers=h) as c:
            r = await c.get(f"{TELNYX}/balance")
            if r.status_code in (401, 403):
                return _res(False, "Telnyx rejected the API key.")
            if r.status_code != 200:
                return _res(None, f"Telnyx answered HTTP {r.status_code}; could not verify the key.")
            conn = (creds.get("connection_id") or "").strip()
            if conn:
                r2 = await c.get(f"{TELNYX}/call_control_applications/{conn}")
                if r2.status_code == 404:
                    return _res(False, "API key is valid, but that Connection ID was not found. Use the Call Control Application ID.")
                if r2.status_code not in (200,):
                    return _res(None, f"API key is valid; could not check the Connection ID (HTTP {r2.status_code}).")
            return _res(True, "Telnyx API key and Connection ID are valid.")
    except httpx.HTTPError as e:
        return _res(None, f"Could not reach Telnyx: {e.__class__.__name__}.")


async def check_vobiz(creds: dict, transport: Optional[httpx.AsyncBaseTransport] = None) -> dict:
    h = {"X-Auth-ID": creds.get("auth_id", ""), "X-Auth-Token": creds.get("auth_token", "")}
    try:
        # follow_redirects=True: Vobiz 307-redirects GET /Account/{id}/ (confirmed live);
        # without this httpx treats the redirect itself as the response and we never see
        # the real 200/401, so a valid key was wrongly reported as "could not verify".
        async with httpx.AsyncClient(timeout=12.0, transport=transport, headers=h, follow_redirects=True) as c:
            r = await c.get(f"{VOBIZ}/Account/{creds.get('auth_id', '')}/")
            if r.status_code in (401, 403):
                return _res(False, "Vobiz rejected the Auth ID / Auth token.")
            if 200 <= r.status_code < 300:
                return _res(True, "Vobiz credentials are valid.")
            return _res(None, f"Vobiz answered HTTP {r.status_code}; could not verify the credentials.")
    except httpx.HTTPError as e:
        return _res(None, f"Could not reach Vobiz: {e.__class__.__name__}.")
