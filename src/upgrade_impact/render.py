"""Render a report as plain text or Markdown (JSON is ``Report.model_dump_json``)."""

from __future__ import annotations

from upgrade_impact.models import BumpReport, ClaimResult, ClaimStatus, MatchStrength, Report

MAX_MATCHES_SHOWN = 8

_VERDICT_TEXT = {
    "affected": "AFFECTED: at least one breaking change reaches your code",
    "review": "REVIEW: possible impact; read the matched lines",
    "no-impact": "NO IMPACT FOUND: none of the breaking changes reach your code",
    "unknown": "UNKNOWN: no release notes found",
}


def _quote(result: ClaimResult) -> list[tuple[int, str, str | None]]:
    return [
        (ev.line, ev.quote, result.claim.version) for ev in result.claim.evidence if ev.verified
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


def _model_line(report: BumpReport) -> str | None:
    run = report.model
    if run is None:
        return None
    fake = " (fake provider: heuristic stand-in, not a model)" if run.provider == "fake" else ""
    return (
        f"model: {run.provider} {run.model}{fake}, {run.calls} call(s), "
        f"{run.usage.input_tokens:,} in / {run.usage.output_tokens:,} out tokens, "
        f"{run.latency_s:.1f} s"
    )


def render_text(report: Report) -> str:
    out: list[str] = []
    for bump_report in report.bumps:
        bump = bump_report.bump
        out.append(f"upgrade-impact: {bump.label}")
        notes = (
            f"release notes: {', '.join(bump_report.notes_versions)} "
            f"from {', '.join(bump_report.notes_sources)} ({bump_report.notes_lines} lines)"
            if bump_report.notes_lines
            else "release notes: none found"
        )
        out.append(notes)
        idx = bump_report.index
        out.append(
            f"code: {idx.usages} usage(s) of {', '.join(idx.import_names) or bump.package} "
            f"in {idx.files_importing} file(s); {idx.files_scanned} Python file(s) scanned"
        )
        out.append(_VERDICT_TEXT[bump_report.verdict])
        out.append(_headline(bump_report))
        if bump_report.claims_dropped:
            out.append(
                f"({bump_report.claims_dropped} claim(s) dropped: their quotes were not found "
                "in the notes)"
            )
        for result in bump_report.results:
            claim = result.claim
            out.append("")
            out.append(f"[{result.status.value}] {claim.id} {claim.kind.value}: {claim.summary}")
            for line, quote, version in _quote(result):
                out.append(f'  notes N{line} ({version}): "{quote}"')
            if claim.symbols or claim.parameters:
                names = ", ".join([*claim.symbols, *(f"{p}=" for p in claim.parameters)])
                out.append(f"  symbols: {names}")
            if _imports_only(result):
                out.append(f"  {_imports_summary(result)}")
                continue
            for match in result.matches[:MAX_MATCHES_SHOWN]:
                usage = match.usage
                out.append(f"  {usage.path}:{usage.line}  {usage.snippet}  ({match.reason})")
            if len(result.matches) > MAX_MATCHES_SHOWN:
                out.append(f"  ... and {len(result.matches) - MAX_MATCHES_SHOWN} more")
        model = _model_line(bump_report)
        if model:
            out.append("")
            out.append(model)
        for warning in bump_report.warnings:
            out.append(f"warning: {warning}")
        out.append("")
    for warning in report.warnings:
        out.append(f"note: {warning}")
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
                cites = "<br>".join(f"N{line}: “{quote}”" for line, quote, _ in _quote(result))
                code = (
                    _imports_summary(result)
                    if _imports_only(result)
                    else "<br>".join(
                        f"`{m.usage.path}:{m.usage.line}` {m.reason}"
                        for m in result.matches[:MAX_MATCHES_SHOWN]
                    )
                    or "—"
                )
                summary = claim.summary.replace("|", "\\|")
                out.append(
                    f"| {result.status.value} | {claim.kind.value}: {summary} | "
                    f"{cites.replace('|', chr(92) + '|')} | {code.replace('|', chr(92) + '|')} |"
                )
            out.append("")
        model = _model_line(bump_report)
        if model:
            out.append(f"<sub>{model}</sub>")
            out.append("")
        for warning in bump_report.warnings:
            out.append(f"> {warning}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
