from __future__ import annotations

from pathlib import Path

import pytest

from upgrade_impact.index.python import index_repository
from upgrade_impact.match import match_claims, symbol_matches
from upgrade_impact.models import Claim, ClaimKind, ClaimStatus, Evidence, MatchStrength


def claim(
    kind: ClaimKind, symbols: list[str] | None = None, parameters: list[str] | None = None
) -> Claim:
    return Claim(
        id="C1",
        kind=kind,
        summary="s",
        symbols=symbols or [],
        parameters=parameters or [],
        evidence=[Evidence(line=1, quote="q", verified=True)],
        verified=True,
    )


@pytest.mark.parametrize(
    ("claim_symbol", "usage_symbol", "expected"),
    [
        ("Client", "httpx.Client", True),
        ("httpx.Client", "httpx.Client", True),
        ("httpx._client.Client", "httpx.Client", True),
        ("Client", "httpx.AsyncClient", False),
        ("Client.get", "httpx.Client", False),
        ("get", "httpx.get", True),
        ("Client.get", "httpx.get", False),
        ("json_format.MessageToDict", "google.protobuf.json_format.MessageToDict", True),
        ("MessageToDict", "google.protobuf.json_format.MessageToDict", True),
        ("httpx", "httpx.Client", False),
    ],
)
def test_symbol_matches(claim_symbol: str, usage_symbol: str, expected: bool) -> None:
    names = ["httpx", "google.protobuf"]
    assert symbol_matches(claim_symbol, usage_symbol, names) is expected


def test_parameter_claims_need_the_keyword(sample_repo: Path) -> None:
    usages = index_repository(sample_repo, ["httpx"]).usages
    results = match_claims(
        [
            claim(ClaimKind.REMOVED, ["Client", "AsyncClient"], ["proxies"]),
            claim(ClaimKind.REMOVED, [], ["app"]),
            claim(ClaimKind.DEPRECATED, ["AsyncClient"], ["cert"]),
        ],
        usages,
        ["httpx"],
    )
    proxies, cert, app = results
    assert proxies.status == ClaimStatus.AFFECTED
    direct = [m for m in proxies.matches if m.strength == MatchStrength.DIRECT]
    assert [(m.usage.path, m.usage.line, m.reason) for m in direct] == [
        ("app/http.py", 9, "passes proxies")
    ]
    # AC(**options) might forward proxies; the transport= call and imports are not matches.
    assert any("**kwargs" in m.reason for m in proxies.matches)
    assert not any(
        m.usage.line == 8 and m.usage.path == "tests/test_app.py" for m in proxies.matches
    )

    assert cert.status == ClaimStatus.POSSIBLE  # only via **options
    # No call passes app=; only AC(**options) could forward it.
    assert app.status == ClaimStatus.POSSIBLE
    assert [m.usage.line for m in app.matches] == [13]


def test_parameter_claims_without_an_api_are_only_possible(sample_repo: Path) -> None:
    usages = index_repository(sample_repo, ["httpx"]).usages
    [result] = match_claims([claim(ClaimKind.REMOVED, [], ["timeout"])], usages, ["httpx"])
    assert result.status == ClaimStatus.POSSIBLE
    passing = [m for m in result.matches if "passes timeout" in m.reason]
    assert {(m.usage.symbol, m.usage.line) for m in passing} == {
        ("httpx.Client", 9),
        ("httpx.get", 23),
    }
    assert all(m.strength == MatchStrength.SYMBOL for m in result.matches)
    assert "do not say which API" in passing[0].reason


def test_symbol_claims_by_kind(sample_repo: Path) -> None:
    usages = index_repository(sample_repo, ["httpx"]).usages
    removed, behaviour, unused = match_claims(
        [
            claim(ClaimKind.REMOVED, ["HTTPStatusError"]),
            claim(ClaimKind.BEHAVIOR_CHANGED, ["Timeout"]),
            claim(ClaimKind.RENAMED, ["httpx.BasicAuth"]),
        ],
        usages,
        ["httpx"],
    )
    assert removed.status == ClaimStatus.AFFECTED
    assert {m.usage.line for m in removed.matches} == {5, 17}
    assert behaviour.status == ClaimStatus.POSSIBLE
    assert unused.status == ClaimStatus.NOT_FOUND


def test_claims_without_symbols_need_review_only_when_the_package_is_used(
    sample_repo: Path,
) -> None:
    usages = index_repository(sample_repo, ["httpx"]).usages
    [result] = match_claims([claim(ClaimKind.BEHAVIOR_CHANGED)], usages, ["httpx"])
    assert result.status == ClaimStatus.NEEDS_REVIEW
    assert {m.strength for m in result.matches} == {MatchStrength.IMPORT}
    [unused] = match_claims([claim(ClaimKind.DROPPED_SUPPORT)], [], ["httpx"])
    assert unused.status == ClaimStatus.NOT_FOUND


def test_results_are_ordered_by_severity(sample_repo: Path) -> None:
    usages = index_repository(sample_repo, ["httpx"]).usages
    results = match_claims(
        [
            claim(ClaimKind.REMOVED, ["Nope"]),
            claim(ClaimKind.BEHAVIOR_CHANGED),
            claim(ClaimKind.REMOVED, ["Client"]),
        ],
        usages,
        ["httpx"],
    )
    assert [r.status for r in results] == [
        ClaimStatus.AFFECTED,
        ClaimStatus.NEEDS_REVIEW,
        ClaimStatus.NOT_FOUND,
    ]


def test_method_claim_matches_instance_calls_and_says_how() -> None:
    from upgrade_impact.match import match_claims
    from upgrade_impact.models import Claim, ClaimKind, ClaimStatus, Usage, UsageKind

    claim = Claim(
        id="C1", kind=ClaimKind.REMOVED, summary="Client.stream removed", symbols=["Client.stream"]
    )
    usage = Usage(
        path="a.py",
        line=3,
        column=1,
        symbol="httpx.Client.stream",
        kind=UsageKind.CALL,
        snippet="c.stream('GET', u)",
        via="c = httpx.Client at line 2",
    )
    [result] = match_claims([claim], [usage], ["httpx"])
    assert result.status == ClaimStatus.AFFECTED
    assert result.matches[0].reason == "uses Client.stream; c = httpx.Client at line 2"
