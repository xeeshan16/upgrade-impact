from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from upgrade_impact.models import (
    Bump,
    Ecosystem,
    NotesSection,
    ReleaseNotes,
    Usage,
    UsageKind,
)
from upgrade_impact.providers.base import Completion, Message, ProviderError
from upgrade_impact.providers.fake import FakeChat, ScriptedChat
from upgrade_impact.recheck import (
    RECHECK_SCHEMA,
    build_prompt,
    code_names,
    line_identifiers,
    recheck_lines,
)

BUMP = Bump(
    ecosystem=Ecosystem.NPM,
    package="sinon",
    from_version="17.0.1",
    to_version="18.0.0",
    source="flags",
)
# Lines from sinon's 17.0.2 / 18.0.0 release notes.
NOTES = ReleaseNotes.from_sections(
    "sinon",
    [
        NotesSection(
            version="17.0.2",
            source="github-release",
            lines=[
                "- [`c57e38ae`](https://github.com/sinonjs/sinon/commit/c57e38ae)",
                "  Remove old template (Carl-Erik Kopseng)",
                "  Remove outdated info from README (#2571) (Carl-Erik Kopseng)",
                "  breaking: Remove sinon.defaultConfig and related modules (#2565)",
            ],
        ),
        NotesSection(
            version="18.0.0",
            source="github-release",
            lines=["  fix #2594: remove needless sandbox creation (Carl-Erik Kopseng)"],
        ),
    ],
)
USAGES = [
    Usage(
        path="test/a.spec.ts",
        line=3,
        column=1,
        symbol="sinon.stub",
        kind=UsageKind.CALL,
        snippet="sinon.stub(obj, 'x')",
    ),
    Usage(
        path="test/b.spec.ts",
        line=9,
        column=1,
        symbol="sinon.createSandbox",
        kind=UsageKind.CALL,
        keywords=["useFakeTimers"],
        snippet="sinon.createSandbox({ useFakeTimers: true })",
    ),
]


def _reply(*items: dict[str, object]) -> str:
    full = []
    for item in items:
        base: dict[str, object] = {
            "reason": "",
            "kind": "other",
            "summary": "",
            "symbols": [],
            "parameters": [],
        }
        full.append({**base, **item})
    return json.dumps({"lines": full})


def test_line_identifiers_and_code_names() -> None:
    assert line_identifiers(
        "breaking: Remove sinon.defaultConfig and related modules (#2565)", ["sinon"]
    ) == {"defaultConfig"}
    assert line_identifiers("Remove old template (Carl-Erik Kopseng)") == set()
    assert line_identifiers("The `use_fake_timers()` option") == {"use_fake_timers"}
    assert line_identifiers("see [docs](https://x.dev/fooBar)") == set()
    assert code_names(USAGES, ["sinon"]) == {"stub", "createSandbox", "useFakeTimers"}


def test_prompt_marks_context_lines_and_fences_the_notes() -> None:
    prompt = build_prompt(BUMP, NOTES, [2, 5])
    assert "SECOND READING: decide for N2, N5" in prompt
    assert "N1 (context): " in prompt
    assert "N2:   Remove old template" in prompt
    assert "N4" not in prompt  # N5's predecessor is in another version: not shown
    assert prompt.count("<<<NOTES ") == 1
    assert prompt.count("<<<END NOTES ") == 1


def test_true_becomes_a_claim_false_is_dismissed() -> None:
    chat = ScriptedChat(
        [
            _reply(
                {"line": 2, "quote": "Remove old template", "breaks_users": False,
                 "reason": "repository housekeeping"},
                {"line": 4, "quote": "Remove sinon.defaultConfig", "breaks_users": True,
                 "kind": "removed", "summary": "sinon.defaultConfig was removed",
                 "symbols": ["sinon.defaultConfig"]},
            )
        ]
    )  # fmt: skip
    out = recheck_lines(chat, BUMP, NOTES, [2, 4], used_names=code_names(USAGES, ["sinon"]))
    assert [d.line.n for d in out.dismissed] == [2]
    assert out.dismissed[0].by == "model"
    assert out.dismissed[0].reason == "repository housekeeping"
    assert len(out.claims) == 1
    assert out.claims[0].symbols == ["sinon.defaultConfig"]
    assert out.claims[0].evidence[0].line == 4
    assert chat.schemas == [RECHECK_SCHEMA]
    assert out.run is not None
    assert out.run.calls == 1


def test_a_line_naming_used_code_is_never_dismissed() -> None:
    notes = ReleaseNotes.from_sections(
        "sinon",
        [NotesSection(version="18.0.0", source="x", lines=["Removed `useFakeTimers` shorthand"])],
    )
    chat = ScriptedChat(
        [_reply({"line": 1, "quote": "Removed `useFakeTimers`", "breaks_users": False})]
    )
    out = recheck_lines(chat, BUMP, notes, [1], used_names=code_names(USAGES, ["sinon"]))
    assert out.dismissed == []
    assert any("`useFakeTimers`" in w and "stays unclaimed" in w for w in out.warnings)


def test_unverifiable_or_unasked_answers_change_nothing() -> None:
    chat = ScriptedChat(
        [
            _reply(
                {"line": 2, "quote": "something that is not on the line", "breaks_users": False},
                {"line": 3, "quote": "Remove outdated info", "breaks_users": False},
                {"line": 5, "quote": "remove needless sandbox", "breaks_users": "no"},
            )
        ]
    )
    out = recheck_lines(chat, BUMP, NOTES, [2, 5], used_names=set())
    assert out.dismissed == []
    assert out.claims == []


def test_protected_lines_are_not_dismissed() -> None:
    chat = ScriptedChat(
        [_reply({"line": 3, "quote": "Remove outdated info", "breaks_users": False})]
    )
    out = recheck_lines(chat, BUMP, NOTES, [3], used_names=set(), protected=[3])
    assert out.dismissed == []
    assert any("instruction to a model" in w for w in out.warnings)


class _Failing:
    provider = "failing"
    model = "x"

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion:
        raise ProviderError("boom")


def test_failures_leave_lines_unclaimed() -> None:
    out = recheck_lines(_Failing(), BUMP, NOTES, [2, 3], used_names=set())
    assert out.dismissed == []
    assert "second reading failed" in out.warnings[0]
    out = recheck_lines(ScriptedChat(["not json"]), BUMP, NOTES, [2], used_names=set())
    assert out.dismissed == []
    assert "unparseable" in out.warnings[0]


def test_lines_are_batched_and_capped() -> None:
    many = ReleaseNotes.from_sections(
        "sinon",
        [
            NotesSection(
                version="18.0.0", source="x", lines=[f"Removed thing {i}" for i in range(50)]
            )
        ],
    )
    chat = FakeChat()
    out = recheck_lines(chat, BUMP, many, list(range(1, 51)), used_names=set(), max_lines=45)
    assert out.run is not None
    assert out.run.calls == 3
    assert len(out.dismissed) == 45  # the fake rule: nothing backticked, nothing changed
    assert "covered 45 of 50" in out.warnings[0]
