"""Markdown rendering of an eval summary."""

from __future__ import annotations

from typing import Any

from upgrade_impact.eval.runner import MODES


def _pct(value: float | None, ci: list[float] | tuple[float, float] | None = None) -> str:
    if value is None:
        return "n/a"
    text = f"{100 * value:.1f}%"
    if ci:
        text += f" ({100 * ci[0]:.0f}-{100 * ci[1]:.0f})"
    return text


# Per-case rows are listed when the run is this small (a real-model sample).
MAX_CASE_ROWS = 40
MAX_FLAGGED = 20
_CAUSES = {
    "possible": "possible matches",
    "no_api": "claims naming no API",
    "runtime": "of which runtime",
    "unclaimed": "unclaimed note lines",
    "suspicious": "injection-like note lines",
}


def _causes(counts: dict[str, int]) -> str:
    return ", ".join(f"{_CAUSES.get(k, k)}: {v}" for k, v in counts.items()) or "—"


def _case_table(rows: list[dict[str, Any]]) -> list[str]:
    out = [
        "",
        "Per case (upgrade-impact):",
        "",
        "| Case | Label | Notes | Verdict | Affected / possible lines | Held at review by | "
        "Ruled out |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        p = row["systems"].get("upgrade-impact")
        if p is None:
            continue
        ruled = ", ".join(f"{k} {v}" for k, v in (p.get("dismissed") or {}).items()) or "—"
        verdict = f"error: {p['error'][:60]}" if p["error"] else p.get("verdict") or "—"
        out.append(
            f"| [{row['id']}]({row['url']}) | {row['label']} | {row['notes_lines']} | "
            f"{verdict} | {len(p['affected'])} / {len(p['possible'])} | "
            f"{_causes(p.get('review_causes') or {})} | {ruled} |"
        )
    return out


def _verdict_lines(summary: dict[str, Any]) -> list[str]:
    out: list[str] = []
    verdicts = {n: e["verdicts"] for n, e in summary["systems"].items() if "verdicts" in e}
    if verdicts:
        out += ["", "Bump verdicts (upgrade-impact's own answer per case):", ""]
        for name, by_label in verdicts.items():
            for label, counts in by_label.items():
                if counts:
                    parts = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
                    out.append(f"- {name}, {label} cases: {parts}")
    for name, entry in summary["systems"].items():
        by_label = entry.get("review_causes")
        if by_label:
            out += ["", f'What held {name} at "review" (cases per cause):', ""]
            for label, counts in by_label.items():
                if counts:
                    out.append(f"- {label} cases: {_causes(counts)}")
        if entry.get("dismissed_lines"):
            ruled = ", ".join(f"{k}: {v}" for k, v in entry["dismissed_lines"].items())
            out.append(f"- note lines ruled out ({name}): {ruled}")
        if flagged := entry.get("suspicious_lines"):
            out += ["", f"Note lines {name} flagged as injection-like (check for false positives):"]
            out.append("")
            out += [f"- {item['case']} {item['line']}" for item in flagged[:MAX_FLAGGED]]
    return out


def render_summary(summary: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> str:
    out = [
        "# upgrade-impact eval",
        "",
        f"Dataset `{summary.get('dataset', '?')}`, split `{summary.get('split', '?')}`: "
        f"{summary['cases_scored']} cases from {summary['repositories']} repositories "
        f"({summary['impact_cases']} impact, {summary['no_impact_cases']} no-impact). "
        f"Provider `{summary.get('provider', '?')}`, model `{summary.get('model', '?')}`. "
        f"Line hits within {summary['window']} lines of a fixed hunk. "
        "95% intervals resample repositories.",
        "",
        "| System | Scoring | File precision | File recall | Hunk recall | Detected | "
        "False alarms | Lines/case |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, entry in summary["systems"].items():
        for mode in MODES:
            if mode == "lenient" and entry["lenient"] == entry["strict"]:
                continue
            m, ci = entry[mode]["metrics"], entry[mode]["ci95"]
            lines = m.get("lines_per_case")
            out.append(
                f"| {name} | {mode} | {_pct(m['file_precision'], ci.get('file_precision'))} | "
                f"{_pct(m['file_recall'], ci.get('file_recall'))} | "
                f"{_pct(m['hunk_recall'], ci.get('hunk_recall'))} | "
                f"{_pct(m['detection_rate'], ci.get('detection_rate'))} | "
                f"{_pct(m['false_alarm_rate'], ci.get('false_alarm_rate'))} | "
                f"{'n/a' if lines is None else f'{lines:.1f}'} |"
            )
    out += [
        "",
        "| System | Calls (cached) | Tokens in/out | Seconds | Errors |",
        "|---|---|---|---|---|",
    ]
    for name, entry in summary["systems"].items():
        out.append(
            f"| {name} | {entry['calls']} ({entry['cached_calls']}) | "
            f"{entry['input_tokens']}/{entry['output_tokens']} | {entry['seconds']} | "
            f"{entry['errors']} |"
        )
    out += _verdict_lines(summary)
    if rows and len(rows) <= MAX_CASE_ROWS:
        out += _case_table(rows)
    if summary.get("case_errors"):
        out += ["", "Cases that could not be scored:", ""]
        out += [f"- {k}: {v}" for k, v in summary["case_errors"].items()]
    return "\n".join(out) + "\n"
