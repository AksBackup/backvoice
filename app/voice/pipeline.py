"""One Pipecat pipeline for BOTH carriers.

Vobiz and Telnyx differ only in (a) how the first websocket message is parsed
and (b) which FrameSerializer wraps the audio. Everything else — STT, LLM,
TTS, interruptions, silence/max-duration handling, end-of-call — is here once.

Built from the same Pipecat 1.x API usage that ran on real Vobiz calls in the
earlier project (VAD/turn settings, Deepgram nova-3 `multi`, Sarvam bulbul:v3
at 24 kHz, Groq with no SDK retries and a spoken fallback line).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.config import get_settings
from app.prompts import with_turn_facts
from app.voice.session import CallSession, utcnow

log = logging.getLogger(__name__)

FAREWELL = {"hi": "ठीक है जी, धन्यवाद। आपका दिन शुभ हो।", "en": "Alright, thank you for your time. Have a great day."}
NUDGE = {"hi": "हेलो? क्या आप वहाँ हैं? मुझे आपकी आवाज़ नहीं आ रही।", "en": "Hello? Are you still there?"}
IDLE_FAREWELL = {"hi": "लगता है कोई जवाब नहीं मिल रहा, इसलिए मैं कॉल बंद कर रही हूँ। धन्यवाद।",
                 "en": "I'm not getting a response, so I'll disconnect now. Thank you."}
LLM_FALLBACK = {"hi": "जी, एक सेकंड प्लीज़।", "en": "Just a second please, I'm with you."}

# Unambiguous "please hang up / stop calling" requests, handled locally so the
# call ends deterministically without an LLM round trip.
_END_ASCII = ("hang up", "end the call", "end call", "disconnect", "call cut", "call kat", "call band", "call bandh",
              "phone cut", "phone rakh", "rakh do", "stop calling", "call mat karna", "mat karna call", "do not call", "don't call")
_END_DEVA = ("फोन रख", "फ़ोन रख", "कॉल काट", "कॉल बंद", "कॉल मत", "फोन मत करना", "फ़ोन मत करना", "मुझे कॉल मत")


def is_explicit_end(text: str) -> bool:
    t = " ".join((text or "").replace("’", "'").casefold().split())
    if not t:
        return False
    return any(p in t for p in _END_ASCII) or any(p in t for p in _END_DEVA)


# ── guard against the model hanging up on a caller who is still there ─────────────────────────────────────
# Seen in real calls: the person answered with "Hello?", the model replied with end_call("no response") and the
# call was cut without a single word being spoken. The model may only hang up early for a clear reason.
_NO_RESPONSE = re.compile(r"no[\s_-]*(response|answer|reply)|silen|unrespons|not\s+respond|no\s+one|nobody", re.I)
# Things people say on pick-up that are NOT an answer: a hello, "yes?", "who is this", "can you hear me".
_NOT_AN_ANSWER = re.compile(
    r"^(hello+|hallo|helo|hi+|hey|yes|yeah|yep|ya|haan|han|ha|ji|hmm+|hm+|ok|okay|speaking|who(\s+is|'?s)?\s+(this|it|calling|speaking)|"
    r"can\s+you\s+hear\s+me|hello\s+hello|हेलो|हैलो|हेल्लो|हाँ|हां|जी|कौन|कौन\s+बोल\s+रहा\s+है|कौन\s+बोल\s+रही\s+है|नमस्ते)[\s.,?!।]*$", re.I)


def end_call_allowed(user_texts: list[str], reason: str = "") -> bool:
    """May the AI hang up now? `user_texts` = what the person has said so far (in order).

    Blocks only the failure seen on real calls: the person said "Hello?" and the model cut the call with
    end_call("no response") without speaking. So:
      - never because of "no response / silence" (the silence watchdog owns that, and it nudges first);
      - never while the person has said nothing, or only greetings ("hello?", "yes?", "who is this");
      - once they have really answered ("No. I do not.", "call me later", "not interested") the model is trusted."""
    said = [t.strip() for t in (user_texts or []) if (t or "").strip()]
    if _NO_RESPONSE.search(reason or ""):
        return False
    return any(not _NOT_AN_ANSWER.match(t) for t in said)


def trim_turns(messages: list[dict], keep: int = 8) -> list[dict]:
    """The conversation turns to send to the LLM: no system message, no empty messages, and NO tool-call plumbing.

    Dropping the assistant message that carries `tool_calls` (it has no text) while keeping its `tool` result leaves an
    orphan tool message, which Groq rejects with 400 'Tools should have a name!' - and the caller then hears silence."""
    out = []
    for m in messages or []:
        if m.get("role") in ("system", "tool") or m.get("tool_calls") or m.get("function_call"):
            continue
        if not str(m.get("content") or "").strip():
            continue
        out.append(m)
    return out[-keep:]


@dataclass
class CallHooks:
    """Persistence/telephony callbacks, so the pipeline never imports Firestore."""
    on_answered: Callable[[CallSession], Awaitable[None]]
    on_progress: Callable[[CallSession], Awaitable[None]]
    on_ended: Callable[[CallSession], Awaitable[None]]
    hangup: Callable[[], Awaitable[bool]]


# ───────────────────────────── service builders ──────────────────────────
def build_stt(pack: dict):
    s = get_settings()
    lang = pack.get("language", "hi")
    if (pack.get("sttProvider") or "deepgram") == "sarvam":
        from pipecat.services.sarvam.stt import SarvamSTTService
        from pipecat.transcriptions.language import Language
        return SarvamSTTService(api_key=s.SARVAM_API_KEY,
                                settings=SarvamSTTService.Settings(language=Language.EN if lang == "en" else Language.HI))
    from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
    english = lang == "en"
    # nova-3 + language="multi" = Deepgram's Hindi<->English code-switching mode (see old project notes).
    return DeepgramSTTService(api_key=s.DEEPGRAM_API_KEY, live_options=LiveOptions(
        model="nova-3" if not english else "nova-2", language="multi" if not english else "en",
        interim_results=True, utterance_end_ms="1200", vad_events=True, endpointing=300))


def build_tts(pack: dict) -> tuple[Any, int]:
    s = get_settings()
    lang = pack.get("language", "hi")
    gender = pack.get("voiceGender", "female")
    voice = pack.get("ttsVoice") or ""
    english_engine = pack.get("englishTts", "deepgram")
    if lang == "en" and english_engine != "sarvam":
        from pipecat.services.deepgram.tts import DeepgramTTSService
        return DeepgramTTSService(api_key=s.DEEPGRAM_API_KEY, sample_rate=8000, settings=DeepgramTTSService.Settings(
            voice=voice or ("aura-2-luna-en" if gender == "female" else "aura-2-orion-en"))), 8000
    from pipecat.services.sarvam.tts import SarvamTTSService
    from pipecat.transcriptions.language import Language
    return SarvamTTSService(api_key=s.SARVAM_API_KEY, sample_rate=24000, settings=SarvamTTSService.Settings(
        voice=voice or ("priya" if gender == "female" else "rahul"), model="bulbul:v3",
        language=Language.EN if lang == "en" else Language.HI, temperature=0.7)), 24000


def build_llm(pack: dict):
    s = get_settings()
    from pipecat.services.groq.llm import GroqLLMService
    lang = pack.get("language", "hi")

    class VoiceGroq(GroqLLMService):
        def create_client(self, api_key=None, base_url=None, **kwargs):
            # No SDK retries: a 429 retry can leave a caller waiting 10-30 s.
            from openai import AsyncOpenAI
            import httpx
            return AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=5.0,
                               http_client=httpx.AsyncClient(limits=httpx.Limits(
                                   max_keepalive_connections=20, max_connections=50, keepalive_expiry=None)))

        async def _process_context(self, context):
            try:
                await super()._process_context(context)
            except Exception as exc:  # never leave a caller in silence
                log.warning("Voice LLM failed; speaking fallback: %s", exc)
                await self._push_llm_text(LLM_FALLBACK.get(lang, LLM_FALLBACK["hi"]))

    if (s.LLM_PROVIDER or "groq").strip().lower() != "groq":
        log.warning("LLM_PROVIDER=%s: live calls only run on groq, using groq (%s)", s.LLM_PROVIDER, s.voice_model)
    model = s.voice_model
    return VoiceGroq(api_key=s.GROQ_API_KEY, retry_on_timeout=False, settings=VoiceGroq.Settings(
        model=model, temperature=0.35, max_tokens=160,
        extra={"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}))


# ───────────────────────────── assembly ──────────────────────────────────
def assemble(*, websocket, serializer, in_rate: int, pack: dict, session: CallSession, hooks: CallHooks):
    """Builds (but does not run) the pipeline. Returns (task, runner, parts)."""
    from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, EndFrame, FunctionCallResultProperties, LLMContextFrame,
                                       LLMFullResponseEndFrame, LLMFullResponseStartFrame, MetricsFrame,
                                       OutputAudioRawFrame, TextFrame, TranscriptionFrame, TTSSpeakFrame)
    from pipecat.metrics.metrics import LLMUsageMetricsData, TTSUsageMetricsData
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
    from pipecat.turns.user_mute import MuteUntilFirstBotCompleteUserMuteStrategy
    from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    cfg = get_settings()
    lang = pack.get("language", "hi")
    faqs = pack.get("faqs") or []

    transport = FastAPIWebsocketTransport(websocket=websocket, params=FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer))
    stt = build_stt(pack)
    llm = build_llm(pack)
    tts, tts_rate = build_tts(pack)

    end_tool = FunctionSchema(name="end_call", properties={"reason": {"type": "string", "description": "short reason"}},
                              required=[], description="End the phone call. Use only AFTER saying goodbye, and only when the conversation is really over "
                                                   "(they declined, asked to stop, or the next step is agreed). NEVER use it because the person is quiet, "
                                                   "only said hello, or asked who is calling.")
    context = LLMContext(messages=[{"role": "system", "content": pack["system_prompt"]}],
                         tools=ToolsSchema(standard_tools=[end_tool]))
    aggregators = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(start_secs=0.3, confidence=0.5, min_volume=0.35)),
        # People say "Hello?" the instant they pick up. Without this that word interrupted the greeting before a
        # single sound was played, so the caller heard nothing. The first greeting always plays to the end.
        user_mute_strategies=[MuteUntilFirstBotCompleteUserMuteStrategy()],
        user_turn_strategies=UserTurnStrategies(stop=[TurnAnalyzerUserTurnStopStrategy(
            turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=1.0)))])))

    state = {"task": None, "greeted": False, "end_task": None, "watch": None, "flush": None}

    async def _finish(reason: str, farewell: Optional[str]):
        if session.ending:
            return
        session.ending = True
        session.end_reason = session.end_reason or reason
        try:
            if farewell:
                await state["task"].queue_frames([TTSSpeakFrame(text=farewell)])
            await asyncio.sleep(3.0 if farewell or reason == "agent_ended" else 0.2)   # let the goodbye play out
            await hooks.hangup()                        # real carrier hangup (EndFrame alone is NOT one)
            await state["task"].queue_frames([EndFrame()])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("end-of-call sequence failed")
            try: await hooks.hangup()
            except Exception: pass

    def _start_end(reason: str, farewell: Optional[str]):
        if state["end_task"] is None:
            state["end_task"] = asyncio.create_task(_finish(reason, farewell))

    async def _end_call_tool(params):
        reason = ""
        try: reason = str((params.arguments or {}).get("reason") or "")
        except Exception: pass
        user_texts = [t["text"] for t in session.transcript if t.get("role") == "user"]
        if not end_call_allowed(user_texts, reason):
            state["blocked_ends"] = state.get("blocked_ends", 0) + 1
            log.info("end_call refused (reason=%r, user_turns=%d) - the caller is still on the line", reason, len(user_texts))
            await params.result_callback(
                {"status": "not_ended", "instruction": "Do NOT end the call yet. The person is still on the line and has not answered you. Reply to what they just said "
                                                        "in one short sentence: if they only said hello, greet them back, say who you are and ask if it is a good time."},
                properties=FunctionCallResultProperties(run_llm=True))
            if state["blocked_ends"] >= 2 and pack.get("greeting"):      # the model keeps trying: say the opening line ourselves
                await task.queue_frames([TTSSpeakFrame(text=pack["greeting"])])
            return
        session.end_reason = session.end_reason or "agent_ended"
        await params.result_callback({"status": "ending"}, properties=FunctionCallResultProperties(run_llm=False))
        _start_end("agent_ended", None)

    llm.register_function("end_call", _end_call_tool)

    class CallControl(FrameProcessor):
        """After STT: records the caller's turns, ends the call on an explicit
        hang-up request, and swallows everything once the call is ending."""
        async def process_frame(self, frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptionFrame):
                text = (frame.text or "").strip()
                if session.ending:
                    return
                if text:
                    session.add_turn("user", text)
                    if is_explicit_end(text):
                        session.end_reason = "caller_requested_end"
                        _start_end("caller_requested_end", FAREWELL.get(lang, FAREWELL["hi"]))
                        return
            await self.push_frame(frame, direction)

    class AssistantTap(FrameProcessor):
        """After the LLM: assembles the agent's reply for the transcript and
        counts usage."""
        def __init__(self):
            super().__init__()
            self._buf: list[str] = []

        async def process_frame(self, frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMFullResponseStartFrame):
                self._buf = []
            elif isinstance(frame, TextFrame) and not isinstance(frame, TranscriptionFrame):
                self._buf.append(frame.text or "")
            elif isinstance(frame, LLMFullResponseEndFrame):
                session.add_turn("assistant", "".join(self._buf))
                self._buf = []
            elif isinstance(frame, MetricsFrame):
                for d in frame.data:
                    if isinstance(d, TTSUsageMetricsData):
                        session.tts_chars += int(d.value or 0)
                    elif isinstance(d, LLMUsageMetricsData):
                        v = d.value
                        session.llm_prompt_tokens += int(getattr(v, "prompt_tokens", 0) or 0)
                        session.llm_completion_tokens += int(getattr(v, "completion_tokens", 0) or 0)
            await self.push_frame(frame, direction)

    class BotSpeechWatch(FrameProcessor):
        """Knows when the bot starts/stops talking. This transport has no `on_bot_stopped_speaking` event (the old
        handler never fired, so the silence nudge/hang-up watchdog never ran) - the frames are the reliable signal."""
        async def process_frame(self, frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, BotStoppedSpeakingFrame):
                state["greeted"] = True
                session.touch()
            elif isinstance(frame, BotStartedSpeakingFrame):
                session.touch()
            await self.push_frame(frame, direction)

    class TurnContext(FrameProcessor):
        """Before each LLM call: keep the last few turns and inject only the
        FAQ facts relevant to the caller's latest sentence."""
        async def process_frame(self, frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame):
                msgs = frame.context.messages
                system = [m for m in msgs if m.get("role") == "system"][:1]
                turns = trim_turns(msgs)
                last_user = next((str(m.get("content")) for m in reversed(turns) if m.get("role") == "user"), "")
                if system:
                    system = [{**system[0], "content": with_turn_facts(system[0]["content"], faqs, last_user)}]
                frame.context.set_messages(system + turns)
            await self.push_frame(frame, direction)

    bot_watch = BotSpeechWatch()
    pipeline = Pipeline([transport.input(), stt, CallControl(), aggregators.user(), TurnContext(), llm,
                         AssistantTap(), tts, transport.output(), bot_watch, aggregators.assistant()])
    task = PipelineTask(pipeline, enable_rtvi=False, params=PipelineParams(
        audio_in_sample_rate=in_rate, audio_out_sample_rate=tts_rate, enable_metrics=True, enable_usage_metrics=True))
    state["task"] = task

    async def _watch():
        """Silence nudge/hang-up and the hard maximum call length."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if session.ending:
                    return
                if session.answered_at and session.connected_seconds() >= cfg.MAX_CALL_SECONDS:
                    session.end_reason = "max_duration"
                    _start_end("max_duration", FAREWELL.get(lang, FAREWELL["hi"]))
                    return
                if not state["greeted"]:
                    session.touch()
                    # The user is muted until the first greeting finishes. If it never plays (TTS down, bad key) end the
                    # call instead of leaving the person in dead silence.
                    if session.answered_at and session.connected_seconds() >= 20:
                        log.error("greeting never played for call %s - ending", session.call_id)
                        session.end_reason = "greeting_failed"
                        _start_end("greeting_failed", None)
                        return
                    continue
                idle = session.idle_seconds()
                if not session.nudged and idle >= cfg.SILENCE_NUDGE_SECONDS:
                    session.nudged = True
                    session.last_activity = __import__("time").monotonic()
                    await task.queue_frames([TTSSpeakFrame(text=NUDGE.get(lang, NUDGE["hi"]))])
                elif session.nudged and idle >= cfg.SILENCE_HANGUP_SECONDS:
                    session.end_reason = "caller_idle"
                    _start_end("caller_idle", IDLE_FAREWELL.get(lang, IDLE_FAREWELL["hi"]))
                    return
        except asyncio.CancelledError:
            pass

    async def _flush():
        """Throttled live-transcript persistence (powers the live monitor)."""
        try:
            while True:
                await asyncio.sleep(2.0)
                if session.dirty:
                    session.dirty = False
                    try: await hooks.on_progress(session)
                    except Exception: log.exception("progress flush failed")
        except asyncio.CancelledError:
            pass

    @transport.event_handler("on_client_connected")
    async def _connected(_t, _c):
        session.answered_at = utcnow()
        session.touch()
        try: await hooks.on_answered(session)
        except Exception: log.exception("on_answered failed")
        greeting = pack.get("greeting", "")
        spoke = False
        pre = pack.get("greetingAudio")
        if pre:
            try:
                await task.queue_frames([OutputAudioRawFrame(audio=base64.b64decode(pre["pcm_b64"]),
                                                             sample_rate=int(pre["rate"]), num_channels=1)])
                context.add_message({"role": "assistant", "content": greeting})
                spoke = True
            except Exception:
                log.exception("prerendered greeting failed; speaking normally")
        if not spoke and greeting:
            await task.queue_frames([TTSSpeakFrame(text=greeting)])
        session.add_turn("assistant", greeting)
        state["watch"] = asyncio.create_task(_watch())
        state["flush"] = asyncio.create_task(_flush())

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_t, _c):
        session.ended_at = utcnow()
        for k in ("watch", "flush"):
            if state[k]: state[k].cancel()
        try: await hooks.on_ended(session)
        except Exception: log.exception("on_ended failed")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    return task, runner, {"context": context, "state": state, "finish": _start_end, "end_call_tool": _end_call_tool, "bot_watch": bot_watch}


# ───────────────────────────── entry points ──────────────────────────────
async def run_vobiz(websocket, *, pack: dict, creds: dict, provider_call_id: str, session: CallSession, hooks: CallHooks):
    from pipecat.serializers.vobiz import VobizFrameSerializer, parse_vobiz_start
    parsed = await parse_vobiz_start(websocket)
    rate = parsed["sample_rate"] or 8000
    serializer = VobizFrameSerializer(
        stream_id=parsed["stream_id"], call_id=parsed["call_id"] or provider_call_id,
        auth_id=creds.get("auth_id", ""), auth_token=creds.get("auth_token", ""),
        params=VobizFrameSerializer.InputParams(vobiz_sample_rate=rate, encoding=parsed["encoding"] or "audio/x-mulaw",
                                                sample_rate=None, auto_hang_up=True))
    task, runner, _ = assemble(websocket=websocket, serializer=serializer, in_rate=rate, pack=pack, session=session, hooks=hooks)
    await runner.run(task)


async def run_telnyx(websocket, *, pack: dict, creds: dict, session: CallSession, hooks: CallHooks):
    from pipecat.runner.utils import parse_telephony_websocket
    from pipecat.serializers.telnyx import TelnyxFrameSerializer
    _, data = await parse_telephony_websocket(websocket)
    serializer = TelnyxFrameSerializer(
        stream_id=data["stream_id"], outbound_encoding=data["outbound_encoding"] or "PCMU", inbound_encoding="PCMU",
        call_control_id=data["call_id"], api_key=creds.get("api_key", ""),
        params=TelnyxFrameSerializer.InputParams(auto_hang_up=True))
    task, runner, _ = assemble(websocket=websocket, serializer=serializer, in_rate=8000, pack=pack, session=session, hooks=hooks)
    await runner.run(task)
