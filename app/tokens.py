"""Signed per-call tokens for webhook / websocket URLs.

The carrier calls back to URLs we generated, e.g. wss://host/ws/vobiz/<token>.
The token is HMAC(call_id) so nobody can open a media socket or fake a
hangup for a call this backend never placed.
"""
import hashlib
import hmac
from typing import Optional

from app.config import get_settings


def _key() -> bytes:
    k = get_settings().CALL_TOKEN_SECRET
    if not k:
        raise RuntimeError("CALL_TOKEN_SECRET is not set")
    return k.encode()


def sign_call_id(call_id: str) -> str:
    # '_' not '.': Vobiz's answer_url validator rejects a URL whose last path
    # segment contains a dot (it reads it as a file extension) — confirmed live,
    # error "answer_url parameter is not valid". call_id is a uuid4().hex, which
    # never contains '_', so rpartition("_") below is unambiguous.
    sig = hmac.new(_key(), call_id.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{call_id}_{sig}"


def verify_call_token(token: str) -> Optional[str]:
    """Returns the call_id if the token is genuine, else None."""
    if not token or "_" not in token:
        return None
    call_id, _, sig = token.rpartition("_")
    expected = hmac.new(_key(), call_id.encode(), hashlib.sha256).hexdigest()[:32]
    return call_id if hmac.compare_digest(sig, expected) else None


# ── per-tenant INBOUND token ────────────────────────────────────────────────
# The inbound webhook URL a company pastes into its carrier contains this token.
# Different HMAC domain than call tokens, so a call token can never be replayed
# as an inbound token (or vice-versa).
def sign_owner(owner_id: str) -> str:
    # Same '_' reasoning as sign_call_id above — this token also ends up in a
    # carrier-facing URL (the inbound webhook URL). rpartition splits on the
    # LAST '_', so this is safe even if owner_id itself contains one — the
    # sig is always 32 hex characters, which never contain '_'.
    sig = hmac.new(_key(), b"inbound:" + owner_id.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{owner_id}_{sig}"


def verify_owner_token(token: str) -> Optional[str]:
    if not token or "_" not in token:
        return None
    owner, _, sig = token.rpartition("_")
    expected = hmac.new(_key(), b"inbound:" + owner.encode(), hashlib.sha256).hexdigest()[:32]
    return owner if hmac.compare_digest(sig, expected) else None
