import json
import os
import time
from collections import defaultdict

MAX_MESSAGES = 20


class InMemoryHistory:
    def __init__(self, max_messages: int = MAX_MESSAGES):
        self._store: dict[str, list] = defaultdict(list)
        self._max = max_messages

    def add(self, phone: str, role: str, content: str) -> None:
        self._store[phone].append({"role": role, "content": content})
        if len(self._store[phone]) > self._max:
            self._store[phone] = self._store[phone][-self._max:]

    def get(self, phone: str) -> list[dict]:
        return list(self._store[phone])


class RedisHistory:
    def __init__(self, redis_url: str, max_messages: int = MAX_MESSAGES, ttl_seconds: int = 7 * 24 * 3600):
        import redis
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._max = max_messages
        self._ttl = ttl_seconds

    def _key(self, phone: str) -> str:
        return f"history:{phone}"

    def add(self, phone: str, role: str, content: str) -> None:
        key = self._key(phone)
        msgs = self.get(phone)
        msgs.append({"role": role, "content": content})
        if len(msgs) > self._max:
            msgs = msgs[-self._max:]
        self._redis.setex(key, self._ttl, json.dumps(msgs))

    def get(self, phone: str) -> list[dict]:
        key = self._key(phone)
        raw = self._redis.get(key)
        if not raw:
            return []
        return json.loads(raw)


def get_history() -> InMemoryHistory | RedisHistory:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            h = RedisHistory(redis_url)
            h._redis.ping()
            return h
        except Exception:
            pass
    return InMemoryHistory()


# --- Conversation state ---
# Same storage mechanism as message history above (Redis with in-memory fallback),
# just a separate key namespace/shape: {"state": str, "context": dict, "updated_at": epoch}.
#
# Keyed by (tenant_id, phone), not phone alone (multi-tenant routing) — two
# different tenants could otherwise collide if the same phone number ever
# messaged both, resuming one tenant's conversation state inside another's.

DEFAULT_STATE = "IDLE"
SESSION_TIMEOUT_SECONDS = 30 * 60  # 30 min inactivity -> treat as IDLE on next message


class InMemorySessionStore:
    def __init__(self, timeout_seconds: int = SESSION_TIMEOUT_SECONDS):
        self._store: dict[tuple[int, str], dict] = {}
        self._timeout = timeout_seconds

    def get(self, tenant_id: int, phone: str) -> dict:
        session = self._store.get((tenant_id, phone))
        if session is None or (time.time() - session["updated_at"]) > self._timeout:
            return {"state": DEFAULT_STATE, "context": {}}
        return {"state": session["state"], "context": session["context"]}

    def set(self, tenant_id: int, phone: str, state: str, context: dict | None = None) -> None:
        self._store[(tenant_id, phone)] = {
            "state": state,
            "context": context or {},
            "updated_at": time.time(),
        }

    def reset(self, tenant_id: int, phone: str) -> None:
        self._store.pop((tenant_id, phone), None)


class RedisSessionStore:
    def __init__(self, redis_url: str, timeout_seconds: int = SESSION_TIMEOUT_SECONDS):
        import redis
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._timeout = timeout_seconds

    def _key(self, tenant_id: int, phone: str) -> str:
        return f"session:{tenant_id}:{phone}"

    def get(self, tenant_id: int, phone: str) -> dict:
        raw = self._redis.get(self._key(tenant_id, phone))
        if not raw:
            return {"state": DEFAULT_STATE, "context": {}}
        session = json.loads(raw)
        if (time.time() - session["updated_at"]) > self._timeout:
            return {"state": DEFAULT_STATE, "context": {}}
        return {"state": session["state"], "context": session["context"]}

    def set(self, tenant_id: int, phone: str, state: str, context: dict | None = None) -> None:
        session = {"state": state, "context": context or {}, "updated_at": time.time()}
        # Redis TTL is just a cleanup backstop (generous buffer over the soft timeout above,
        # which is what actually governs "reset to IDLE after 30 min").
        self._redis.setex(self._key(tenant_id, phone), self._timeout + 300, json.dumps(session))

    def reset(self, tenant_id: int, phone: str) -> None:
        self._redis.delete(self._key(tenant_id, phone))


def get_session_store() -> InMemorySessionStore | RedisSessionStore:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            s = RedisSessionStore(redis_url)
            s._redis.ping()
            return s
        except Exception:
            pass
    return InMemorySessionStore()
