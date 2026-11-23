from __future__ import annotations

import pytest

from upgrade_impact.eval.dataset import Case, Label, LabelFile, LabelHunk, Split
from upgrade_impact.eval.metrics import aggregate, bootstrap, score_case
from upgrade_impact.eval.runner import check_gate
from upgrade_impact.models import Ecosystem


def make_case(
    case_id: str,
    repo: str,
    files: list[LabelFile] | None = None,
) -> Case:
    return Case(
        id=case_id,
        repo=repo,
        url=f"https://github.com/{repo}/pull/1",
        ecosystem=Ecosystem.PYPI,
        package="httpx",
        from_version="0.27.2",
        to_version="0.28.0",
        snapshot="0" * 40,
        label=Label.IMPACT if files else Label.NO_IMPACT,
        files=files or [],
        merged_at="2026-01-01T00:00:00Z",
        merge_style="merge",
        split=Split.DEV,
    )


IMPACT = make_case(
    "a#1:httpx",
    "acme/a",
    [
        LabelFile(path="app/main.py", lines_known=True, hunks=[LabelHunk(start=10, end=12)]),
        LabelFile(path="app/other.py", lines_known=True, hunks=[LabelHunk(start=40, end=40)]),
        LabelFile(path="app/moved.py", lines_known=False),
    ],
)


def test_score_case_counts_files_lines_and_hunks() -> None:
    places = {
        ("app/main.py", 9),  # within the 2-line window of 10-12
        ("app/main.py", 30),  # wrong line in a right file
        ("app/moved.py", 5),  # right file, lines unknown: not line-scored
        ("app/unrelated.py", 1),  # wrong file
    }
    s = score_case(IMPACT, places, window=2)
    assert (s.file_tp, s.file_fp, s.file_fn) == (2, 1, 1)
    assert s.predicted_lines == 4
    assert s.scored_lines == 3
    assert s.line_hits == 1
    assert (s.hunks, s.hunks_found) == (2, 1)
    assert s.detected
    assert score_case(IMPACT, {("app/main.py", 5)}, window=2).line_hits == 0
    assert score_case(IMPACT, {("app/main.py", 5)}, window=5).line_hits == 1


def test_aggregate_separates_impact_and_clean_cases() -> None:
    clean_quiet = make_case("b#2:httpx", "acme/b")
    clean_noisy = make_case("c#3:httpx", "acme/c")
    scores = [
        score_case(IMPACT, {("app/main.py", 11), ("app/x.py", 1)}),
        score_case(clean_quiet, set()),
        score_case(clean_noisy, {("app/main.py", 3)}),
    ]
    m = aggregate(scores)
    assert m["file_precision"] == pytest.approx(0.5)
    assert m["file_recall"] == pytest.approx(1 / 3)
    assert m["file_f1"] == pytest.approx(0.4)
    assert m["line_precision"] == pytest.approx(0.5)
    assert m["hunk_recall"] == pytest.approx(0.5)
    assert m["detection_rate"] == 1.0
    assert m["false_alarm_rate"] == 0.5
    assert m["lines_per_case"] == pytest.approx(1.0)


def test_aggregate_without_cases_is_undefined() -> None:
    m = aggregate([])
    assert m["file_precision"] is None
    assert m["false_alarm_rate"] is None
    assert m["file_f1"] is None


def test_bootstrap_resamples_repositories_deterministically() -> None:
    scores = [
        score_case(IMPACT, {("app/main.py", 11)}),
        score_case(make_case("d#1:httpx", "acme/d", IMPACT.files), set()),
        score_case(make_case("e#1:httpx", "acme/e"), {("x.py", 1)}),
        score_case(make_case("f#1:httpx", "acme/f"), set()),
    ]
    first = bootstrap(scores, ["detection_rate", "false_alarm_rate"], samples=200, seed=3)
    again = bootstrap(scores, ["detection_rate", "false_alarm_rate"], samples=200, seed=3)
    assert first == again
    interval = first["detection_rate"]
    assert interval is not None
    low, high = interval
    assert 0.0 <= low <= 0.5 <= high <= 1.0
    assert bootstrap(scores[:1], ["detection_rate"]) == {"detection_rate": None}


def test_gate_reports_each_failed_threshold() -> None:
    summary = {
        "systems": {
            "upgrade-impact": {
                "lenient": {"metrics": {"file_recall": 0.4, "false_alarm_rate": 0.5}},
                "calls": 3,
            }
        }
    }
    gate = {
        "checks": [
            {"system": "upgrade-impact", "metric": "file_recall", "min": 0.3},
            {"system": "upgrade-impact", "metric": "false_alarm_rate", "max": 0.4},
            {"system": "upgrade-impact", "mode": "totals", "metric": "calls", "max": 2},
            {"system": "naive-prompt", "metric": "file_recall", "min": 0.1},
            {"system": "upgrade-impact", "metric": "hunk_recall", "min": 0.1},
        ]
    }
    failures = check_gate(summary, gate)
    assert failures == [
        "upgrade-impact lenient false_alarm_rate = 0.500 > 0.4",
        "upgrade-impact totals calls = 3.000 > 2",
        "naive-prompt: not in this run",
        "upgrade-impact lenient hunk_recall: no value",
    ]


def test_gate_summary_level_checks() -> None:
    summary = {"cases_scored": 10, "systems": {}}
    gate = {
        "checks": [
            {"summary": "cases_scored", "min": 20},
            {"summary": "cases_scored", "max": 5},
            {"summary": "missing", "min": 1},
            {"summary": "cases_scored", "min": 1, "max": 50},
        ]
    }
    assert check_gate(summary, gate) == [
        "cases_scored = 10 < 20",
        "cases_scored = 10 > 5",
        "missing: no value",
    ]
