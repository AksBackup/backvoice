"""
Tiny Firestore abstraction.

Production uses google-cloud-firestore's AsyncClient (via firebase-admin
credentials). Tests use MemoryFS. Both implement the same narrow surface so
the business logic never imports Firestore directly.

Sentinels (work in set/update data, including dotted-path keys in update()):
    INCREMENT(n)      atomic numeric increment
    ARRAY_UNION(*v)   atomic array union
    SERVER_TS         server timestamp
    DELETE_FIELD      remove the field
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class INCREMENT:
    n: float


@dataclass(frozen=True)
class ARRAY_UNION:
    values: tuple

    def __init__(self, *values):
        object.__setattr__(self, "values", tuple(values))


class _Sentinel:
    def __init__(self, name): self.name = name
    def __repr__(self): return self.name


SERVER_TS = _Sentinel("SERVER_TS")
DELETE_FIELD = _Sentinel("DELETE_FIELD")


class NotFound(Exception):
    pass


# ───────────────────────────── in-memory (tests / local) ─────────────────
class MemoryFS:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    # helpers
    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _apply(self, cur: dict, key: str, val: Any):
        # dotted path support
        parts = key.split(".")
        node = cur
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                node[p] = nxt
            node = nxt
        leaf = parts[-1]
        if isinstance(val, INCREMENT):
            node[leaf] = (node.get(leaf) or 0) + val.n
        elif isinstance(val, ARRAY_UNION):
            arr = list(node.get(leaf) or [])
            for v in val.values:
                if v not in arr:
                    arr.append(copy.deepcopy(v))
            node[leaf] = arr
        elif val is SERVER_TS:
            node[leaf] = self._now()
        elif val is DELETE_FIELD:
            node.pop(leaf, None)
        else:
            node[leaf] = copy.deepcopy(val)

    async def get(self, path: str) -> Optional[dict]:
        d = self.docs.get(path)
        return copy.deepcopy(d) if d is not None else None

    def _resolve(self, v):
        """Replace sentinels inside a value that has no prior state."""
        if isinstance(v, dict):
            return {k: self._resolve(x) for k, x in v.items() if x is not DELETE_FIELD}
        if isinstance(v, INCREMENT): return v.n
        if isinstance(v, ARRAY_UNION): return [copy.deepcopy(x) for x in v.values]
        if v is SERVER_TS: return self._now()
        return copy.deepcopy(v)

    def _deep_merge(self, cur: dict, key: str, val):
        if isinstance(val, dict) and isinstance(cur.get(key), dict):
            for k2, v2 in val.items():
                self._deep_merge(cur[key], k2, v2)
        elif isinstance(val, dict):
            cur[key] = self._resolve(val)
        else:
            self._apply(cur, key, val)

    async def set(self, path: str, data: dict, merge: bool = False):
        cur = copy.deepcopy(self.docs.get(path, {})) if merge else {}
        for k, v in data.items():
            if merge:
                if "." in k:
                    self._apply(cur, k, v)
                else:
                    self._deep_merge(cur, k, v)
            else:
                if v is not DELETE_FIELD:
                    cur[k] = self._resolve(v)
        self.docs[path] = cur

    async def update(self, path: str, data: dict):
        if path not in self.docs:
            raise NotFound(path)
        cur = self.docs[path]
        for k, v in data.items():
            self._apply(cur, k, v)

    async def delete(self, path: str):
        self.docs.pop(path, None)

    async def query(self, collection: str, where: Iterable[tuple] = (), limit: int = 500) -> list[tuple[str, dict]]:
        """Direct children of `collection` matching all (field, op, value)."""
        prefix = collection.rstrip("/") + "/"
        out = []
        for path, doc in self.docs.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if "/" in rest:
                continue
            if all(self._match(doc, f, op, v) for f, op, v in where):
                out.append((rest, copy.deepcopy(doc)))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _get_field(doc, field):
        node = doc
        for p in field.split("."):
            if not isinstance(node, dict) or p not in node:
                return None
            node = node[p]
        return node

    def _match(self, doc, field, op, value):
        x = self._get_field(doc, field)
        if op == "==": return x == value
        if op == "!=": return x != value
        if x is None: return False
        if op == "<": return x < value
        if op == "<=": return x <= value
        if op == ">": return x > value
        if op == ">=": return x >= value
        if op == "in": return x in value
        if op == "array_contains": return isinstance(x, list) and value in x
        raise ValueError(op)


# ───────────────────────────── real Firestore ────────────────────────────
class FirestoreFS:
    def __init__(self, client):
        from google.cloud import firestore  # noqa
        self._fs = firestore
        self.c = client

    def _conv(self, v):
        fs = self._fs
        if isinstance(v, INCREMENT): return fs.Increment(v.n)
        if isinstance(v, ARRAY_UNION): return fs.ArrayUnion(list(v.values))
        if v is SERVER_TS: return fs.SERVER_TIMESTAMP
        if v is DELETE_FIELD: return fs.DELETE_FIELD
        if isinstance(v, dict): return {k: self._conv(x) for k, x in v.items()}
        return v

    def _ref(self, path):
        return self.c.document(path)

    async def get(self, path):
        snap = await self._ref(path).get()
        return snap.to_dict() if snap.exists else None

    async def set(self, path, data, merge=False):
        await self._ref(path).set({k: self._conv(v) for k, v in data.items()}, merge=merge)

    async def update(self, path, data):
        await self._ref(path).update({k: self._conv(v) for k, v in data.items()})

    async def delete(self, path):
        await self._ref(path).delete()

    async def query(self, collection, where=(), limit=500):
        q = self.c.collection(collection)
        for f, op, v in where:
            q = q.where(f, op, v)
        q = q.limit(limit)
        return [(d.id, d.to_dict()) async for d in q.stream()]


_fs_singleton: Optional[object] = None


def get_fs():
    """Process-wide FS. Tests replace it with set_fs(MemoryFS())."""
    global _fs_singleton
    if _fs_singleton is None:
        _fs_singleton = _build_real_fs()
    return _fs_singleton


def set_fs(fs):
    global _fs_singleton
    _fs_singleton = fs


def _build_real_fs():
    import firebase_admin
    from firebase_admin import credentials
    from google.cloud import firestore
    from app.config import get_settings
    s = get_settings()
    if not firebase_admin._apps:
        if s.FIREBASE_PRIVATE_KEY and s.FIREBASE_CLIENT_EMAIL:
            cred = credentials.Certificate({
                "type": "service_account",
                "project_id": s.FIREBASE_PROJECT_ID,
                "client_email": s.FIREBASE_CLIENT_EMAIL,
                "private_key": s.FIREBASE_PRIVATE_KEY.replace("\\n", "\n"),
                "token_uri": "https://oauth2.googleapis.com/token",
            })
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred, {"projectId": s.FIREBASE_PROJECT_ID or None})
    app = firebase_admin.get_app()
    client = firestore.AsyncClient(project=app.project_id, credentials=app.credential.get_credential())
    return FirestoreFS(client)


# Path helpers — ONE place that knows the layout.
def P_user(owner): return f"users/{owner}"
def P_wallet(owner): return f"users/{owner}/voice/wallet"
def P_settings(owner): return f"users/{owner}/voice/settings"
def P_info(owner): return f"users/{owner}/voice/info"
def P_secrets(owner): return f"users/{owner}/voice_secrets/telephony"
def P_products(owner): return f"users/{owner}/voice_products"
def P_product(owner, pid): return f"users/{owner}/voice_products/{pid}"
def P_calls(owner): return f"users/{owner}/voice_calls"
def P_call(owner, cid): return f"users/{owner}/voice_calls/{cid}"
def P_leads(owner): return f"users/{owner}/leads"
def P_customers(owner): return f"users/{owner}/customers"
def P_lead(owner, lid): return f"users/{owner}/leads/{lid}"
def P_batch(owner, bid): return f"users/{owner}/lead_batches/{bid}"
def P_company(owner): return f"users/{owner}"
def P_phone_index(owner, norm): return f"users/{owner}/lead_phone_index/{norm}"
P_ACTIVE_BATCHES = "voice_active_batches"          # top-level registry (dialer scan)
def P_active_batch(owner, bid): return f"{P_ACTIVE_BATCHES}/{owner}__{bid}"
