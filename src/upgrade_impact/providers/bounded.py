"""Cap how many model calls are in flight when packages are checked in parallel."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

from upgrade_impact.providers.base import ChatModel, Completion, Message


class BoundedChat:
    """Wraps a :class:`ChatModel` so at most ``limit`` calls run at once.

    Grouped PRs check their packages on worker threads. Without a cap, eight
    packages would send eight requests to a local Ollama that serves one at a
    time, and the queued ones would hit the request timeout while waiting.
    The wrapped model must tolerate ``limit`` concurrent calls (the HTTP
    providers do: they share one thread-safe ``httpx.Client``).
    """

    def __init__(self, inner: ChatModel, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._inner = inner
        self._slots = threading.BoundedSemaphore(limit)
        self.limit = limit

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion:
        with self._slots:
            return self._inner.complete(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                json_mode=json_mode,
                json_schema=json_schema,
            )
