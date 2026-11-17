"""Pre-render the greeting WHILE THE PHONE IS RINGING.

The caller hears the first words the instant they answer, instead of waiting
for the TTS socket to connect and synthesise. Falls back silently (the
pipeline just speaks the greeting normally) on any failure or if disabled.
"""
from __future__ import annotations

import base64
import io
import logging
import wave
from typing import Optional

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
RATE = 24000   # bulbul:v3's native rate — must equal the pipeline's TTS rate


def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected 16-bit mono WAV")
        return w.readframes(w.getnframes()), w.getframerate()


async def prerender_sarvam(text: str, *, voice: str, language_code: str = "hi-IN",
                           client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    s = get_settings()
    if not (s.PRERENDER_GREETING and s.SARVAM_API_KEY and text):
        return None
    own = client is None
    client = client or httpx.AsyncClient(timeout=8.0)
    try:
        r = await client.post(SARVAM_TTS_URL,
                              headers={"api-subscription-key": s.SARVAM_API_KEY, "Content-Type": "application/json"},
                              json={"text": text, "target_language_code": language_code, "speaker": voice,
                                    "model": "bulbul:v3", "speech_sample_rate": RATE, "enable_preprocessing": True})
        r.raise_for_status()
        audios = r.json().get("audios") or []
        if not audios:
            return None
        pcm, rate = wav_to_pcm(base64.b64decode(audios[0]))
        if rate != RATE:
            log.warning("greeting prerender: rate %s != %s, skipping", rate, RATE)
            return None
        return {"pcm_b64": base64.b64encode(pcm).decode(), "rate": rate}
    except Exception as e:
        log.warning("greeting prerender failed (will speak normally): %s", e)
        return None
    finally:
        if own:
            await client.aclose()
