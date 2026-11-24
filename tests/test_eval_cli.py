"""``upgrade-impact mine`` and ``eval`` end to end on a local git history.

Release notes are put in the eval's notes cache up front, so nothing here
touches the network; the model is the deterministic fake provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.gitrepo import build_history
from upgrade_impact.cli import main
from upgrade_impact.eval.dataset import Label, load_cases
from upgrade_impact.eval.runner import notes_cache_path
from upgrade_impact.models import NotesSection, ReleaseNotes

NOTES = {
    "httpx": ["The deprecated `app` argument has now been removed."],
    "requests": ["Dropped support for Python 2.7."],
    "rich": ["Removed `Console.print_exception`."],
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    remote = build_history(tmp_path / "history")
    cache = tmp_path / "cache"
    monkeypatch.setenv("UPGRADE_IMPACT_GIT_URL", f"file://{remote}")
    monkeypatch.setenv("UPGRADE_IMPACT_CACHE_DIR", str(cache))
    monkeypatch.delenv("UPGRADE_IMPACT_PROVIDER", raising=False)
    return cache


def _seed_notes(dataset: Path, cache: Path) -> None:
    for case in load_cases(dataset):
        notes = ReleaseNotes.from_sections(
            case.package,
            [NotesSection(version=case.to_version, source="changelog", lines=NOTES[case.package])],
        )
        path = notes_cache_path(case, cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(notes.model_dump_json(), encoding="utf-8")


def test_mine_then_eval(env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repos = tmp_path / "repos.txt"
    repos.write_text("# comment\nacme/app\n", encoding="utf-8")
    dataset = tmp_path / "cases.jsonl"
    assert main(["mine", "--repos-file", str(repos), "-o", str(dataset), "--since", ""]) == 0
    cases = load_cases(dataset)
    assert sorted((c.package, c.label) for c in cases) == [
        ("httpx", Label.IMPACT),
        ("requests", Label.NO_IMPACT),
        ("rich", Label.IMPACT),
    ]
    assert "wrote 3 cases (2 impact, 1 no-impact)" in capsys.readouterr().err

    _seed_notes(dataset, env)
    gate = tmp_path / "gate.json"
    gate.write_text(
        json.dumps(
            {
                "checks": [
                    {"system": "import-grep", "metric": "file_recall", "min": 1.0},
                    {"system": "upgrade-impact", "mode": "totals", "metric": "errors", "max": 0},
                ]
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "eval"
    code = main(
        [
            "eval", str(dataset), "--split", "all", "--provider", "fake", "--out", str(out),
            "--bootstrap", "50", "--gate", str(gate),
        ]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "| import-grep | strict |" in captured.out
    assert "gate: 2 checks passed" in captured.err

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["impact_cases"] == 2
    assert summary["no_impact_cases"] == 1
    assert summary["provider"] == "fake"
    systems = summary["systems"]
    assert set(systems) == {"upgrade-impact", "import-grep", "symbol-grep", "naive-prompt"}
    # Every file that imports the package is flagged, so file recall is perfect and
    # the clean requests bump is a false alarm.
    grep = systems["import-grep"]["strict"]["metrics"]
    assert grep["file_recall"] == 1.0
    assert grep["false_alarm_rate"] == 1.0
    # symbol-grep finds `app` on main.py:3 for httpx (the line that was fixed).
    rows = [json.loads(line) for line in (out / "cases.jsonl").read_text().splitlines()]
    httpx_row = next(r for r in rows if r["id"].endswith(":httpx"))
    assert ["app/main.py", 3] in httpx_row["systems"]["symbol-grep"]["affected"]
    assert httpx_row["systems"]["upgrade-impact"]["verdict"] in {"affected", "review"}
    assert "review_causes" in httpx_row["systems"]["upgrade-impact"]
    assert httpx_row["systems"]["upgrade-impact"]["claims"]
    assert summary["settings"]["recheck"] == "on"
    report = (out / "report.md").read_text()
    assert report.startswith("# upgrade-impact eval")
    # A small run lists every case with what held it at "review".
    assert "| Case | Label | Notes | Verdict |" in report
    assert httpx_row["id"] in report


def test_eval_gate_failure_exits_one(
    env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "cases.jsonl"
    assert main(["mine", "acme/app", "-o", str(dataset), "--since", ""]) == 0
    _seed_notes(dataset, env)
    gate = tmp_path / "gate.json"
    gate.write_text(
        json.dumps({"checks": [{"system": "import-grep", "metric": "false_alarm_rate", "max": 0}]}),
        encoding="utf-8",
    )
    code = main(
        [
            "eval", str(dataset), "--split", "all", "--no-llm", "--systems", "import-grep",
            "--out", str(tmp_path / "e"), "--bootstrap", "10", "--gate", str(gate),
        ]
    )  # fmt: skip
    assert code == 1
    assert "gate: import-grep lenient false_alarm_rate = 1.000 > 0" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["eval", "missing.jsonl"], "does not exist"),
        (["mine", "-o", "x.jsonl"], "give owner/repo slugs"),
    ],
)
def test_usage_errors(
    argv: list[str], message: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    assert message in capsys.readouterr().err


def test_eval_rejects_unknown_systems_and_empty_splits(
    env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "cases.jsonl"
    assert main(["mine", "acme/app", "-o", str(dataset), "--since", ""]) == 0
    assert main(["eval", str(dataset), "--systems", "magic"]) == 2
    assert "unknown system(s): magic" in capsys.readouterr().err
    assert main(["eval", str(dataset), "--split", "all", "--label", "impact", "--limit", "0"]) == 2
    assert "no cases" in capsys.readouterr().err
