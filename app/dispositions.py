"""What happens to a lead after a call.

`plan_outcome` is a PURE function (no I/O) so every rule is unit-tested.
`apply_outcome` is the only place that writes the result to Firestore.

The app's lead stages are few (new_, contacted, qualified, proposal, won,
lost), so the detailed AI outcome lives in lead.voice.lastOutcome, and the
app's own stage/priority only move on clear signals — never backwards, and
never out of won/lost.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.fs import ARRAY_UNION, DELETE_FIELD, P_lead
from app import phone as phonelib
from app.windows import clamp_into_window

DISPOSITIONS = (
    "interested", "callback", "not_interested", "wrong_number", "dnc",
    "voicemail", "no_answer", "busy", "failed", "human_requested", "connected_other",
    # added for the wider use cases (booking, enquiries, support, reminders)
    "appointment_booked", "resolved", "confirmed", "unresolved",
)
RETRYABLE = {"no_answer", "busy", "voicemail", "failed"}
STAGE_RANK = {"new_": 0, "contacted": 1, "qualified": 2, "proposal": 3, "won": 4, "lost": 4}
DEFAULT_RETRY_GAP_MIN = {"no_answer": 120, "busy": 30, "voicemail": 240, "failed": 60}
DEFAULT_MAX_ATTEMPTS = 3
VOICE_USER = "Astric Voice"


@dataclass
class OutcomePlan:
    disposition: str
    state: str                       # done|dnc|callback_scheduled|retry_scheduled|exhausted
    lead_updates: dict = field(default_factory=dict)
    followups: list = field(default_factory=list)
    activity_note: str = ""
    retry_at: Optional[datetime] = None
    terminal: bool = True            # lead needs no further AI call in this batch


def _advance(current: str, target: str) -> Optional[str]:
    """Move a stage forward only; never touch won/lost; None = leave as is."""
    cur = current or "new_"
    if cur in ("won", "lost"):
        return None
    return target if STAGE_RANK.get(cur, 0) < STAGE_RANK[target] else None


def _followup(note: str, due: datetime, now: datetime) -> dict:
    return {"id": uuid.uuid4().hex, "note": note, "completed": False, "dueAt": due, "createdAt": now}


def plan_outcome(*, disposition: str, lead: dict, voice_settings: dict, now: datetime,
                 attempts: int, summary: str = "", temperature: Optional[str] = None,
                 callback_at: Optional[datetime] = None,
                 appointment_at: Optional[datetime] = None) -> OutcomePlan:
    if disposition not in DISPOSITIONS:
        disposition = "connected_other"
    stage = (lead or {}).get("stage", "new_")
    phone_norm = (lead or {}).get("phoneNorm", "")
    window = voice_settings.get("window")
    tz = phonelib.timezone_for(phone_norm, (window or {}).get("tz", "Asia/Kolkata"))
    max_attempts = int(voice_settings.get("maxAttempts") or DEFAULT_MAX_ATTEMPTS)
    gaps = {**DEFAULT_RETRY_GAP_MIN, **(voice_settings.get("retryGapMinutes") or {})}
    summary = (summary or "").strip()

    u: dict = {
        "voice.lastOutcome": disposition,
        "voice.lastCallAt": now,
        "voice.attempts": attempts,
        "voice.needsHuman": False,
    }
    if summary:
        u["voice.lastSummary"] = summary[:600]
    plan = OutcomePlan(disposition=disposition, state="done", lead_updates=u)
    label = disposition.replace("_", " ")
    plan.activity_note = f"AI call — {label}" + (f": {summary[:300]}" if summary else "")

    if disposition == "interested":
        new_stage = _advance(stage, "qualified")
        if new_stage: u["stage"] = new_stage
        u["priority"] = temperature if temperature in ("hot", "warm", "cold") else "hot"
        u["voice.needsHuman"] = True
        plan.followups.append(_followup("AI call: lead is interested — please call back", now + timedelta(hours=1), now))

    elif disposition == "appointment_booked":
        new_stage = _advance(stage, "qualified")
        if new_stage: u["stage"] = new_stage
        u["priority"] = temperature if temperature in ("hot", "warm", "cold") else "hot"
        when = appointment_at or (now + timedelta(days=1))
        u["voice.appointmentAt"] = when
        note = "AI call: appointment booked" + ("" if appointment_at else " (time not captured — please confirm)")
        plan.followups.append(_followup(note, when, now))
        if not appointment_at:
            u["voice.needsHuman"] = True

    elif disposition in ("resolved", "confirmed"):
        new_stage = _advance(stage, "contacted")
        if new_stage: u["stage"] = new_stage

    elif disposition == "unresolved":
        new_stage = _advance(stage, "contacted")
        if new_stage: u["stage"] = new_stage
        u["voice.needsHuman"] = True
        plan.followups.append(_followup("AI call: issue or question was not resolved — please follow up", now + timedelta(minutes=30), now))

    elif disposition == "callback":
        new_stage = _advance(stage, "contacted")
        if new_stage: u["stage"] = new_stage
        when = callback_at or (now + timedelta(hours=4))
        when = clamp_into_window(when, window, tz)
        plan.retry_at, plan.state, plan.terminal = when, "callback_scheduled", False
        plan.followups.append(_followup("AI call: lead asked for a callback", when, now))

    elif disposition == "not_interested":
        u["priority"] = "cold"
        if voice_settings.get("autoMarkLost") and STAGE_RANK.get(stage, 0) <= 1 and stage not in ("won", "lost"):
            u["stage"] = "lost"
        else:
            new_stage = _advance(stage, "contacted")
            if new_stage: u["stage"] = new_stage

    elif disposition == "wrong_number":
        u["voice.invalidNumber"] = True

    elif disposition == "dnc":
        plan.state = "dnc"
        u["voice.dnc"] = True
        u["priority"] = "cold"

    elif disposition in ("human_requested", "connected_other"):
        new_stage = _advance(stage, "contacted")
        if new_stage: u["stage"] = new_stage
        u["voice.needsHuman"] = True
        why = "lead asked to speak to a person" if disposition == "human_requested" else "the call needs a human look"
        plan.followups.append(_followup(f"AI call: {why}", now + timedelta(minutes=30), now))

    else:  # no_answer / busy / voicemail / failed
        if attempts < max_attempts:
            when = clamp_into_window(now + timedelta(minutes=int(gaps.get(disposition, 120))), window, tz)
            plan.retry_at, plan.state, plan.terminal = when, "retry_scheduled", False
        else:
            plan.state = "exhausted"

    u["voice.state"] = plan.state
    u["voice.nextCallAt"] = plan.retry_at if plan.retry_at else DELETE_FIELD
    return plan


async def apply_outcome(fs, owner: str, lead_id: str, plan: OutcomePlan, now: datetime) -> None:
    """Targeted writes only — never rewrites the lead's own arrays."""
    data = dict(plan.lead_updates)
    activity = {"id": uuid.uuid4().hex, "note": plan.activity_note, "createdBy": VOICE_USER,
                "type": "call", "createdAt": now}
    data["activities"] = ARRAY_UNION(activity)
    if plan.followups:
        data["followUps"] = ARRAY_UNION(*plan.followups)
    data["updatedAt"] = now
    await fs.update(P_lead(owner, lead_id), data)
