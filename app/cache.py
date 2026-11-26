"""只读工具的短期缓存。写操作不进缓存，写入成功后业务版本号递增使旧键失效。"""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

logger = logging.getLogger("digital_employee.cache")


class Cache(Protocol):
    def get(self, key: str) -> dict | None: ...

    def set(self, key: str, value: dict, ttl_seconds: int) -> None: ...


class MemoryCache:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.time():
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        self._items[key] = (time.time() + ttl_seconds, value)


class RedisCache:
    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> dict | None:
        raw = self._client.get(key)
        if raw is None:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        self._client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)


def build_cache(redis_url: str) -> Cache:
    if not redis_url:
        return MemoryCache()
    try:
        cache = RedisCache(redis_url)
        cache.set("de:ping", {"ok": True}, 5)
        if cache.get("de:ping") is None:
            raise RuntimeError("redis ping failed")
        return cache
    except Exception:
        logger.warning("Redis 不可用，只读缓存退回进程内存", exc_info=True)
        return MemoryCache()
