"""Same login as the Astric app: a Firebase ID token in `Authorization: Bearer`.

The tenant is NEVER taken from the request. It is derived from the verified
uid: admin -> their own uid; employee -> users/{uid}.adminId (exactly how the
app's orgOwnerIdProvider resolves it).
"""
import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from fastapi import Depends, Header, HTTPException

from app.fs import get_fs, P_user


@dataclass
class Principal:
    uid: str
    owner_id: str
    role: str
    name: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def _firebase_verify(token: str) -> dict:
    from firebase_admin import auth as fb_auth
    from app.fs import get_fs  # ensures firebase_admin is initialised
    get_fs()
    return await asyncio.to_thread(fb_auth.verify_id_token, token)


_verifier: Callable[[str], Awaitable[dict]] = _firebase_verify


def set_token_verifier(fn):
    """Tests inject a fake verifier."""
    global _verifier
    _verifier = fn


async def get_principal(authorization: Optional[str] = Header(default=None)) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        decoded = await _verifier(token)
    except Exception:
        raise HTTPException(401, "Invalid or expired token.")
    uid = decoded.get("uid") or decoded.get("user_id") or decoded.get("sub")
    if not uid:
        raise HTTPException(401, "Invalid token.")
    user = await get_fs().get(P_user(uid))
    if user is None:
        raise HTTPException(403, "No Astric account found for this login.")
    role = user.get("role", "admin")
    owner = user.get("adminId") if (role == "employee" and user.get("adminId")) else uid
    return Principal(uid=uid, owner_id=owner, role=role, name=user.get("name", ""))


async def require_admin(p: Principal = Depends(get_principal)) -> Principal:
    if not p.is_admin:
        raise HTTPException(403, "Only the account admin can do this.")
    return p
