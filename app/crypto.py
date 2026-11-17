"""Encryption for per-company telephony credentials (stored in Firestore
under voice_secrets/, which no client can read — rules deny it — and which
is ALSO encrypted so a leaked Firestore export doesn't leak carrier keys)."""
import json
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().SECRETS_KEY
    if not key:
        raise RuntimeError("SECRETS_KEY is not set")
    return Fernet(key.encode())


def encrypt_json(obj: dict) -> str:
    return _fernet().encrypt(json.dumps(obj).encode()).decode()


def decrypt_json(token: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except InvalidToken as e:
        raise ValueError("Stored credentials could not be decrypted (wrong SECRETS_KEY?)") from e


def mask(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    return "•" * 6 + v[-4:] if len(v) > 4 else "•" * len(v)
