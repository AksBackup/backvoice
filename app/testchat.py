"""Text-only test of the real agent prompt (no phone, no audio, no minutes)
so an admin can tune greeting / system prompt / product before launching."""
from typing import Optional

import httpx

from app import prompts
from app.config import get_settings
from app.dialer.engine import VoiceConfig


async def groq_chat(messages: list[dict]) -> str:
    s = get_settings()
    if not s.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not configured")
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                         headers={"Authorization": f"Bearer {s.GROQ_API_KEY}"},
                         json={"model": s.voice_model, "messages": messages, "temperature": 0.35, "max_tokens": 160})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


async def test_reply(cfg: VoiceConfig, product: Optional[dict], history: list[dict], message: str,
                     language: str, chat=groq_chat, purpose: Optional[str] = None, direction: str = "out") -> dict:
    lead = {"name": "Test Customer"}
    system = prompts.build_system_prompt(settings=cfg.settings, info=cfg.info, company_name=cfg.company_name,
                                         product=product, lead=lead, language=language,
                                         purpose=purpose, direction=direction)
    greeting = prompts.build_greeting(settings=cfg.settings, lead=lead, company_name=cfg.company_name,
                                      language=language, product=product, purpose=purpose, direction=direction)
    if not message.strip():
        return {"greeting": greeting, "reply": ""}
    faqs = (cfg.info or {}).get("faq") or []
    msgs = [{"role": "system", "content": prompts.with_turn_facts(system, faqs, message)},
            {"role": "assistant", "content": greeting}]
    for h in (history or [])[-10:]:
        if h.get("role") in ("user", "assistant") and str(h.get("content", "")).strip():
            msgs.append({"role": h["role"], "content": str(h["content"])[:600]})
    msgs.append({"role": "user", "content": message[:600]})
    return {"greeting": greeting, "reply": await chat(msgs)}
