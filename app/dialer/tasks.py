"""Celery tasks — thin wrappers. All logic lives in dialer/engine.py.

Live call audio never runs here (that is the FastAPI/Pipecat process); Celery
only schedules, dials, settles finished calls and reaps stuck ones.
"""
import asyncio
import dataclasses
import logging
import threading
from typing import Optional

from app.celery_app import celery
from app.dialer import engine
from app.fs import get_fs
from app.rds import get_redis
from app.telephony.base import HangupInfo

log = logging.getLogger(__name__)
_loop: Optional[asyncio.AbstractEventLoop] = None
_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """One persistent event loop per worker process, owned by a dedicated background thread.

    Why a thread: with --pool=threads / --pool=solo (the only options on Windows) several tasks can run at once.
    Calling run_until_complete() on a shared loop from two threads raises
    "RuntimeError: This event loop is already running". Instead every task submits its coroutine to this one
    loop with run_coroutine_threadsafe(), so the Firestore/Redis clients (bound to that loop) stay valid and
    concurrent tasks are simply interleaved safely.
    """
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="voice-celery-loop", daemon=True).start()
            _loop = loop
        return _loop


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop()).result()


@celery.task(name="voice.tick")
def tick_task():
    return run(engine.tick(get_fs(), get_redis()))


@celery.task(name="voice.reap")
def reap_task():
    return run(engine.reap_stuck(get_fs(), get_redis()))


@celery.task(bind=True, name="voice.finalize", max_retries=16)
def finalize_task(self, call_id: str, hangup: Optional[dict] = None, tries: int = 0):
    try:
        res = run(engine.finalize_when_ready(get_fs(), get_redis(), call_id, hangup, tries))
    except Exception as e:      # transient Firestore/Redis/analysis failure: try again instead of leaving the call "On call"
        log.exception("finalize failed for %s", call_id)
        raise self.retry(exc=e, countdown=10, kwargs={"call_id": call_id, "hangup": hangup, "tries": tries})
    if res == "wait":
        raise self.retry(countdown=2, kwargs={"call_id": call_id, "hangup": hangup, "tries": tries + 1})
    return res


def enqueue_finalize(call_id: str, info: Optional[HangupInfo], countdown: int = 3):
    finalize_task.apply_async(kwargs={"call_id": call_id, "hangup": dataclasses.asdict(info) if info else None, "tries": 0},
                              countdown=countdown)
