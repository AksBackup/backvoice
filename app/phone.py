"""Phone handling. The app stores leads' `phoneNorm` as digits only, with the
country code (a bare 10-digit number is assumed Indian by the app, so
non-Indian leads MUST be entered with their country code)."""
import re
from typing import Iterable, Optional


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def to_e164(phone_norm: str) -> Optional[str]:
    d = digits(phone_norm).lstrip("0") if digits(phone_norm).startswith("00") else digits(phone_norm)
    if d.startswith("00"):
        d = d[2:]
    if not (8 <= len(d) <= 15):
        return None
    return "+" + d


def is_india(phone_norm: str) -> bool:
    d = digits(phone_norm)
    return len(d) == 12 and d.startswith("91") and d[2] in "6789"


def country_code(phone_norm: str) -> str:
    d = digits(phone_norm)
    if is_india(d):
        return "91"
    for n in (3, 2, 1):  # longest-first against the few codes we map
        if d[:n] in _TZ:
            return d[:n]
    return d[:2]


# Rough local timezone by country code, used to keep calls inside the
# lead's own daytime. Anything unmapped falls back to the company timezone.
_TZ = {
    "91": "Asia/Kolkata", "1": "America/New_York", "44": "Europe/London",
    "971": "Asia/Dubai", "966": "Asia/Riyadh", "65": "Asia/Singapore",
    "61": "Australia/Sydney", "49": "Europe/Berlin", "33": "Europe/Paris",
    "977": "Asia/Kathmandu", "880": "Asia/Dhaka", "94": "Asia/Colombo",
}


def timezone_for(phone_norm: str, default: str = "Asia/Kolkata") -> str:
    return _TZ.get(country_code(phone_norm), default)


PROVIDERS = ("vobiz", "telnyx")


def route_provider(phone_norm: str, configured: Iterable[str], preferred: Optional[str] = None) -> Optional[str]:
    """Pick the carrier for a number.

    preferred = "vobiz" | "telnyx": the user's explicit choice. It is honoured only if that carrier is set up;
    otherwise None (we never silently place the call on a carrier the user did not pick).
    preferred = None / "" / "auto": India -> Vobiz (falls back to Telnyx if only Telnyx is set up);
    everything else -> Telnyx. None = can't be dialled with this account."""
    have = set(configured)
    pref = (preferred or "").strip().lower()
    if pref in PROVIDERS:
        return pref if pref in have else None
    if is_india(phone_norm):
        return "vobiz" if "vobiz" in have else ("telnyx" if "telnyx" in have else None)
    return "telnyx" if "telnyx" in have else None


def default_language(phone_norm: str, company_default: str = "hi") -> str:
    return company_default if is_india(phone_norm) else "en"
