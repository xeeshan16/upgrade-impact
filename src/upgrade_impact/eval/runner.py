"""Run every system over the dataset and score it.

For each case: clone the repository (partial, cached), export the source files
at the snapshot, read the release notes once (cached on disk so every system
and every re-run sees the same notes), then let each system predict.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from upgrade_impact.eval.dataset import Case, Label
from upgrade_impact.eval.git import GitError, clone
from upgrade_impact.eval.metrics import CaseScore, aggregate, bootstrap, score_case
from upgrade_impact.eval.snapshot import ensure_snapshot, package_usages
from upgrade_impact.eval.systems import CaseContext, Prediction, System
from upgrade_impact.models import ReleaseNotes
from upgrade_impact.pipeline import fetch_notes
from upgrade_impact.sources.github import GitHubClient
from upgrade_impact.sources.registry import RegistryClient

log = logging.getLogger(__name__)

MODES = ("strict", "lenient")
CI_METRICS = ("file_precision", "file_recall", "hunk_recall", "detection_rate", "false_alarm_rate")


def notes_cache_path(case: Case, cache_dir: Path) -> Path:
    key = hashlib.sha256(
        f"{case.ecosystem.value}|{case.package}|{case.from_version}|{case.to_version}".encode()
    ).hexdigest()[:20]
    return cache_dir / "notes" / f"{key}.json"


def notes_for(
    case: Case, cache_dir: Path, registry: RegistryClient, github: GitHubClient
) -> ReleaseNotes:
    path = notes_cache_path(case, cache_dir)
    if path.exists():
        return ReleaseNotes.model_validate_json(path.read_text(encoding="utf-8"))
    notes = fetch_notes(case.bump, registry, github)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notes.model_dump_json(), encoding="utf-8")
    return notes


@dataclass(slots=True)
class CaseRecord:
    case: Case
    predictions: dict[str, Prediction] = field(default_factory=dict)
    notes_lines: int = 0
    error: str | None = None


@dataclass(slots=True)
class EvalResult:
    records: list[CaseRecord]
    systems: list[str]
    window: int
    meta: dict[str, Any] = field(default_factory=dict)

    def scores(self, system: str, mode: str) -> list[CaseScore]:
        return [
            score_case(r.case, r.predictions[system].places(mode), self.window)
            for r in self.records
            if system in r.predictions and r.predictions[system].error is None
        ]

    def summary(self, *, samples: int = 1000) -> dict[str, Any]:
        scored = [r for r in self.records if r.error is None]
        out: dict[str, Any] = {
            **self.meta,
            "window": self.window,
            "cases": len(self.records),
            "cases_scored": len(scored),
            "impact_cases": sum(r.case.label == Label.IMPACT for r in scored),
            "no_impact_cases": sum(r.case.label == Label.NO_IMPACT for r in scored),
            "repositories": len({r.case.repo for r in scored}),
            "case_errors": {r.case.id: r.error for r in self.records if r.error},
            "systems": {},
        }
        for system in self.systems:
            preds = [r.predictions[system] for r in scored if system in r.predictions]
            entry: dict[str, Any] = {
                "errors": sum(p.error is not None for p in preds),
                "calls": sum(run.calls for p in preds for run in p.runs),
                "cached_calls": sum(run.cached_calls for p in preds for run in p.runs),
                "input_tokens": sum(run.usage.input_tokens for p in preds for run in p.runs),
                "output_tokens": sum(run.usage.output_tokens for p in preds for run in p.runs),
                "seconds": round(sum(p.seconds for p in preds), 2),
            }
            verdicts: dict[str, Counter[str]] = {"impact": Counter(), "no_impact": Counter()}
            for r in scored:
                p = r.predictions.get(system)
                if p is not None and p.verdict is not None:
                    verdicts[r.case.label.value][p.verdict] += 1
            if any(verdicts.values()):
                entry["verdicts"] = {k: dict(v) for k, v in verdicts.items()}
            for mode in MODES:
                scores = self.scores(system, mode)
                entry[mode] = {
                    "metrics": aggregate(scores),
                    "ci95": bootstrap(scores, CI_METRICS, samples=samples),
                }
            out["systems"][system] = entry
        return out

    def case_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for r in self.records:
            row: dict[str, Any] = {
                "id": r.case.id,
                "url": r.case.url,
                "label": r.case.label.value,
                "split": r.case.split.value,
                "label_files": sorted(r.case.label_paths),
                "notes_lines": r.notes_lines,
                "error": r.error,
                "systems": {},
            }
            for name, p in r.predictions.items():
                row["systems"][name] = {
                    "affected": sorted(p.affected),
                    "possible": sorted(p.possible),
                    "verdict": p.verdict,
                    "error": p.error,
                    "seconds": round(p.seconds, 3),
                }
            rows.append(row)
        return rows


def run_eval(
    cases: Sequence[Case],
    systems: Sequence[System],
    *,
    cache_dir: Path,
    registry: RegistryClient,
    github: GitHubClient,
    window: int = 2,
    git_base_url: str = "https://github.com",
    progress: Callable[[str], None] = lambda _m: None,
) -> EvalResult:
    records: list[CaseRecord] = []
    for number, case in enumerate(cases, start=1):
        record = CaseRecord(case=case)
        records.append(record)
        started = time.perf_counter()
        try:
            git = clone(
                case.repo, cache_dir / "git" / case.repo.replace("/", "__"), base_url=git_base_url
            )
            root = ensure_snapshot(git, cache_dir, case.repo, case.snapshot)
        except GitError as exc:
            record.error = f"code: {exc}"
            progress(f"[{number}/{len(cases)}] {case.id}: {record.error}")
            continue
        notes = notes_for(case, cache_dir, registry, github)
        record.notes_lines = len(notes.lines)
        ctx = CaseContext(case=case, root=root, notes=notes, usages=package_usages(root, case.bump))
        for system in systems:
            try:
                record.predictions[system.name] = system.predict(ctx)
            except Exception as exc:  # one system failing must not stop the eval
                log.exception("%s failed on %s", system.name, case.id)
                record.predictions[system.name] = Prediction(error=f"{type(exc).__name__}: {exc}")
        progress(
            f"[{number}/{len(cases)}] {case.id} ({case.label.value}, "
            f"{len(notes.lines)} note lines) in {time.perf_counter() - started:.1f}s"
        )
    return EvalResult(records=records, systems=[s.name for s in systems], window=window)


# -- gate ------------------------------------------------------------------------


def check_gate(summary: dict[str, Any], gate: dict[str, Any]) -> list[str]:
    """Threshold checks: ``{"checks": [{"system", "mode", "metric", "min"|"max"}]}``.

    ``mode`` is ``strict`` or ``lenient`` (default) for scores, or ``totals`` for
    the per-system counters (``calls``, ``errors``, ``input_tokens``...). A check
    with ``summary`` instead of ``system`` tests a run-level count such as
    ``cases_scored``."""
    failures: list[str] = []
    for check in gate.get("checks", []):
        if "summary" in check:
            key = check["summary"]
            total = summary.get(key)
            if not isinstance(total, int | float):
                failures.append(f"{key}: no value")
            elif "min" in check and total < check["min"]:
                failures.append(f"{key} = {total} < {check['min']}")
            elif "max" in check and total > check["max"]:
                failures.append(f"{key} = {total} > {check['max']}")
            continue
        system, mode, metric = check["system"], check.get("mode", "lenient"), check["metric"]
        entry = summary["systems"].get(system)
        if entry is None:
            failures.append(f"{system}: not in this run")
            continue
        value = entry[mode]["metrics"].get(metric) if mode in MODES else entry.get(metric)
        label = f"{system} {mode} {metric}"
        if value is None:
            failures.append(f"{label}: no value")
            continue
        if "min" in check and value < check["min"]:
            failures.append(f"{label} = {value:.3f} < {check['min']}")
        if "max" in check and value > check["max"]:
            failures.append(f"{label} = {value:.3f} > {check['max']}")
    return failures


def load_gate(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
        raise ValueError(f"{path}: expected an object with a 'checks' list")
    return data
