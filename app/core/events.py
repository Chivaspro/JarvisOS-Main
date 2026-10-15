"""Small async event bus used to decouple UI, API, and agent services."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    """Publish named events to async subscribers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: Handler) -> None:
        if handler not in self._handlers[event_name]:
            self._handlers[event_name].append(handler)

    async def publish(self, event_name: str, payload: dict[str, Any]) -> None:
        handlers = tuple(self._handlers.get(event_name, ()))
        if handlers:
            await asyncio.gather(*(handler(payload) for handler in handlers))
