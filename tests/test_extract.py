from __future__ import annotations

import json
from pathlib import Path

from upgrade_impact.extract import (
    CLAIMS_SCHEMA,
    build_prompt,
    extract_claims,
    locate_quote,
    parse_claims,
    parse_reply,
    uncovered_lines,
    verify_claims,
    verify_quote,
)
from upgrade_impact.models import (
    Bump,
    Claim,
    ClaimKind,
    Ecosystem,
    Evidence,
    NotesSection,
    ReleaseNotes,
    TokenUsage,
)
from upgrade_impact.notes import changelog_sections
from upgrade_impact.providers.base import Completion, ProviderError
from upgrade_impact.providers.fake import FakeChat, ScriptedChat

BUMP = Bump(
    ecosystem=Ecosystem.PYPI,
    package="httpx",
    from_version="0.27.0",
    to_version="0.28.1",
    source="flags",
)


def httpx_notes(fixtures: Path) -> ReleaseNotes:
    text = (fixtures / "notes" / "httpx-CHANGELOG.md").read_text()
    sections = [
        NotesSection(version=v, source="changelog", lines=lines)
        for v, lines in changelog_sections(text, "0.27.0", "0.28.1")
    ]
    return ReleaseNotes.from_sections("httpx", sections)


def _line_of(notes: ReleaseNotes, needle: str) -> int:
    return next(line.n for line in notes.lines if needle in line.text)


def test_prompt_numbers_lines_and_fences_them(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)
    prompt, nonce = build_prompt(BUMP, notes, [line.n for line in notes.lines])
    assert f"<<<NOTES {nonce}>>>" in prompt
    assert f"<<<END NOTES {nonce}>>>" in prompt
    n = _line_of(notes, "`proxies` argument")
    assert f"N{n}: * The deprecated `proxies` argument has now been removed." in prompt
    assert "=== version 0.28.0 ===" in prompt
    assert "Upgrade: 0.27.0 -> 0.28.1" in prompt


def test_verify_quote(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)
    n = _line_of(notes, "`proxies` argument")
    assert verify_quote(notes, n, "The deprecated `proxies` argument has now been removed")
    assert verify_quote(notes, n, '"proxies" argument has now been removed.')  # markup-insensitive
    assert not verify_quote(notes, n, "the timeout argument was removed")
    assert not verify_quote(notes, n + 1, "proxies argument has now been removed")
    # A small model's habits: a line prefix in the quote, or citing a neighbouring line.
    assert locate_quote(notes, n, f"N{n}: * The deprecated `proxies` argument") == n
    assert locate_quote(notes, n + 1, "proxies argument has now been removed") == n
    assert locate_quote(notes, n + 5, "proxies argument has now been removed") is None
    assert not verify_quote(notes, 10_000, "anything")
    assert not verify_quote(notes, n, "the")  # too short to prove anything


def test_verify_claims_drops_fabrications_and_splits_symbols(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)
    n = _line_of(notes, "`proxies` argument")
    claims = [
        Claim(
            id="",
            kind=ClaimKind.REMOVED,
            summary="proxies removed",
            symbols=["httpx.Client(proxies=...)", "AsyncClient", "Transport.handle_proxy"],
            parameters=["proxies", "--proxies", "not an identifier"],
            evidence=[Evidence(line=n, quote="deprecated `proxies` argument has now been removed")],
        ),
        Claim(
            id="",
            kind=ClaimKind.REMOVED,
            summary="invented",
            symbols=["Client.stream"],
            evidence=[Evidence(line=n, quote="Client.stream was removed")],
        ),
        Claim(
            id="",
            kind=ClaimKind.REMOVED,
            summary="duplicate",
            symbols=["AsyncClient", "httpx.Client", "Transport.handle_proxy"],
            parameters=["proxies"],
            evidence=[Evidence(line=n, quote="proxies` argument has now been removed")],
        ),
    ]
    kept, dropped = verify_claims(notes, claims)
    assert [c.summary for c in dropped] == ["invented"]
    assert len(kept) == 1
    claim = kept[0]
    assert claim.id == "C1"
    assert claim.verified
    assert claim.version == "0.28.0"
    # "Client" and "AsyncClient" are not literally in the notes line, but "Client" appears
    # elsewhere in the 0.27.x/0.28.x notes? It does not: only symbols found anywhere survive.
    assert claim.parameters == ["proxies"]
    assert "Transport.handle_proxy" in claim.unverified_symbols
    assert set(claim.symbols) | set(claim.unverified_symbols) == {
        "httpx.Client",
        "AsyncClient",
        "Transport.handle_proxy",
    }


def test_parse_claims_is_lenient() -> None:
    reply = """Here you go:
```json
{"changes": [
  {"evidence": {"line": "N4", "quote": "x removed"}, "summary": "s", "kind": "Removed",
   "symbols": ["x"], "parameters": []},
  {"evidence": [{"line": 5, "quote": "y"}], "summary": "t", "kind": "weird kind"},
  "not an object"
]}
```"""
    claims, invalid = parse_claims(reply)
    assert invalid == 1
    assert [(c.kind, c.evidence[0].line) for c in claims] == [
        (ClaimKind.REMOVED, 4),
        (ClaimKind.OTHER, 5),
    ]
    assert parse_claims("[]") == ([], 0)


def test_extract_with_a_scripted_model_and_one_repair(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)
    n = _line_of(notes, "`app` argument")
    good = json.dumps(
        {
            "claims": [
                {
                    "evidence": [
                        {"line": n, "quote": "The deprecated `app` argument has now been removed."}
                    ],
                    "summary": "Client(app=...) is gone; use transport=ASGITransport(app=...).",
                    "kind": "removed",
                    "symbols": [],
                    "parameters": ["app"],
                }
            ]
        }
    )
    chat = ScriptedChat(["I think the answer is", good])
    result = extract_claims(chat, BUMP, notes, budget_tokens=5000)
    assert result.run.calls == 2
    assert len(chat.requests[1]) == 4  # system, user, bad assistant reply, repair request
    assert chat.schemas[0] == CLAIMS_SCHEMA
    assert [c.parameters for c in result.claims] == [["app"]]
    assert result.dropped == 0
    assert result.run.usage.total == 30


def test_extract_reports_unparseable_and_failed_calls(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)
    result = extract_claims(ScriptedChat(["nope", "still nope"]), BUMP, notes, budget_tokens=5000)
    assert result.claims == []
    assert any("unparseable" in w for w in result.warnings)

    class Failing(ScriptedChat):
        def complete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise ProviderError("connection refused")

    failed = extract_claims(Failing([]), BUMP, notes, budget_tokens=5000)
    assert failed.run.calls == 0
    assert any("connection refused" in w for w in failed.warnings)


def test_fake_provider_on_real_notes_is_labelled(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)
    result = extract_claims(FakeChat(), BUMP, notes, budget_tokens=300)
    assert result.run.calls >= 2  # small budget forces several chunks
    assert result.claims, "the fake provider should find the removal lines"
    assert all(c.summary.startswith("[fake provider]") for c in result.claims)
    removed = [c for c in result.claims if c.kind == ClaimKind.REMOVED]
    assert {tuple(c.parameters) for c in removed} >= {("proxies",), ("app",)}


def test_flagged_lines_without_claims_get_one_nudge(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)
    n = _line_of(notes, "`proxies` argument")
    lazy = json.dumps({"breaking_lines": [n, 99999], "claims": []})
    good = json.dumps(
        {
            "breaking_lines": [n],
            "claims": [
                {
                    "evidence": [{"line": n, "quote": "`proxies` argument has now been removed"}],
                    "summary": "proxies is gone",
                    "kind": "removed",
                    "symbols": [],
                    "parameters": ["proxies"],
                }
            ],
        }
    )
    chat = ScriptedChat([lazy, good])
    result = extract_claims(chat, BUMP, notes, budget_tokens=5000)
    assert result.run.calls == 2
    assert f"You listed N{n} as breaking changes" in chat.requests[1][-1].content
    assert [c.parameters for c in result.claims] == [["proxies"]]

    # Only one nudge: a model that keeps returning nothing is left alone.
    stubborn = ScriptedChat([lazy, lazy])
    assert extract_claims(stubborn, BUMP, notes, budget_tokens=5000).claims == []
    assert len(stubborn.requests) == 2

    # Nothing flagged and nothing claimed: no nudge.
    quiet = ScriptedChat([json.dumps({"breaking_lines": [], "claims": []})])
    assert extract_claims(quiet, BUMP, notes, budget_tokens=5000).run.calls == 1


def test_parse_reply_reads_flagged_lines() -> None:
    reply = parse_reply('{"breaking_lines": [3, "N7", "x"], "claims": []}')
    assert reply.breaking_lines == [3, 7]
    assert parse_reply('{"claims": []}').breaking_lines == []


def test_uncovered_lines_are_the_safety_net(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)
    proxies = _line_of(notes, "`proxies` argument")
    app = _line_of(notes, "`app` argument")
    everything = uncovered_lines(notes, [])
    assert {proxies, app} <= set(everything)
    covered = Claim(
        id="C1",
        kind=ClaimKind.REMOVED,
        summary="s",
        evidence=[Evidence(line=proxies, quote="q", verified=True)],
        verified=True,
    )
    remaining = uncovered_lines(notes, [covered])
    assert proxies not in remaining
    assert app in remaining


def test_parameters_named_as_symbols_are_moved_by_the_cited_text(fixtures: Path) -> None:
    """The exact shape qwen2.5:7b returned on the real-model run for httpx 0.28.0."""
    notes = httpx_notes(fixtures)
    app = _line_of(notes, "`app` argument")
    verify = _line_of(notes, "`verify` argument as a string")
    reply = json.dumps(
        {
            "breaking_lines": [app, verify],
            "claims": [
                {
                    "evidence": [
                        {
                            "line": app,
                            "quote": "* The deprecated `app` argument has now been removed.",
                        }
                    ],
                    "summary": "The `app` argument has been removed.",
                    "kind": "removed",
                    "symbols": ["app"],
                    "parameters": [],
                },
                {
                    "evidence": [{"line": verify, "quote": "The `verify` argument as a string"}],
                    "summary": "verify as a string is deprecated",
                    "kind": "deprecated",
                    "symbols": ["verify", "Client"],
                    "parameters": [],
                },
            ],
        }
    )
    result = extract_claims(ScriptedChat([reply]), BUMP, notes, budget_tokens=5000)
    by_kind = {c.kind: c for c in result.claims}
    assert (by_kind[ClaimKind.REMOVED].symbols, by_kind[ClaimKind.REMOVED].parameters) == (
        [],
        ["app"],
    )
    deprecated = by_kind[ClaimKind.DEPRECATED]
    assert deprecated.parameters == ["verify"]
    assert "verify" not in deprecated.symbols


def test_cut_off_replies_are_retried_in_halves(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)

    class Looping(ScriptedChat):
        """Runs out of tokens on long chunks, like qwen2.5:7b did on 28 lines."""

        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            self.requests.append(list(messages))
            lines = [m for m in messages[-1].content.splitlines() if m.startswith("N")]
            if len(lines) > 8:
                text = '{"breaking_lines": [1, 2, 3], "claims": [{"evidence": [{"line": 1, "quo'
                return Completion(
                    text, "loop", TokenUsage(input_tokens=1, output_tokens=9), 0.0, "length"
                )
            return Completion(
                '{"breaking_lines": [], "claims": []}', "loop", TokenUsage(), 0.0, "stop"
            )

    chat = Looping([])
    result = extract_claims(chat, BUMP, notes, budget_tokens=5000)
    sizes = [
        len([m for m in r[-1].content.splitlines() if m.startswith("N")]) for r in chat.requests
    ]
    assert sizes[0] == len(notes.lines)
    # Halves are split again until they fit; each line ends up in exactly one answered chunk.
    assert sum(size for size in sizes if size <= 8) == len(notes.lines)
    assert any("retried in two halves" in w for w in result.warnings)


def test_off_by_one_citations_are_repointed(fixtures: Path) -> None:
    notes = httpx_notes(fixtures)
    n = _line_of(notes, "`proxies` argument")
    claim = Claim(
        id="",
        kind=ClaimKind.REMOVED,
        summary="s",
        parameters=["proxies"],
        evidence=[Evidence(line=n - 1, quote="The deprecated `proxies` argument has now been")],
    )
    [kept], dropped = verify_claims(notes, [claim])
    assert dropped == []
    assert kept.evidence[0].line == n
