"""Layered voice prompt.

  1. BASE rules        — platform-owned, not editable by tenants (safety, voice style)
  2. Company + info    — name (existing companyInfo), industry, location, hours, about
  3. Agent persona     — admin settings (name, gender, tone, custom instructions)
  4. Product           — the thing being sold on this batch
  5. Lead              — name, company, notes, last call summary, attempt number
  6. Turn facts        — FAQ answers relevant to the caller's latest sentence (dynamic)

Layers 1-5 are static for the whole call (provider prompt-caching friendly);
layer 6 is appended last and replaced each turn.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app import purposes

TURN_MARKER = "\n\nTURN-RELEVANT FACTS:\n"


def _clip(v: Any, n: int) -> str:
    t = str(v or "").strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


class _Safe(dict):
    def __missing__(self, key):  # unknown {placeholders} stay visible, never crash
        return "{" + key + "}"


def render_template(text: str, **vars) -> str:
    try:
        return (text or "").format_map(_Safe(**vars))
    except (ValueError, IndexError):
        return text or ""


def first_name(full: str) -> str:
    return (full or "").strip().split(" ")[0] if (full or "").strip() else ""


def default_greeting(language: str, gender: str) -> str:
    if language == "en":
        return "Hello {lead_name}, this is {agent_name} calling from {company_name}. Do you have a minute?"
    verb = "बोल रहा हूँ" if (gender or "female") == "male" else "बोल रही हूँ"
    return "नमस्ते {lead_name} जी, मैं {company_name} से {agent_name} " + verb + "। क्या आपके पास एक मिनट है?"


_MALE_HI = (("रही हूँ", "रहा हूँ"), ("सकती हूँ", "सकता हूँ"), ("चाहती हूँ", "चाहता हूँ"), ("सकती", "सकता"))


def _hi_for_gender(text: str, gender: str) -> str:
    """Hindi purpose templates are written with feminine first-person forms; flip them for a male voice."""
    if (gender or "female") != "male":
        return text
    for f, m in _MALE_HI:
        text = text.replace(f, m)
    return text


def build_greeting(*, settings: dict, lead: dict, company_name: str, language: str, product: Optional[dict],
                   purpose: Optional[str] = None, direction: str = purposes.OUT) -> str:
    """Opening line. Order of preference: the company's own greeting (separate
    one for inbound if set) -> the purpose's default opening -> generic default."""
    gender = settings.get("voiceGender", "female")
    custom = ((settings.get("inboundGreeting") if direction == purposes.IN else None) or settings.get("greeting") or "").strip()
    if custom:
        template = custom
    elif purpose or direction == purposes.IN:
        p = purposes.get(purpose, direction)
        template = p.open_en if language == "en" else _hi_for_gender(p.open_hi, gender)
    else:
        template = default_greeting(language, gender)
    name = first_name((lead or {}).get("name", ""))
    text = render_template(
        template,
        lead_name=name or ("ji" if language != "en" else "there"),
        agent_name=settings.get("agentName") or "Asha",
        company_name=company_name or "our company",
        product=(product or {}).get("name", ""),
    )
    return " ".join(text.split())


def _language_rule(language: str, script: str) -> str:
    if language == "en":
        return "Speak simple, clear English."
    s = ("Write Hindi words in Devanagari script and keep English words / brand names in Latin script."
         if script != "roman" else "Write Hindi in Roman script (Hinglish) and keep English words as they are.")
    return ("Speak natural Hinglish — the same Hindi/English mix the person uses; if they speak only English, "
            "reply in English. " + s)


BASE_RULES = """You are {agent} on a LIVE PHONE CALL for {company}. Everything you write is spoken aloud.
RULES:
1. Keep replies short and natural: usually one sentence, at most two. Ask only one question at a time.
2. No lists, markdown, emojis or stage directions. Say numbers, prices and dates the way they are spoken.
3. {language_rule}
4. Never invent prices, offers, features, availability or promises. If you do not know, say a colleague will confirm and offer a callback.
5. If asked, say you are an AI assistant calling on behalf of {company}. Never claim to be human.
6. If the person asks not to be called, says it is a wrong number, or is clearly not interested: acknowledge politely, do not pitch again, then use the end_call tool.
7. If the person asks for a human or manager: say an executive will call them soon, then use the end_call tool.
8. When the conversation is complete (next step agreed, callback time agreed, or they decline), say a brief goodbye and use the end_call tool.
9. The opening greeting has ALREADY been spoken. Do not introduce yourself again, EXCEPT when the person only says hello / hello? / can you hear me / who is this: then answer warmly, say in one short sentence who you are and why you called, and ask if it is a good time.
10. NEVER use end_call because the person is quiet, said only hello, or has not answered yet. Always speak a reply first. Only end the call when it is truly over (rules 6-8).
{gender_note}"""


def _product_block(p: Optional[dict]) -> str:
    if not p:
        return "CALL GOAL: have a friendly conversation, understand what the person needs and offer to have someone follow up."
    feats = p.get("features") or []
    if not isinstance(feats, list):
        feats = [str(feats)]
    obj = p.get("objections") or []
    lines = [f"PRODUCT / SERVICE BEING DISCUSSED: {_clip(p.get('name'), 80)}"]
    for label, key, n in (("About", "description", 400), ("Price / offer", "price", 160),
                          ("Call goal", "goal", 200), ("Pitch", "pitch", 400)):
        if p.get(key):
            lines.append(f"{label}: {_clip(p[key], n)}")
    if feats:
        lines.append("Key points: " + "; ".join(_clip(f, 80) for f in feats[:6] if str(f).strip()))
    for o in obj[:5]:
        if isinstance(o, dict) and o.get("q") and o.get("a"):
            lines.append(f"If they say \"{_clip(o['q'], 80)}\": {_clip(o['a'], 200)}")
    return "\n".join(lines)


def build_system_prompt(*, settings: dict, info: dict, company_name: str, product: Optional[dict],
                        lead: dict, language: str, attempt: int = 1,
                        purpose: Optional[str] = None, direction: str = purposes.OUT) -> str:
    agent = settings.get("agentName") or "Asha"
    gender = settings.get("voiceGender", "female")
    gender_note = ""
    if language != "en":
        gender_note = ("Male voice: use masculine first-person Hindi forms." if gender == "male"
                       else "Female voice: use feminine first-person Hindi forms.")
    parts = [BASE_RULES.format(agent=agent, company=company_name or "the company",
                               language_rule=_language_rule(language, settings.get("hindiScript", "devanagari")),
                               gender_note=gender_note)]

    co = [f"COMPANY: {company_name}"] if company_name else []
    for label, key, n in (("Industry", "industry", 80), ("Location", "location", 120),
                          ("Working hours", "hours", 100), ("About", "about", 500)):
        if (info or {}).get(key):
            co.append(f"{label}: {_clip(info[key], n)}")
    if co:
        parts.append("\n".join(co))

    if settings.get("tone"):
        parts.append(f"TONE: {_clip(settings['tone'], 120)}")
    if settings.get("systemPrompt"):
        parts.append("ADDITIONAL INSTRUCTIONS FROM THE COMPANY:\n" + _clip(settings["systemPrompt"], 2500))
    parts.append(_product_block(product))
    if purpose or direction == purposes.IN:
        parts.append(purposes.prompt_block(purpose, direction))

    lead_lines = []
    if lead:
        if lead.get("name"): lead_lines.append(f"Name: {_clip(lead['name'], 60)}")
        if lead.get("company"): lead_lines.append(f"Their company: {_clip(lead['company'], 60)}")
        if lead.get("notes"): lead_lines.append(f"Notes: {_clip(lead['notes'], 300)}")
        prev = ((lead.get("voice") or {}).get("lastSummary"))
        if prev: lead_lines.append(f"Summary of the previous AI call: {_clip(prev, 300)}")
    if attempt > 1 and direction != purposes.IN:
        lead_lines.append(f"This is call attempt number {attempt}; do not repeat what was already covered.")
    if lead_lines:
        parts.append("ABOUT THE PERSON YOU ARE CALLING:\n" + "\n".join(lead_lines))
    return "\n\n".join(parts)


_WORD = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)


def select_faqs(faqs: list, text: str, k: int = 3) -> list[dict]:
    """The k FAQ entries sharing the most words with the caller's last
    sentence (cheap, no embeddings, works for Devanagari and Latin)."""
    toks = {w.casefold() for w in _WORD.findall(text or "") if len(w) > 2}
    if not toks:
        return []
    scored = []
    for f in faqs or []:
        if not isinstance(f, dict) or not f.get("q") or not f.get("a"):
            continue
        ft = {w.casefold() for w in _WORD.findall(f["q"] + " " + f["a"]) if len(w) > 2}
        score = len(toks & ft)
        if score:
            scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored[:k]]


def with_turn_facts(system_prompt: str, faqs: list, last_user_text: str) -> str:
    base = system_prompt.split(TURN_MARKER, 1)[0]
    picked = select_faqs(faqs, last_user_text)
    if not picked:
        return base
    return base + TURN_MARKER + "\n".join(f"Q: {_clip(f['q'], 120)}\nA: {_clip(f['a'], 300)}" for f in picked)
