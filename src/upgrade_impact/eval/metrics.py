"""Scores: file and line precision/recall, false alarms, with bootstrap intervals.

* **File level** (impact cases): a predicted file is right when a human changed
  it in the fix.
* **Line level** (impact cases, files whose fix lines map onto the snapshot):
  a predicted line is right when it lies within ``window`` lines of a fixed
  hunk; a hunk is found when any predicted line hits it. Predicted lines in
  label files whose lines are unknown are left out of line precision.
* **Detection**: impact cases with at least one right file.
* **False alarm** (no-impact cases): the system predicted any place at all.

Intervals resample *repositories* (cluster bootstrap), because cases from one
repository are not independent.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from upgrade_impact.eval.dataset import Case, Label

Place = tuple[str, int]


@dataclass(frozen=True, slots=True)
class CaseScore:
    case_id: str
    repo: str
    label: Label
    predicted_files: int
    file_tp: int
    file_fp: int
    file_fn: int
    predicted_lines: int
    scored_lines: int
    line_hits: int
    hunks: int
    hunks_found: int

    @property
    def detected(self) -> bool:
        return self.file_tp > 0

    @property
    def flagged(self) -> bool:
        return self.predicted_lines > 0


def score_case(case: Case, places: set[Place], window: int = 2) -> CaseScore:
    predicted_files = {p for p, _ in places}
    label_files = {f.path: f for f in case.files}
    tp = len(predicted_files & label_files.keys())
    scored = hits = 0
    found: set[tuple[str, int]] = set()
    hunks = 0
    for f in case.files:
        if f.lines_known:
            hunks += len(f.hunks)
    for path, line in places:
        label = label_files.get(path)
        if label is not None and not label.lines_known:
            continue
        scored += 1
        if label is None:
            continue
        hit = False
        for i, h in enumerate(label.hunks):
            if h.start - window <= line <= h.end + window:
                found.add((path, i))
                hit = True
        hits += hit
    return CaseScore(
        case_id=case.id,
        repo=case.repo,
        label=case.label,
        predicted_files=len(predicted_files),
        file_tp=tp,
        file_fp=len(predicted_files) - tp,
        file_fn=len(label_files) - tp,
        predicted_lines=len(places),
        scored_lines=scored,
        line_hits=hits,
        hunks=hunks,
        hunks_found=len(found),
    )


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def aggregate(scores: Sequence[CaseScore]) -> dict[str, float | None]:
    impact = [s for s in scores if s.label == Label.IMPACT]
    clean = [s for s in scores if s.label == Label.NO_IMPACT]
    tp = sum(s.file_tp for s in impact)
    fp = sum(s.file_fp for s in impact)
    fn = sum(s.file_fn for s in impact)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else None
    )
    return {
        "file_precision": precision,
        "file_recall": recall,
        "file_f1": f1,
        "line_precision": _ratio(
            sum(s.line_hits for s in impact), sum(s.scored_lines for s in impact)
        ),
        "hunk_recall": _ratio(sum(s.hunks_found for s in impact), sum(s.hunks for s in impact)),
        "detection_rate": _ratio(sum(s.detected for s in impact), len(impact)),
        "false_alarm_rate": _ratio(sum(s.flagged for s in clean), len(clean)),
        "lines_per_case": _ratio(sum(s.predicted_lines for s in scores), len(scores)),
    }


Metric = Callable[[Sequence[CaseScore]], float | None]


def bootstrap(
    scores: Sequence[CaseScore],
    names: Sequence[str],
    *,
    samples: int = 1000,
    seed: int = 7,
    level: float = 0.95,
) -> dict[str, tuple[float, float] | None]:
    """Percentile intervals from resampling repositories with replacement."""
    by_repo: dict[str, list[CaseScore]] = defaultdict(list)
    for s in scores:
        by_repo[s.repo].append(s)
    repos = sorted(by_repo)
    out: dict[str, tuple[float, float] | None] = {}
    if len(repos) < 2:
        return dict.fromkeys(names)
    rng = random.Random(seed)  # noqa: S311 - statistics, not security
    draws: dict[str, list[float]] = {n: [] for n in names}
    for _ in range(samples):
        sample = [s for r in rng.choices(repos, k=len(repos)) for s in by_repo[r]]
        values = aggregate(sample)
        for n in names:
            v = values.get(n)
            if v is not None:
                draws[n].append(v)
    tail = (1 - level) / 2
    for n in names:
        ordered = sorted(draws[n])
        if len(ordered) < samples // 2:
            out[n] = None
            continue
        lo = ordered[int(tail * (len(ordered) - 1))]
        hi = ordered[int((1 - tail) * (len(ordered) - 1))]
        out[n] = (lo, hi)
    return out
