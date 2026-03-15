from __future__ import annotations

import abc
import asyncio
from typing import Any

from .schemas import BufferGateRequest, Settings


class BufferFull(Exception):
    """Raised when a buffer cannot accept more items."""


class RequestBuffer(abc.ABC):
    @abc.abstractmethod
    async def put(self, item: BufferGateRequest) -> None: ...

    @abc.abstractmethod
    async def get(self) -> BufferGateRequest: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class InMemoryBuffer(RequestBuffer):
    def __init__(self, maxsize: int) -> None:
        self._queue: asyncio.Queue[BufferGateRequest] = asyncio.Queue(maxsize=maxsize)

    async def put(self, item: BufferGateRequest) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            raise BufferFull("Queue full") from None

    async def get(self) -> BufferGateRequest:
        return await self._queue.get()

    async def close(self) -> None:
        pass


class RedisBuffer(RequestBuffer):
    """Buffer backed by a Redis list (LPUSH / BRPOP).

    Requires ``redis[hiredis]``: ``pip install pipegate[redis]``
    """

    def __init__(
        self,
        *,
        redis_url: str,
        connection_id: str,
        maxsize: int,
    ) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise ImportError(
                "redis package is required for the redis buffer backend. "
                "Install it with: pip install pipegate[redis]"
            ) from None

        self._redis: Any = aioredis.from_url(redis_url, decode_responses=True)
        self._key = f"pipegate:buffer:{connection_id}"
        self._maxsize = maxsize

    async def put(self, item: BufferGateRequest) -> None:
        if self._maxsize and await self._redis.llen(self._key) >= self._maxsize:
            raise BufferFull("Redis list at capacity")
        await self._redis.lpush(self._key, item.model_dump_json())

    async def get(self) -> BufferGateRequest:
        while True:
            result: list[str] | None = await self._redis.brpop(self._key, timeout=1)
            if result is not None:
                _key, payload = result
                return BufferGateRequest.model_validate_json(payload)

    async def close(self) -> None:
        await self._redis.delete(self._key)
        await self._redis.aclose()


def create_buffer(settings: Settings, connection_id: str) -> RequestBuffer:
    if settings.buffer_backend == "redis":
        return RedisBuffer(
            redis_url=settings.redis_url,
            connection_id=connection_id,
            maxsize=settings.max_queue_depth,
        )
    return InMemoryBuffer(maxsize=settings.max_queue_depth)
