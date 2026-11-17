"""Per-call state shared between the pipeline, the watchdogs and finalisation."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CallSession:
    call_id: str
    owner: str
    lead_id: str
    language: str = "hi"
    transcript: list = field(default_factory=list)       # [{role,text,at}]
    answered_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    end_reason: str = ""
    tts_chars: int = 0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    nudged: bool = False
    ending: bool = False
    dirty: bool = False

    def add_turn(self, role: str, text: str):
        text = (text or "").strip()
        if not text:
            return
        # collapse an immediate duplicate (STT often re-emits a final)
        if self.transcript and self.transcript[-1]["role"] == role and self.transcript[-1]["text"] == text:
            return
        self.transcript.append({"role": role, "text": text, "at": utcnow().isoformat()})
        self.dirty = True
        if role == "user":
            self.touch()

    def touch(self):
        self.last_activity = time.monotonic()
        self.nudged = False

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_activity

    def connected_seconds(self) -> float:
        if not self.answered_at:
            return 0.0
        end = self.ended_at or utcnow()
        return max(0.0, (end - self.answered_at).total_seconds())
