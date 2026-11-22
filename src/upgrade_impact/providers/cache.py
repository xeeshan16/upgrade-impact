"""An on-disk cache in front of any chat model.

Every call here runs at temperature 0 with a fixed prompt, so the same
request gives the same answer and re-running a check on the same bump (or
the eval harness on the same dataset) should cost nothing. The key covers
everything that can change the answer: provider, model, messages, output
limit, temperature and JSON schema. Entries are plain JSON files under
``<cache_dir>/llm/``; delete the directory to clear it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from upgrade_impact.models import TokenUsage
from upgrade_impact.providers.base import ChatModel, Completion, Message

log = logging.getLogger(__name__)

CACHE_VERSION = 1


class CachingChat:
    """Wraps a :class:`ChatModel`; hits come back with ``cached=True`` and zero latency."""

    def __init__(self, inner: ChatModel, directory: Path) -> None:
        self._inner = inner
        self._dir = directory
        self.hits = 0
        self.misses = 0

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    def key(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        json_schema: Mapping[str, Any] | None,
    ) -> str:
        payload = {
            "v": CACHE_VERSION,
            "provider": self.provider,
            "model": self.model,
            "messages": [[m.role, m.content] for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "json_mode": json_mode,
            "json_schema": json_schema,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()

    def _path(self, key: str) -> Path:
        return self._dir / key[:2] / f"{key}.json"

    def _load(self, key: str) -> Completion | None:
        path = self._path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Completion(
                text=str(data["text"]),
                model=str(data["model"]),
                usage=TokenUsage(**data["usage"]),
                latency_s=0.0,
                finish_reason=data.get("finish_reason"),
                cached=True,
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("ignoring unreadable cache entry %s: %s", path, exc)
            return None

    def _store(self, key: str, completion: Completion) -> None:
        path = self._path(key)
        data = {
            "text": completion.text,
            "model": completion.model,
            "usage": completion.usage.model_dump(),
            "finish_reason": completion.finish_reason,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a concurrent reader never sees half a file.
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            Path(tmp).replace(path)
        except OSError as exc:
            log.warning("could not write cache entry %s: %s", path, exc)

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion:
        key = self.key(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
            json_schema=json_schema,
        )
        if temperature == 0.0 and (hit := self._load(key)) is not None:
            self.hits += 1
            return hit
        self.misses += 1
        completion = self._inner.complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
            json_schema=json_schema,
        )
        if temperature == 0.0:
            self._store(key, completion)
        return completion
