"""Lead helpers for INBOUND calls (kept separate from engine/inbound so both can use them without an import cycle).

Rule: a caller becomes a lead only when the call turns out to be an ENQUIRY (a new person asking about what the
company sells). Support calls, follow-ups on earlier dealings, existing customers, spam and wrong numbers do not.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from app import phone as phonelib
from app.fs import P_lead, P_leads, P_phone_index, P_customers, SERVER_TS


async def find_existing_lead(fs, owner: str, caller_norm: str) -> tuple[Optional[str], dict]:
    """The lead that already has this phone number (index doc first, then a legacy query), else (None, {})."""
    idx = await fs.get(P_phone_index(owner, caller_norm))
    if idx and idx.get("leadId"):
        lead = await fs.get(P_lead(owner, idx["leadId"]))
        if lead:
            return idx["leadId"], lead
    for lid, lead in await fs.query(P_leads(owner), [("phoneNorm", "==", caller_norm)], limit=1):
        return lid, lead
    return None, {}


async def is_existing_customer(fs, owner: str, caller_norm: str) -> bool:
    """Best effort: customers store the phone as typed, so try the common spellings of the same number."""
    last10 = caller_norm[-10:]
    for v in dict.fromkeys(["+" + caller_norm, caller_norm, last10, "+91" + last10, "+91 " + last10]):
        try:
            if await fs.query(P_customers(owner), [("phone", "==", v)], limit=1):
                return True
        except Exception:
            return False
    return False


async def create_enquiry_lead(fs, owner: str, caller_norm: str, now: datetime, *, name: str = "", summary: str = "",
                              interest: str = "", temperature: Optional[str] = None) -> Optional[str]:
    """Creates the lead for an enquiry call and returns its id (or the id of the lead that appeared in the meantime)."""
    existing, _ = await find_existing_lead(fs, owner, caller_norm)
    if existing:
        return existing
    e164 = phonelib.to_e164(caller_norm)
    name = (name or "").strip()[:80] or f"Caller {e164}"
    note = (summary or "").strip()
    if interest:
        note = (f"Asked about: {interest.strip()[:120]}. " + note).strip()
    lead_id = uuid.uuid4().hex
    lead = {
        "name": name, "email": "", "phone": e164, "phoneNorm": caller_norm, "company": "",
        "stage": "new_", "priority": temperature if temperature in ("hot", "warm", "cold") else "warm",
        "assignedTo": "", "estimatedValue": None, "notes": note,
        "source": "other", "sourceCustom": "Inbound call (enquiry)",
        "activities": [{"id": uuid.uuid4().hex, "note": "Enquiry call answered by Astric Voice", "type": "source",
                        "createdBy": "Astric Voice", "createdAt": now}],
        "followUps": [{"id": uuid.uuid4().hex, "note": "New enquiry from an inbound call — please follow up",
                       "completed": False, "dueAt": now + timedelta(minutes=30), "createdAt": now}],
        "sentEmails": [], "customFields": {}, "categories": [], "ownerId": owner, "ownerName": "", "createdBy": owner,
        "batchId": "", "sharedWith": [], "visibleTo": [owner], "createdAt": now, "updatedAt": now,
    }
    await fs.set(P_lead(owner, lead_id), lead)
    await fs.set(P_phone_index(owner, caller_norm), {"leadId": lead_id, "createdBy": owner, "createdAt": SERVER_TS})
    return lead_id
