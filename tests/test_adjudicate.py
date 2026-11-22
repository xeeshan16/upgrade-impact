from __future__ import annotations

import json
from pathlib import Path

import pytest

from upgrade_impact.adjudicate import (
    ADJUDICATION_SCHEMA,
    SourceFiles,
    adjudicate,
    note_context,
    rank_usages,
    verify_code_quote,
)
from upgrade_impact.index.python import index_repository
from upgrade_impact.match import match_claims
from upgrade_impact.models import (
    Bump,
    Claim,
    ClaimKind,
    ClaimResult,
    ClaimStatus,
    Ecosystem,
    Evidence,
    NotesSection,
    ReleaseNotes,
    Usage,
    Verdict,
)
from upgrade_impact.providers.fake import FakeChat, ScriptedChat
from upgrade_impact.render import render_markdown, render_text

BUMP = Bump(
    ecosystem=Ecosystem.PYPI,
    package="httpx",
    from_version="0.27.2",
    to_version="0.28.0",
    source="flags",
)
NOTES = ReleaseNotes.from_sections(
    "httpx",
    [
        NotesSection(
            version="0.28.0",
            source="changelog",
            lines=[
                "The 0.28 release includes a limited set of deprecations.",
                "",
                "* The deprecated `proxies` argument has now been removed.",
                "* The deprecated `app` argument has now been removed.",
                "* JSON request bodies use a compact representation.",
            ],
        )
    ],
)
# The same change, worded the way it would need to be for a parameter-only claim
# to be decidable from the notes.
NAMED_NOTES = ReleaseNotes.from_sections(
    "httpx",
    [
        NotesSection(
            version="0.28.0",
            source="changelog",
            lines=[
                "The 0.28 release includes a limited set of deprecations.",
                "",
                "* The deprecated `proxies` argument has now been removed.",
                "* The deprecated `app` argument of `AsyncClient` has now been removed.",
                "* JSON request bodies use a compact representation.",
            ],
        )
    ],
)
CODE = """\
import httpx


def make_app_client(app):
    return httpx.AsyncClient(app=app, base_url="http://test")


def make_transport(app):
    return httpx.ASGITransport(app=app)


def post_json(url, payload):
    return httpx.post(url, json=payload)
"""


def app_claim() -> Claim:
    return Claim(
        id="C1",
        kind=ClaimKind.REMOVED,
        summary="The deprecated app argument has been removed.",
        parameters=["app"],
        evidence=[Evidence(line=4, quote="The deprecated `app` argument", verified=True)],
        verified=True,
        version="0.28.0",
    )


def _status(result: ClaimResult) -> ClaimStatus:
    """Read the status afresh (adjudication mutates results in place)."""
    return result.status


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "conftest.py").write_text(CODE)
    return tmp_path


def _results(repo: Path, claims: list[Claim]) -> tuple[list[ClaimResult], list[Usage]]:
    usages = index_repository(repo, ["httpx"]).usages
    return match_claims(claims, usages, ["httpx"]), usages


def _decision(
    key: str, verdict: str, code_line: int, code_quote: str, **extra: object
) -> dict[str, object]:
    return {
        "candidate": key,
        "verdict": verdict,
        "code_line": code_line,
        "code_quote": code_quote,
        "note_line": extra.get("note_line", 4),
        "note_quote": extra.get("note_quote", "The deprecated `app` argument"),
        "notes_api": extra.get("notes_api", ""),
        "reason": extra.get("reason", "because"),
    }


def test_model_separates_the_removed_argument_from_a_namesake(repo: Path) -> None:
    results, usages = _results(repo, [app_claim()])
    [result] = results
    assert result.status == ClaimStatus.POSSIBLE
    assert [m.usage.line for m in result.matches] == [5, 9]
    chat = ScriptedChat(
        [
            json.dumps(
                {
                    "decisions": [
                        _decision(
                            "K1",
                            "affected",
                            5,
                            "httpx.AsyncClient(app=app",
                            notes_api="AsyncClient",
                        ),
                        _decision(
                            "K2",
                            "not_affected",
                            9,
                            "httpx.ASGITransport(app=app)",
                            notes_api="AsyncClient",
                            reason="app= is the transport's own argument",
                        ),
                    ]
                }
            )
        ]
    )
    outcome = adjudicate(
        chat,
        BUMP,
        NAMED_NOTES,
        results,
        usages=usages,
        files=SourceFiles(repo),
        max_candidates=12,
    )
    assert _status(result) == ClaimStatus.AFFECTED
    assert [m.usage.line for m in result.matches] == [5]
    assert result.matches[0].adjudication is not None
    assert result.matches[0].adjudication.verified
    assert [m.usage.line for m in result.cleared] == [9]
    assert outcome.run.calls == 1
    assert outcome.warnings == []
    # The prompt fences notes and code, numbers code lines, and asks for the schema.
    prompt = chat.requests[0][-1].content
    assert "N4: * The deprecated `app` argument of `AsyncClient` has now been removed." in prompt
    assert "N3: * The deprecated `proxies` argument has now been removed." in prompt
    assert "    9|     return httpx.ASGITransport(app=app)" in prompt
    assert chat.schemas[0] == ADJUDICATION_SCHEMA

    text = render_text_for(result)
    assert "model: affected [code L5, notes N4]: because" in text
    assert "cleared conftest.py:9" in text
    assert "app= is the transport's own argument" in text


def test_parameter_only_affected_needs_the_api_named_in_the_notes(repo: Path) -> None:
    """The day 2 real-model failure: httpx's notes say "the deprecated `app` argument
    has now been removed" without naming Client, and qwen2.5:7b called
    ASGITransport(app=...) affected. Without the API named in the notes, a
    same-named parameter is not evidence."""
    results, usages = _results(repo, [app_claim()])
    [result] = results
    reply = json.dumps(
        {
            "decisions": [
                _decision("K1", "affected", 5, "httpx.AsyncClient(app=app"),
                _decision("K2", "affected", 9, "httpx.ASGITransport(app=app)", notes_api="app"),
            ]
        }
    )
    outcome = adjudicate(
        ScriptedChat([reply]), BUMP, NOTES, results, usages=usages, files=SourceFiles(repo)
    )
    assert _status(result) == ClaimStatus.POSSIBLE
    assert all(
        m.adjudication is not None and m.adjudication.effective == Verdict.UNCLEAR
        for m in result.matches
    )
    assert outcome.warnings == [
        "C1: 2 'affected' verdict(s) named no API from the notes for a parameter-only "
        "change and count as unclear"
    ]
    text = render_text_for(result)
    assert "the notes do not tie the change to the API this line calls (it named 'app')" in text

    # Named in the notes, but not the API this line calls: still unclear.
    results, usages = _results(repo, [app_claim()])
    reply = json.dumps(
        {
            "decisions": [
                _decision("K2", "affected", 9, "ASGITransport(app=app)", notes_api="AsyncClient"),
            ]
        }
    )
    adjudicate(
        ScriptedChat([reply]), BUMP, NAMED_NOTES, results, usages=usages, files=SourceFiles(repo)
    )
    assert results[0].status == ClaimStatus.POSSIBLE


def render_text_for(result: ClaimResult) -> str:
    from upgrade_impact.models import BumpReport, Report

    report = Report(
        source="x",
        repository_root=".",
        bumps=[BumpReport(bump=BUMP, notes_lines=5, results=[result])],
    )
    markdown = render_markdown(report)
    assert "cleared `conftest.py:9`" in markdown or not result.cleared
    return render_text(report)


def test_unverified_not_affected_never_clears_a_candidate(repo: Path) -> None:
    results, usages = _results(repo, [app_claim()])
    [result] = results
    chat = ScriptedChat(
        [
            json.dumps(
                {
                    "decisions": [
                        _decision("K1", "not_affected", 5, "client = something_else()"),
                        _decision("K2", "not_affected", 9, "ASGITransport(app=app)", note_line=1),
                    ]
                }
            )
        ]
    )
    outcome = adjudicate(chat, BUMP, NOTES, results, usages=usages, files=SourceFiles(repo))
    assert result.status == ClaimStatus.POSSIBLE
    assert result.cleared == []
    decisions = [m.adjudication for m in result.matches]
    assert all(d is not None and d.effective == Verdict.UNCLEAR for d in decisions)
    assert decisions[0] is not None
    assert not decisions[0].code_verified
    # N1 holds no such quote (and is not a line the claim was shown with).
    assert decisions[1] is not None
    assert decisions[1].code_verified
    assert not decisions[1].note_verified
    assert outcome.warnings == [
        "C1: 2 adjudication(s) cited lines that could not be verified and count as unclear"
    ]
    assert "its citations did not check out" in render_text_for(result)


def test_direct_matches_are_not_sent_to_the_model(repo: Path) -> None:
    claim = app_claim().model_copy(update={"symbols": ["AsyncClient"]})
    results, usages = _results(repo, [claim])
    [result] = results
    assert result.status == ClaimStatus.AFFECTED
    chat = ScriptedChat([])  # raises if called
    outcome = adjudicate(chat, BUMP, NOTES, results, usages=usages, files=SourceFiles(repo))
    assert outcome.run.calls == 0
    assert result.status == ClaimStatus.AFFECTED


def behaviour_claim() -> Claim:
    return Claim(
        id="C2",
        kind=ClaimKind.BEHAVIOR_CHANGED,
        summary="JSON request bodies are now compact.",
        evidence=[Evidence(line=5, quote="JSON request bodies use a compact", verified=True)],
        verified=True,
        version="0.28.0",
    )


def test_claims_without_symbols_review_ranked_calls(repo: Path) -> None:
    results, usages = _results(repo, [behaviour_claim()])
    [result] = results
    assert result.status == ClaimStatus.NEEDS_REVIEW
    ranked = rank_usages(result.claim, NOTES, usages)
    assert ranked[0].symbol == "httpx.post"  # passes json=, which the note mentions
    chat = ScriptedChat(
        [
            json.dumps(
                {
                    "decisions": [
                        _decision(
                            "K1",
                            "affected",
                            13,
                            "httpx.post(url, json=payload)",
                            note_line=5,
                            note_quote="JSON request bodies use a compact representation",
                        )
                    ]
                }
            )
        ]
    )
    adjudicate(chat, BUMP, NOTES, results, usages=usages, files=SourceFiles(repo))
    assert _status(result) == ClaimStatus.AFFECTED
    assert result.matches[0].usage.symbol == "httpx.post"


def test_claims_without_symbols_are_cleared_only_when_every_call_was_read(repo: Path) -> None:
    def all_clear(lines: list[tuple[int, str]]) -> str:
        return json.dumps(
            {
                "decisions": [
                    _decision(
                        f"K{i}",
                        "not_affected",
                        line,
                        quote,
                        note_line=5,
                        note_quote="JSON request bodies",
                    )
                    for i, (line, quote) in enumerate(lines, start=1)
                ]
            }
        )

    calls = [(13, "httpx.post"), (5, "httpx.AsyncClient("), (9, "httpx.ASGITransport(")]
    results, usages = _results(repo, [behaviour_claim()])
    adjudicate(
        ScriptedChat([all_clear(calls)]),
        BUMP,
        NOTES,
        results,
        usages=usages,
        files=SourceFiles(repo),
    )
    assert results[0].status == ClaimStatus.NOT_FOUND
    assert len(results[0].cleared) == 3

    results, usages = _results(repo, [behaviour_claim()])
    outcome = adjudicate(
        ScriptedChat([all_clear(calls[:2])]),
        BUMP,
        NOTES,
        results,
        usages=usages,
        files=SourceFiles(repo),
        max_candidates=2,
    )
    assert results[0].status == ClaimStatus.NEEDS_REVIEW
    assert "C2: the model reviewed 2 of 3 candidates" in outcome.warnings


def test_runtime_claims_and_bad_replies(repo: Path) -> None:
    runtime = behaviour_claim().model_copy(update={"kind": ClaimKind.DROPPED_SUPPORT})
    results, usages = _results(repo, [runtime, app_claim()])
    chat = ScriptedChat(["not json at all"])
    outcome = adjudicate(chat, BUMP, NOTES, results, usages=usages, files=SourceFiles(repo))
    assert outcome.run.calls == 1  # only the app claim; runtime support is not about lines
    assert outcome.warnings[0].startswith("C1: unparseable adjudication reply")
    by_id = {r.claim.id: r for r in results}
    assert by_id["C1"].status == ClaimStatus.POSSIBLE
    assert by_id["C2"].status == ClaimStatus.NEEDS_REVIEW


def test_batches_and_missing_files(repo: Path, tmp_path: Path) -> None:
    results, usages = _results(repo, [app_claim()])
    chat = ScriptedChat(
        [
            json.dumps(
                {
                    "decisions": [
                        _decision(
                            "K1", "affected", 5, "AsyncClient(app=app", notes_api="`AsyncClient`"
                        )
                    ]
                }
            ),
            json.dumps({"decisions": [_decision("K2", "unclear", 9, "ASGITransport")]}),
        ]
    )
    outcome = adjudicate(
        chat, BUMP, NAMED_NOTES, results, usages=usages, files=SourceFiles(repo), batch_size=1
    )
    assert outcome.run.calls == 2
    assert results[0].status == ClaimStatus.AFFECTED

    results, usages = _results(repo, [app_claim()])
    elsewhere = tmp_path / "empty"
    elsewhere.mkdir()
    outcome = adjudicate(
        ScriptedChat([]), BUMP, NOTES, results, usages=usages, files=SourceFiles(elsewhere)
    )
    assert outcome.warnings == ["C1: 2 candidate file(s) could not be read"]
    assert results[0].status == ClaimStatus.POSSIBLE


def test_fake_provider_rule_names_and_never_clears(repo: Path) -> None:
    claim = app_claim().model_copy(
        update={
            "evidence": [Evidence(line=4, quote="The deprecated `app` argument", verified=True)]
        }
    )
    results, usages = _results(repo, [claim])
    adjudicate(FakeChat(), BUMP, NOTES, results, usages=usages, files=SourceFiles(repo))
    [result] = results
    assert result.status == ClaimStatus.POSSIBLE
    assert result.cleared == []
    reasons = [m.adjudication.reason for m in result.matches if m.adjudication]
    assert reasons == ["[fake provider] passes app but the note names no API"] * 2


def test_helpers() -> None:
    window = [(4, "def f(app):"), (5, "    return httpx.AsyncClient(app=app)")]
    assert verify_code_quote(window, 5, "5| return httpx.AsyncClient(app=app)") == 5
    assert verify_code_quote(window, 4, "httpx.AsyncClient(app=app)") == 5  # off by one
    assert verify_code_quote(window, 5, "x") is None
    assert verify_code_quote(window, 9, "def f") is None
    assert note_context(NOTES, app_claim()) == [3, 4, 5]
    unverified = app_claim().model_copy(
        update={"evidence": [Evidence(line=4, quote="x", verified=False)]}
    )
    assert note_context(NOTES, unverified) == []


def test_review_budget_caps_model_calls_per_bump(repo: Path) -> None:
    results, usages = _results(repo, [app_claim(), behaviour_claim()])
    reply = json.dumps({"decisions": [_decision("K1", "unclear", 5, "AsyncClient(app=app")]})
    outcome = adjudicate(
        ScriptedChat([reply]),
        BUMP,
        NOTES,
        results,
        usages=usages,
        files=SourceFiles(repo),
        batch_size=1,
        max_calls=1,
    )
    assert outcome.run.calls == 1
    assert outcome.warnings == [
        "C1: review budget used up after 1 of 2 candidates",
        "review budget of 1 model call(s) used up; 1 claim(s) were not reviewed "
        "(raise UPGRADE_IMPACT_MAX_REVIEW_CALLS)",
    ]
    by_id = {r.claim.id: r for r in results}
    assert by_id["C1"].status == ClaimStatus.POSSIBLE
    assert by_id["C2"].status == ClaimStatus.NEEDS_REVIEW
