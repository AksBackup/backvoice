"""Post-call analysis: transcript -> structured outcome.

Runs AFTER the call on a cheap model (DeepSeek by default, Groq fallback);
the live call never waits for it. Deciding the disposition from the whole
transcript with JSON output is far more reliable than asking the live model
to call tools mid-conversation, especially in Hindi.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.dispositions import DISPOSITIONS

log = logging.getLogger(__name__)

SYSTEM = """You review a phone call transcript between an AI sales/support agent and a person, and classify the outcome.
Return ONLY a JSON object with these keys:
  "disposition": one of {dispositions}
  "temperature": "hot" | "warm" | "cold"   (how ready the person is to buy / proceed)
  "summary": 1-2 plain sentences in English about what happened and what the person wants
  "callback_at": ISO-8601 date-time (with the person's local UTC offset) ONLY if the person asked to be called at a specific time or day, else null
  "appointment_at": ISO-8601 date-time (with the person's local UTC offset) ONLY if a specific appointment/meeting/demo day AND time was agreed on the call, else null
  "language": "hi" | "en" | "hinglish" | "other"
  "intent": why the person called us - "enquiry" | "support" | "followup" | "other"   (see below)
  "caller_name": the name the person said for themselves, or null
  "interest": what they asked about or wanted (the product / service), at most 12 words, or null
Intent (why they called):
  enquiry   a NEW potential customer asking about what the company sells: products, services, prices, availability, a demo or a visit
  support   an EXISTING customer who needs help with something already bought or booked: a problem, complaint, order or delivery status, billing, repair, cancellation
  followup  the person is following up on an earlier conversation, quote, appointment or callback they already had with the company
  other     spam, someone selling to us, a wrong number, a job seeker, silence, or anything that is not a buying enquiry
  If unsure choose "other". Use "enquiry" only for a clear question about, or interest in, what the company sells.
Definitions:
  interested        wants the product/next step (demo, quote, visit, price details)
  callback          asked to be called again later / is busy right now
  not_interested    clearly declined, but did not ask to stop calling
  wrong_number      not the intended person / number does not belong to them
  dnc               asked not to be called again / complained about the call
  voicemail         an automated greeting, voicemail or answering machine
  human_requested   asked to speak to a person, executive or manager
  connected_other   a real conversation without a clear outcome
  appointment_booked  a specific appointment / demo / meeting / visit was agreed (set appointment_at)
  resolved          the person's question or issue was fully answered / fixed during the call
  confirmed         the person acknowledged a reminder, notification or update
  unresolved        the person had a question or issue that was NOT resolved and needs a human
Be conservative: use "interested" only for a genuine positive signal. The agent's own lines are not the person's intent."""


def format_transcript(turns: list[dict]) -> str:
    lines = []
    for t in turns or []:
        who = "AGENT" if t.get("role") == "assistant" else "PERSON"
        lines.append(f"{who}: {t.get('text', '').strip()}")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try: return json.loads(m.group(0))
            except Exception: return None
    return None


def parse_callback(value, now: datetime, tz: str) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    dt = dt.astimezone(timezone.utc)
    return dt if dt > now else None


def normalise(raw: Optional[dict], now: datetime, tz: str) -> dict:
    raw = raw or {}
    disp = raw.get("disposition")
    if disp not in DISPOSITIONS:
        disp = "connected_other"
    temp = raw.get("temperature") if raw.get("temperature") in ("hot", "warm", "cold") else None
    return {
        "disposition": disp, "temperature": temp,
        "summary": str(raw.get("summary") or "").strip()[:600],
        "callback_at": parse_callback(raw.get("callback_at"), now, tz),
        "appointment_at": parse_callback(raw.get("appointment_at"), now, tz),
        "language": raw.get("language") if raw.get("language") in ("hi", "en", "hinglish", "other") else None,
        "intent": raw.get("intent") if raw.get("intent") in ("enquiry", "support", "followup", "other") else None,
        "caller_name": (str(raw.get("caller_name")).strip()[:80] if raw.get("caller_name") and str(raw.get("caller_name")).strip().lower() not in ("null", "none", "unknown") else None),
        "interest": (str(raw.get("interest")).strip()[:140] if raw.get("interest") and str(raw.get("interest")).strip().lower() not in ("null", "none") else None),
    }


async def _chat_json(base_url: str, api_key: str, model: str, system: str, user: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=40.0) as c:
        r = await c.post(f"{base_url.rstrip('/')}/chat/completions",
                         headers={"Authorization": f"Bearer {api_key}"},
                         json={"model": model, "temperature": 0, "max_tokens": 400,
                               "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def llm_json(system: str, user: str) -> Optional[str]:
    s = get_settings()
    first = (s.ANALYSIS_PROVIDER or s.LLM_PROVIDER or "groq").strip().lower()
    order = ["groq", "deepseek"] if first == "groq" else ["deepseek", "groq"]
    for prov in order:
        try:
            if prov == "deepseek" and s.DEEPSEEK_API_KEY:
                return await _chat_json(s.DEEPSEEK_BASE_URL, s.DEEPSEEK_API_KEY, s.DEEPSEEK_MODEL, system, user)
            if prov == "groq" and s.GROQ_API_KEY:
                # A retired / mistyped model answers 404. The main GROQ_MODEL is tried next.
                for model in dict.fromkeys([s.analysis_model, s.GROQ_MODEL.strip()]):
                    try:
                        return await _chat_json("https://api.groq.com/openai/v1", s.GROQ_API_KEY, model, system, user)
                    except Exception as e:
                        log.warning("analysis via groq model %s failed: %s", model, e)
                continue
        except Exception as e:
            log.warning("analysis via %s failed: %s", prov, e)
    return None


async def analyze_call(*, transcript: list[dict], now: datetime, tz: str, llm=llm_json, purpose: str = "") -> dict:
    """`llm` is injectable for tests. Never raises: on any failure it returns a
    safe 'connected_other' so a human looks at it."""
    spoken = [t for t in transcript or [] if t.get("role") == "user" and (t.get("text") or "").strip()]
    if not spoken:
        return normalise({"disposition": "voicemail", "summary": "The call connected but the person never spoke."}, now, tz)
    from app import purposes
    hint = purposes.PURPOSES[purpose].focus if purpose in purposes.PURPOSES and purposes.PURPOSES[purpose].focus else ""
    user = (f"Current time: {now.astimezone(ZoneInfo(tz)).isoformat()} (person's timezone {tz})\n"
            + (f"Call purpose: {purposes.PURPOSES[purpose].label}. {hint}\n" if purpose in purposes.PURPOSES else "")
            + "\nTRANSCRIPT:\n" + format_transcript(transcript))
    try:
        raw = await llm(SYSTEM.format(dispositions=", ".join(DISPOSITIONS)), user)
    except Exception as e:  # defensive: analysis must never break finalisation
        log.warning("analysis crashed: %s", e)
        raw = None
    parsed = _extract_json(raw)
    if parsed is None:
        return normalise({"disposition": "connected_other",
                          "summary": "Call completed; automatic analysis was unavailable — please review the transcript."}, now, tz)
    return normalise(parsed, now, tz)
