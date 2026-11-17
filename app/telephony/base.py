"""Carrier adapter interface. One small file per carrier; the dialer and
pipeline only ever talk to this."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class HangupInfo:
    provider_call_id: str = ""
    duration_seconds: Optional[float] = None   # carrier-reported (fallback only)
    answered: Optional[bool] = None            # None = unknown
    cause: str = ""                            # normalised: no_answer|busy|failed|completed|""


class Carrier(ABC):
    name: str = ""
    required_fields: tuple = ()

    @abstractmethod
    async def place_call(self, creds: dict, to_e164: str, call_id: str, token: str, base_url: str) -> Optional[str]:
        """Start an outbound call. Returns the carrier's call id, or None."""

    @abstractmethod
    async def hangup(self, creds: dict, provider_call_id: str) -> bool: ...

    @classmethod
    def validate_creds(cls, creds: dict) -> list[str]:
        return [f for f in cls.required_fields if not str((creds or {}).get(f, "")).strip()]


def ws_url(base_url: str, path: str) -> str:
    return base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + path
