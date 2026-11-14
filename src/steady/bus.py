"""A tiny in-process publish/subscribe bus for asyncio.

Topics are plain strings ("window", "band.event", "band.status", ...). Each subscriber gets
its own bounded queue; a slow subscriber loses its oldest messages instead of blocking the
screening loop.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class Bus:
    def __init__(self, maxsize: int = 500) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._maxsize = maxsize

    def subscribe(self, *topics: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self._maxsize)
        for t in topics:
            self._subs[t].append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        for queues in self._subs.values():
            if q in queues:
                queues.remove(q)

    def publish(self, topic: str, payload: Any) -> None:
        for q in list(self._subs.get(topic, ())) + list(self._subs.get("*", ())):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait((topic, payload))
