"""Calling-hours window + retry timing."""
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_WINDOW = {"start": "09:00", "end": "21:00", "tz": "Asia/Kolkata", "days": [0, 1, 2, 3, 4, 5, 6]}


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _window(w: Optional[dict]) -> dict:
    return {**DEFAULT_WINDOW, **(w or {})}


def in_window(now_utc: datetime, window: Optional[dict], tz_override: Optional[str] = None) -> bool:
    w = _window(window)
    tz = ZoneInfo(tz_override or w["tz"])
    local = now_utc.astimezone(tz)
    if local.weekday() not in w["days"]:
        return False
    sh, sm = _hm(w["start"]); eh, em = _hm(w["end"])
    mins = local.hour * 60 + local.minute
    return sh * 60 + sm <= mins < eh * 60 + em


def next_window_start(now_utc: datetime, window: Optional[dict], tz_override: Optional[str] = None) -> datetime:
    """First instant >= now that is inside the window (UTC)."""
    w = _window(window)
    tz = ZoneInfo(tz_override or w["tz"])
    if in_window(now_utc, w, tz_override):
        return now_utc
    sh, sm = _hm(w["start"])
    local = now_utc.astimezone(tz)
    for add in range(0, 9):
        day = (local + timedelta(days=add)).replace(hour=sh, minute=sm, second=0, microsecond=0)
        if day.weekday() in w["days"] and day.astimezone(timezone.utc) >= now_utc:
            return day.astimezone(timezone.utc)
    return now_utc + timedelta(hours=12)


def clamp_into_window(when_utc: datetime, window: Optional[dict], tz_override: Optional[str] = None) -> datetime:
    """A retry/callback time that lands outside calling hours is moved to the
    next opening."""
    return next_window_start(when_utc, window, tz_override)
