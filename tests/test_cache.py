from __future__ import annotations

from pathlib import Path

from upgrade_impact.providers.base import Message
from upgrade_impact.providers.cache import CachingChat
from upgrade_impact.providers.fake import ScriptedChat


def test_same_request_is_answered_from_disk(tmp_path: Path) -> None:
    inner = ScriptedChat(['{"a": 1}', '{"b": 2}'])
    chat = CachingChat(inner, tmp_path / "llm")
    assert (chat.provider, chat.model) == ("scripted", "scripted")
    messages = [Message("system", "s"), Message("user", "u")]

    first = chat.complete(messages, json_mode=True, json_schema={"type": "object"})
    assert (first.text, first.cached) == ('{"a": 1}', False)
    again = CachingChat(inner, tmp_path / "llm").complete(
        messages, json_mode=True, json_schema={"type": "object"}
    )
    assert (again.text, again.cached, again.latency_s) == ('{"a": 1}', True, 0.0)
    assert again.usage.input_tokens == 10
    assert len(inner.requests) == 1

    # Anything that can change the answer changes the key.
    other = chat.complete(messages, json_mode=True, json_schema={"type": "array"})
    assert (other.text, other.cached) == ('{"b": 2}', False)
    assert (chat.hits, chat.misses) == (0, 2)


def test_sampling_is_not_cached_and_bad_entries_are_ignored(tmp_path: Path) -> None:
    inner = ScriptedChat(["one", "two", "three"])
    chat = CachingChat(inner, tmp_path / "llm")
    messages = [Message("user", "u")]
    assert chat.complete(messages, temperature=0.7).text == "one"
    assert chat.complete(messages, temperature=0.7).text == "two"
    assert not any((tmp_path / "llm").rglob("*.json"))

    key = chat.key(messages, max_tokens=1024, temperature=0.0, json_mode=False, json_schema=None)
    entry = tmp_path / "llm" / key[:2] / f"{key}.json"
    entry.parent.mkdir(parents=True)
    entry.write_text("{not json")
    assert chat.complete(messages).text == "three"
    assert chat.complete(messages).cached
