"""Redis helpers: live call registry, per-batch work queues, locks."""
import json
from typing import Optional

import redis.asyncio as aioredis

from app.config import get_settings

_r = None


def get_redis():
    global _r
    if _r is None:
        _r = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    return _r


def set_redis(r):
    global _r
    _r = r


CALL_TTL = 3 * 3600


def k_queue(owner, batch): return f"vq:{owner}:{batch}"
def k_active_all(): return "active:calls"
def k_active_owner(owner): return f"active:o:{owner}"
def k_active_batch(owner, batch): return f"active:b:{owner}:{batch}"
def k_call(call_id): return f"call:{call_id}"
def k_lead_lock(owner, lead): return f"lock:lead:{owner}:{lead}"
def k_final(call_id): return f"final:{call_id}"


async def save_call_meta(r, call_id: str, meta: dict):
    await r.set(k_call(call_id), json.dumps(meta, default=str), ex=CALL_TTL)


async def load_call_meta(r, call_id: str) -> Optional[dict]:
    raw = await r.get(k_call(call_id))
    return json.loads(raw) if raw else None


async def patch_call_meta(r, call_id: str, **fields) -> Optional[dict]:
    m = await load_call_meta(r, call_id)
    if m is None:
        return None
    m.update(fields)
    await save_call_meta(r, call_id, m)
    return m


async def mark_active(r, owner, batch, call_id):
    await r.sadd(k_active_all(), call_id)
    await r.sadd(k_active_owner(owner), call_id)
    if batch:
        await r.sadd(k_active_batch(owner, batch), call_id)


async def mark_inactive(r, owner, batch, call_id):
    await r.srem(k_active_all(), call_id)
    await r.srem(k_active_owner(owner), call_id)
    if batch:
        await r.srem(k_active_batch(owner, batch), call_id)


async def inflight(r, owner) -> int:
    return await r.scard(k_active_owner(owner))


async def inflight_global(r) -> int:
    return await r.scard(k_active_all())


async def inflight_batch(r, owner, batch) -> int:
    return await r.scard(k_active_batch(owner, batch))


async def lock_lead(r, owner, lead, ttl=1800) -> bool:
    return bool(await r.set(k_lead_lock(owner, lead), "1", nx=True, ex=ttl))


async def unlock_lead(r, owner, lead):
    await r.delete(k_lead_lock(owner, lead))


async def rate_hit(r, key: str, limit: int, window_s: int) -> bool:
    """True if this call is within the limit (fixed window)."""
    n = await r.incr(key)
    if n == 1:
        await r.expire(key, window_s)
    return n <= limit
