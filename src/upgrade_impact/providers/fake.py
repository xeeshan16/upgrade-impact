"""Deterministic providers for tests and CI. Never a substitute for a real model.

``FakeChat`` answers extraction prompts with a keyword heuristic: note lines
that say "removed", "renamed", "no longer", "breaking" and so on become claims,
with backticked identifiers as symbols. It answers adjudication prompts with a
name rule (see :func:`fake_decisions`), and second readings with a backtick
rule (see :func:`fake_recheck`). Every summary and reason starts with
``[fake provider]`` so its output can never be mistaken for a model's.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from upgrade_impact.models import TokenUsage
from upgrade_impact.providers.base import Completion, Message

NOTE_LINE = re.compile(r"^N(\d+): (.*)$", re.MULTILINE)
_KINDS = (
    (re.compile(r"\brenamed?\b", re.I), "renamed"),
    (
        re.compile(r"\b(removed?|dropped|no longer (available|exported|supported))\b", re.I),
        "removed",
    ),
    (
        re.compile(r"\b(drop(ped)? support|python 3\.\d+ is no longer|node(\.js)? \d+)", re.I),
        "dropped_support",
    ),
    (re.compile(r"\bdefault(s)?\b.*\b(now|changed)\b|\bnow defaults?\b", re.I), "default_changed"),
    (
        re.compile(r"\b(breaking|no longer|now (raises|returns|requires)|instead of)\b", re.I),
        "behavior_changed",
    ),
    (re.compile(r"\bdeprecat", re.I), "deprecated"),
)
_BACKTICKED = re.compile(r"`([^`]+)`")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_IDENT = re.compile(r"^[A-Za-z_][\w.]*(\(\))?$")
_PARAM = re.compile(r"`?(\w+)=?`?\s+(argument|parameter|option|kwarg|keyword)", re.I)


def _usage(messages: Sequence[Message], text: str) -> TokenUsage:
    return TokenUsage(
        input_tokens=sum(len(m.content) // 4 for m in messages), output_tokens=len(text) // 4
    )


def fake_claims(prompt: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for number, text in NOTE_LINE.findall(prompt):
        if _HEADING.match(text):
            continue
        kind = next((k for pattern, k in _KINDS if pattern.search(text)), None)
        if kind is None:
            continue
        ticked = [t.strip() for t in _BACKTICKED.findall(text)]
        params = [m.group(1) for m in _PARAM.finditer(text)]
        symbols = [t.removesuffix("()") for t in ticked if _IDENT.match(t) and t not in params]
        quote = text.strip().lstrip("-*• ").strip()[:80]
        claims.append(
            {
                "evidence": [{"line": int(number), "quote": quote}],
                "summary": f"[fake provider] {quote}",
                "kind": kind,
                "symbols": symbols,
                "parameters": params,
            }
        )
    return claims


ADJUDICATE_MARKER = "CANDIDATE CODE"  # mirrors upgrade_impact.adjudicate
_CANDIDATE = re.compile(
    r"^(K\d+): (\S+) line (\d+), resolved as (\S+) .*?\n<<<CODE \S+ \1>>>\n(.*?)\n<<<END CODE",
    re.MULTILINE | re.DOTALL,
)
_CODE_LINE = re.compile(r"^\s*(\d+)\| ?(.*)$")
_NAMES = re.compile(r"^Names involved: (.*)$", re.MULTILINE)


def fake_decisions(prompt: str) -> list[dict[str, Any]]:
    """Adjudicate with a rule: a candidate is affected when its symbol's last name
    appears on the first cited note line, and unclear otherwise. The rule never
    clears a candidate: telling ``Client(app=)`` from ``ASGITransport(app=)`` takes
    a model."""
    notes = NOTE_LINE.findall(prompt)
    note_n, note_text = (int(notes[0][0]), notes[0][1]) if notes else (0, "")
    names_match = _NAMES.search(prompt)
    params = [
        n.strip().rstrip("=")
        for n in (names_match.group(1).split(",") if names_match else [])
        if n.strip().endswith("=")
    ]
    decisions: list[dict[str, Any]] = []
    for key, _path, line, symbol, body in _CANDIDATE.findall(prompt):
        code = dict(
            (int(m.group(1)), m.group(2))
            for raw in body.splitlines()
            if (m := _CODE_LINE.match(raw))
        )
        text = code.get(int(line), "").strip()
        last = symbol.replace("/", ".").rsplit(".", 1)[-1]
        if re.search(rf"\b{re.escape(last)}\b", note_text):
            verdict, why = "affected", f"the note names {last}"
        elif params and any(f"{p}=" in text or f"{p}:" in text for p in params):
            verdict, why = "unclear", f"passes {', '.join(params)} but the note names no API"
        else:
            verdict, why = "unclear", "the rule cannot tell"
        decisions.append(
            {
                "candidate": key,
                "verdict": verdict,
                "code_line": int(line),
                "code_quote": text[:60],
                "note_line": note_n,
                "note_quote": note_text.strip().lstrip("-*• ")[:60],
                "notes_api": last if verdict == "affected" else "",
                "reason": f"[fake provider] {why}",
            }
        )
    return decisions


RECHECK_MARKER = "SECOND READING"  # mirrors upgrade_impact.recheck


def fake_recheck(prompt: str) -> list[dict[str, Any]]:
    """Second reading by rule: a listed line that backticks an identifier changes the
    API; one that names nothing in backticks does not."""
    answers: list[dict[str, Any]] = []
    for number, text in NOTE_LINE.findall(prompt):
        ticked = [t.strip().removesuffix("()") for t in _BACKTICKED.findall(text)]
        symbols = [t for t in ticked if _IDENT.match(t)]
        quote = text.strip().lstrip("-*• ").strip()[:60]
        breaks = bool(symbols)
        answers.append(
            {
                "line": int(number),
                "quote": quote,
                "breaks_users": breaks,
                "reason": "[fake provider] "
                + ("it names an identifier" if breaks else "it names no identifier"),
                "kind": "other",
                "summary": f"[fake provider] {quote}" if breaks else "",
                "symbols": symbols,
                "parameters": [],
            }
        )
    return answers


class FakeChat:
    """Keyword-heuristic stand-in for a model. Deterministic for a given prompt."""

    def __init__(self, model: str = "fake-extractor") -> None:
        self._model = model

    @property
    def provider(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion:
        prompt = messages[-1].content if messages else ""
        if RECHECK_MARKER in prompt:
            text = json.dumps({"lines": fake_recheck(prompt)})
            return Completion(text, self._model, _usage(messages, text), 0.0, "stop")
        if ADJUDICATE_MARKER in prompt:
            text = json.dumps({"decisions": fake_decisions(prompt)})
            return Completion(text, self._model, _usage(messages, text), 0.0, "stop")
        claims = fake_claims(prompt)
        flagged = sorted({ev["line"] for claim in claims for ev in claim["evidence"]})
        text = json.dumps({"breaking_lines": flagged, "claims": claims})
        return Completion(text, self._model, _usage(messages, text), 0.0, "stop")


class ScriptedChat:
    """Returns pre-written responses in order; records every request. For tests."""

    def __init__(self, responses: Iterable[str], model: str = "scripted") -> None:
        self._responses = deque(responses)
        self._model = model
        self.requests: list[list[Message]] = []
        self.schemas: list[Mapping[str, Any] | None] = []

    @property
    def provider(self) -> str:
        return "scripted"

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion:
        self.requests.append(list(messages))
        self.schemas.append(json_schema)
        if not self._responses:
            raise AssertionError("ScriptedChat ran out of responses")
        text = self._responses.popleft()
        return Completion(text, self._model, TokenUsage(input_tokens=10, output_tokens=5), 0.0)
