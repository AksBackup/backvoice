import asyncio
import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.dialer.engine import DialerError
from app.routes import api, webhooks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
s = get_settings()

app = FastAPI(title="Astric Voice", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in s.CORS_ORIGINS.split(",")],
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(api.router)
app.include_router(webhooks.router)


@app.exception_handler(DialerError)
async def dialer_error(_: Request, e: DialerError):
    # Same {error: "..."} shape as the Node backend, plus a stable code.
    return JSONResponse({"error": e.message, "code": e.code}, status_code=e.status)


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, e: StarletteHTTPException):
    # `error` matches the Node backend's shape; `detail` kept for FastAPI-style clients.
    return JSONResponse({"error": str(e.detail), "detail": e.detail}, status_code=e.status_code)


def _warm_pipecat():
    # 🐞 FIX (live call dropped: "WebSocketDisconnect ABNORMAL_CLOSURE" right after
    # Vobiz connects, nothing ever received): every pipecat import in app/voice/pipeline.py
    # is deliberately LAZY (deferred until the exact call path that needs it), so the
    # package's own heavy first-time import — numpy, torch (for VAD/smart-turn), the
    # provider SDKs — used to happen DURING the first live call's opening seconds,
    # right as Vobiz was waiting for this server to start reading the media stream.
    # On a slower machine that import alone can take several seconds, long enough for
    # Vobiz (or the tunnel) to give up and close the socket before a single byte of
    # audio was ever read — exactly what the logs showed (the "ᓚᘏᗢ Pipecat ..." import
    # banner printing mid-call, between "accepted" and the disconnect).
    #
    # Fix: trigger these same imports once, here, at server startup — they're cached
    # by Python afterwards (sys.modules), so the identical `from pipecat... import`
    # lines already in pipeline.py become instant no-ops on every real call. This
    # changes nothing about which providers get imported per-call; it only moves
    # the one-time cost earlier, off the critical path of the first call.
    t0 = time.monotonic()
    try:
        import pipecat.pipeline.runner        # noqa: F401  (pulls in the shared pipecat/torch/numpy machinery)
        import pipecat.pipeline.task           # noqa: F401
        import pipecat.audio.vad.silero        # noqa: F401  (the single heaviest one — torch)
        import pipecat.serializers.vobiz       # noqa: F401
        import pipecat.serializers.telnyx      # noqa: F401
        import pipecat.services.groq.llm       # noqa: F401
        import pipecat.services.sarvam.tts     # noqa: F401
        import pipecat.services.sarvam.stt     # noqa: F401
        logging.getLogger(__name__).info("pipecat warmed up in %.1fs", time.monotonic() - t0)
    except Exception:
        # Never let a warmup problem stop the server from starting — a real call will
        # just pay the lazy-import cost itself, same as before this fix existed.
        logging.getLogger(__name__).exception("pipecat warmup failed (non-fatal)")


@app.on_event("startup")
async def _startup():
    missing = [k for k in ("SECRETS_KEY", "CALL_TOKEN_SECRET", "GROQ_API_KEY", "DEEPGRAM_API_KEY", "SARVAM_API_KEY",
                           "PUBLIC_BASE_URL", "FIREBASE_PROJECT_ID") if not getattr(s, k)]
    if missing:
        logging.getLogger(__name__).warning("Missing configuration: %s", ", ".join(missing))
    # AWAITED, not fire-and-forget: uvicorn does not start accepting connections
    # until this startup event finishes (that's why "Application startup complete"
    # always prints before "Uvicorn running on http://...:8000" in its own logs —
    # you can see that exact order in your own terminal). So blocking here, rather
    # than running this in the background, means the warmup is GUARANTEED finished
    # the moment the server becomes reachable at all — nobody has to watch logs or
    # wait an extra few seconds after that; "Uvicorn running on..." already means
    # "ready for a call". Startup just takes a little longer to reach that line.
    await asyncio.get_event_loop().run_in_executor(None, _warm_pipecat)
