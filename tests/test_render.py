from __future__ import annotations

from upgrade_impact.models import (
    Bump,
    BumpReport,
    Claim,
    ClaimKind,
    ClaimResult,
    ClaimStatus,
    Ecosystem,
    Evidence,
    Match,
    MatchStrength,
    Report,
    Usage,
    UsageKind,
)
from upgrade_impact.render import MAX_ANNOTATIONS, render_annotations, render_markdown


def _bump_report(package: str, status: ClaimStatus, lines: int, notes_lines: int = 3) -> BumpReport:
    claim = Claim(
        id="C1",
        kind=ClaimKind.REMOVED,
        summary="`connect(timeout=)`: removed, 100% gone",
        symbols=[f"{package}.connect"],
        evidence=[Evidence(line=2, quote="removed `connect`", verified=True)],
        verified=True,
    )
    matches = [
        Match(
            usage=Usage(
                path=f"src/a,b:{i}.py",
                line=i + 1,
                column=0,
                symbol=f"{package}.connect",
                kind=UsageKind.CALL,
                snippet="connect()",
            ),
            strength=MatchStrength.DIRECT,
            reason="calls the removed function",
        )
        for i in range(lines)
    ]
    return BumpReport(
        bump=Bump(
            ecosystem=Ecosystem.PYPI, package=package, from_version="1", to_version="2", source="t"
        ),
        notes_lines=notes_lines,
        results=[ClaimResult(claim=claim, status=status, matches=matches)],
    )


def test_annotations_escape_and_cap() -> None:
    report = Report(
        source="s",
        repository_root="/r",
        bumps=[_bump_report("lib", ClaimStatus.AFFECTED, MAX_ANNOTATIONS + 3)],
    )
    lines = render_annotations(report).splitlines()
    warnings = [line for line in lines if line.startswith("::warning ")]
    assert len(warnings) == MAX_ANNOTATIONS
    # Property values escape ':' and ','; the message escapes '%' and newlines.
    assert warnings[0].startswith("::warning file=src/a%2Cb%3A0.py,line=1,title=lib 2%3A removed::")
    assert "100%25 gone" in warnings[0]
    assert "%0Acalls the removed function" in warnings[0]
    assert lines[-1].startswith("::notice title=upgrade-impact::3 more matched line(s)")


def test_no_annotations_without_matches() -> None:
    report = Report(
        source="s", repository_root="/r", bumps=[_bump_report("lib", ClaimStatus.NOT_FOUND, 0)]
    )
    assert render_annotations(report) == ""


def test_markdown_overview_for_several_bumps_lists_worst_first() -> None:
    report = Report(
        source="s",
        repository_root="/r",
        bumps=[
            _bump_report("calm", ClaimStatus.NOT_FOUND, 0),
            _bump_report("hot", ClaimStatus.AFFECTED, 2),
        ],
    )
    assert report.verdict == "affected"
    assert report.affected_lines() == 2
    text = render_markdown(report)
    assert text.startswith("**2 bumps: AFFECTED")
    rows = [line for line in text.splitlines() if line.startswith("| ") and "`" in line]
    assert rows[0].startswith("| affected | `hot` | 1 → 2 | 2 affected line(s) |")
    assert rows[1].startswith("| no-impact | `calm` |")
    # Sections follow the same order.
    assert text.index("### `hot`") < text.index("### `calm`")
