"""Render a report as plain text or Markdown (JSON is ``Report.model_dump_json``)."""

from __future__ import annotations

from upgrade_impact.models import (
    BumpReport,
    ClaimResult,
    ClaimStatus,
    Ecosystem,
    Match,
    MatchStrength,
    ModelRun,
    Report,
)

MAX_MATCHES_SHOWN = 8

_LANGUAGE = {Ecosystem.PYPI: "Python", Ecosystem.NPM: "JavaScript/TypeScript"}

_VERDICT_TEXT = {
    "affected": "AFFECTED: at least one breaking change reaches your code",
    "review": "REVIEW: possible impact; read the matched lines",
    "no-impact": "NO IMPACT FOUND: none of the breaking changes reach your code",
    "unknown": "UNKNOWN: no release notes found",
}


_INFERRED = " (cited line flagged by the model; it gave no quote)"


def _quote(result: ClaimResult) -> list[tuple[int, str, str | None, str]]:
    return [
        (ev.line, ev.quote, result.claim.version, _INFERRED if ev.inferred else "")
        for ev in result.claim.evidence
        if ev.verified
    ]


def _imports_only(result: ClaimResult) -> bool:
    return bool(result.matches) and all(m.strength == MatchStrength.IMPORT for m in result.matches)


def _imports_summary(result: ClaimResult) -> str:
    files = sorted({m.usage.path for m in result.matches})
    shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
    return f"imported in {shown}; {result.matches[0].reason}"


def _headline(report: BumpReport) -> str:
    if report.model is None:
        return "claims: not extracted (--no-llm)"
    total = len(report.results)
    parts = [
        f"{report.count(ClaimStatus.AFFECTED)} affected",
        f"{report.count(ClaimStatus.POSSIBLE)} possible",
        f"{report.count(ClaimStatus.NEEDS_REVIEW)} need review",
        f"{report.count(ClaimStatus.NOT_FOUND)} not used by your code",
    ]
    return f"{total} breaking change(s): " + ", ".join(parts)


def _run_line(label: str, run: ModelRun | None) -> str | None:
    if run is None:
        return None
    fake = " (fake provider: heuristic stand-in, not a model)" if run.provider == "fake" else ""
    cached = f" ({run.cached_calls} from cache)" if run.cached_calls else ""
    return (
        f"{label}: {run.provider} {run.model}{fake}, {run.calls} call(s){cached}, "
        f"{run.usage.input_tokens:,} in / {run.usage.output_tokens:,} out tokens, "
        f"{run.latency_s:.1f} s"
    )


def _model_line(report: BumpReport) -> str | None:
    return _run_line("model", report.model)


def _review_line(report: BumpReport) -> str | None:
    return _run_line("code review", report.adjudication)


def _decision(match: Match) -> str | None:
    decision = match.adjudication
    if decision is None:
        return None
    cites = []
    if decision.code_line is not None:
        cites.append(f"code L{decision.code_line}")
    if decision.note_line is not None:
        cites.append(f"notes N{decision.note_line}")
    where = f" [{', '.join(cites)}]" if cites else ""
    if not decision.verified:
        return (
            f"model said {decision.verdict.value}, but its citations did not check out, "
            f"so it counts as unclear: {decision.reason}"
        )
    return f"model: {decision.verdict.value}{where}: {decision.reason}"


def _text_result(result: ClaimResult) -> list[str]:
    claim = result.claim
    out = ["", f"[{result.status.value}] {claim.id} {claim.kind.value}: {claim.summary}"]
    out.extend(
        f'  notes N{n} ({version}): "{quote}"{note}' for n, quote, version, note in _quote(result)
    )
    if claim.symbols or claim.parameters:
        names = ", ".join([*claim.symbols, *(f"{p}=" for p in claim.parameters)])
        out.append(f"  symbols: {names}")
    if _imports_only(result):
        out.append(f"  {_imports_summary(result)}")
        return out
    for match in result.matches[:MAX_MATCHES_SHOWN]:
        usage = match.usage
        out.append(f"  {usage.path}:{usage.line}  {usage.snippet}  ({match.reason})")
        if decision := _decision(match):
            out.append(f"      {decision}")
    if len(result.matches) > MAX_MATCHES_SHOWN:
        out.append(f"  ... and {len(result.matches) - MAX_MATCHES_SHOWN} more")
    for match in result.cleared[:MAX_MATCHES_SHOWN]:
        usage = match.usage
        out.append(f"  cleared {usage.path}:{usage.line}  {usage.snippet}")
        if decision := _decision(match):
            out.append(f"      {decision}")
    if len(result.cleared) > MAX_MATCHES_SHOWN:
        out.append(f"  ... and {len(result.cleared) - MAX_MATCHES_SHOWN} more cleared")
    return out


def _text_uncovered(report: BumpReport) -> list[str]:
    if not report.uncovered:
        return []
    out = [
        "",
        f"[unclaimed] {len(report.uncovered)} note line(s) read like breaking changes, "
        "but no verified claim cites them; check these by hand:",
    ]
    out.extend(
        f"  N{note.n} ({note.version}): {note.text.strip()}"
        for note in report.uncovered[:MAX_MATCHES_SHOWN]
    )
    if len(report.uncovered) > MAX_MATCHES_SHOWN:
        out.append(f"  ... and {len(report.uncovered) - MAX_MATCHES_SHOWN} more")
    return out


def _text_header(report: BumpReport) -> list[str]:
    bump = report.bump
    notes = (
        f"release notes: {', '.join(report.notes_versions)} "
        f"from {', '.join(report.notes_sources)} ({report.notes_lines} lines)"
        if report.notes_lines
        else "release notes: none found"
    )
    idx = report.index
    out = [
        f"upgrade-impact: {bump.label}",
        notes,
        f"code: {idx.usages} usage(s) of {', '.join(idx.import_names) or bump.package} "
        f"in {idx.files_importing} file(s); "
        f"{idx.files_scanned} {_LANGUAGE[bump.ecosystem]} file(s) scanned",
        _VERDICT_TEXT[report.verdict],
        _headline(report),
    ]
    if report.claims_dropped:
        out.append(
            f"({report.claims_dropped} claim(s) dropped: their quotes were not found in the notes)"
        )
    return out


def render_text(report: Report) -> str:
    out: list[str] = []
    for bump_report in report.bumps:
        out.extend(_text_header(bump_report))
        for result in bump_report.results:
            out.extend(_text_result(result))
        out.extend(_text_uncovered(bump_report))
        lines = [line for line in (_model_line(bump_report), _review_line(bump_report)) if line]
        if lines:
            out.extend(["", *lines])
        out.extend(f"warning: {warning}" for warning in bump_report.warnings)
        out.append("")
    out.extend(f"note: {warning}" for warning in report.warnings)
    return "\n".join(out).rstrip() + "\n"


def render_markdown(report: Report) -> str:
    out: list[str] = []
    for bump_report in report.bumps:
        bump = bump_report.bump
        out.append(f"### `{bump.package}` {bump.from_version or '?'} → {bump.to_version}")
        out.append("")
        out.append(f"**{_VERDICT_TEXT[bump_report.verdict]}**. {_headline(bump_report)}.")
        out.append("")
        if bump_report.notes_lines:
            out.append(
                f"Release notes: {', '.join(bump_report.notes_versions)} "
                f"({', '.join(bump_report.notes_sources)}, {bump_report.notes_lines} lines). "
                f"Code: {bump_report.index.usages} usage(s) in "
                f"{bump_report.index.files_importing} file(s)."
            )
            out.append("")
        if bump_report.results:
            out.append("| status | change | notes | your code |")
            out.append("|---|---|---|---|")
            for result in bump_report.results:
                claim = result.claim
                cites = "<br>".join(f"N{line}: “{quote}”" for line, quote, _, _ in _quote(result))
                code = (
                    _imports_summary(result)
                    if _imports_only(result)
                    else "<br>".join(
                        [
                            *(
                                f"`{m.usage.path}:{m.usage.line}` {m.reason}"
                                + (f" — {d}" if (d := _decision(m)) else "")
                                for m in result.matches[:MAX_MATCHES_SHOWN]
                            ),
                            *(
                                f"cleared `{m.usage.path}:{m.usage.line}` — {_decision(m)}"
                                for m in result.cleared[:MAX_MATCHES_SHOWN]
                            ),
                        ]
                    )
                    or "—"
                )
                summary = claim.summary.replace("|", "\\|")
                out.append(
                    f"| {result.status.value} | {claim.kind.value}: {summary} | "
                    f"{cites.replace('|', chr(92) + '|')} | {code.replace('|', chr(92) + '|')} |"
                )
            out.append("")
        if bump_report.uncovered:
            out.append(
                "Note lines that read like breaking changes but no verified claim cites "
                "(check by hand):"
            )
            out.append("")
            for note in bump_report.uncovered[:MAX_MATCHES_SHOWN]:
                out.append(f"- N{note.n} ({note.version}): {note.text.strip()}")
            out.append("")
        for line in (_model_line(bump_report), _review_line(bump_report)):
            if line:
                out.append(f"<sub>{line}</sub>")
                out.append("")
        for warning in bump_report.warnings:
            out.append(f"> {warning}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
