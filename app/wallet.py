"""Minutes wallet — the Python half of users/{owner}/voice/wallet.

The Node payment backend ONLY ever increments `minutesPurchased`.
This module ONLY ever increments `minutesUsed`. Remaining = purchased - used.
"""
import math
from dataclasses import dataclass

from app.config import get_settings
from app.fs import INCREMENT, SERVER_TS, P_wallet


@dataclass
class Wallet:
    purchased: float
    used: float

    @property
    def remaining(self) -> float:
        return round(self.purchased - self.used, 4)


async def get_wallet(fs, owner: str) -> Wallet:
    d = await fs.get(P_wallet(owner)) or {}
    return Wallet(float(d.get("minutesPurchased") or 0), float(d.get("minutesUsed") or 0))


def billable_minutes(connected_seconds: float) -> float:
    """Connected talk time only: ringing / unanswered / blips are free;
    otherwise rounded UP to the next BILLING_ROUND_SECONDS."""
    s = get_settings()
    if connected_seconds is None or connected_seconds < s.MIN_BILLABLE_SECONDS:
        return 0.0
    step = s.BILLING_ROUND_SECONDS
    return round(math.ceil(connected_seconds / step) * step / 60.0, 4)


async def charge(fs, owner: str, connected_seconds: float) -> float:
    minutes = billable_minutes(connected_seconds)
    if minutes > 0:
        await fs.set(P_wallet(owner), {"minutesUsed": INCREMENT(minutes), "lastUsedAt": SERVER_TS}, merge=True)
    return minutes


async def gate(fs, owner: str, inflight_calls: int = 0) -> tuple[bool, str]:
    """Can this account start ONE more call right now?"""
    s = get_settings()
    w = await get_wallet(fs, owner)
    reserve = s.INFLIGHT_RESERVE_MINUTES
    available = w.remaining - inflight_calls * reserve
    if w.purchased <= 0:
        return False, "no_minutes_purchased"
    if available < reserve:
        return False, "low_balance"
    return True, "ok"
