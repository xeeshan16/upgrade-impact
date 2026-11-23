from __future__ import annotations

from pathlib import Path

import pytest

from tests.gitrepo import build_history
from upgrade_impact.eval.dataset import (
    Case,
    Label,
    Split,
    dump_cases,
    load_cases,
    select,
    split_for,
)
from upgrade_impact.eval.git import Git, GitError, Hunk, clone, parse_hunks, valid_repo_slug
from upgrade_impact.eval.mine import (
    MineOptions,
    human_coauthors,
    is_bot,
    is_notable,
    looks_generated,
    mine_repositories,
)


@pytest.fixture(scope="module")
def remote(tmp_path_factory: pytest.TempPathFactory) -> str:
    base = tmp_path_factory.mktemp("history")
    return f"file://{build_history(base)}"


@pytest.fixture(scope="module")
def mined(remote: str, tmp_path_factory: pytest.TempPathFactory) -> list[Case]:
    cache = tmp_path_factory.mktemp("cache")
    cases, stats = mine_repositories(
        ["acme/app"], cache, MineOptions(since=None), git_base_url=remote
    )
    reasons = stats["acme/app"].reasons
    assert reasons["not a major (or 0.x minor) bump"] == 1
    assert reasons["clean bump fixed or reverted later"] == 1
    assert reasons["package not imported at the snapshot"] == 1
    return cases


def test_parse_hunks_reads_old_side_ranges_and_insertions() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -3 +3 @@\n-a\n+b\n"
        "@@ -10,3 +10,1 @@\n-c\n-d\n-e\n+f\n"
        "@@ -20,0 +18,2 @@\n+g\n+h\n"
        "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n"
    )
    assert parse_hunks(diff) == {"x.py": [Hunk(3, 3), Hunk(10, 12), Hunk(20, 20, pure_insert=True)]}


@pytest.mark.parametrize(
    ("start", "end", "notable"),
    [
        ("1.2.3", "2.0.0", True),
        ("0.27.2", "0.28.0", True),
        ("0.0.23", "0.0.24", True),
        ("1.2.3", "1.3.0", False),
        ("0.27.2", "0.27.3", False),
        ("2.0.0", "1.0.0", False),
        ("16", "17.0.0", True),
        ("not-a-version", "also-not", False),
    ],
)
def test_is_notable(start: str, end: str, notable: bool) -> None:
    assert is_notable(start, end) is notable


def test_bot_and_coauthor_detection() -> None:
    assert is_bot("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")
    assert is_bot("renovate[bot]", "29139614+renovate[bot]@users.noreply.github.com")
    assert is_bot("Renovate Bot", "bot@renovateapp.com")
    assert not is_bot("Jane Dev", "jane@example.com")
    body = (
        "Bumps x.\n\nCo-authored-by: dependabot[bot] <support@github.com>\n"
        "Co-authored-by: Jane Dev <jane@example.com>"
    )
    assert human_coauthors(body) == ["Jane Dev"]


def test_looks_generated() -> None:
    assert not looks_generated([Hunk(1, 3), Hunk(10, 10)])
    assert looks_generated([Hunk(1, 400)])
    assert looks_generated([Hunk(i, i) for i in range(1, 100, 2)])


def test_slug_validation_and_bad_clone(tmp_path: Path) -> None:
    assert valid_repo_slug("acme/app")
    assert not valid_repo_slug("../etc")
    with pytest.raises(GitError):
        clone("not a slug", tmp_path / "x")
    with pytest.raises(GitError):
        clone("acme/missing", tmp_path / "y", base_url=f"file://{tmp_path}/nowhere")


def test_merge_pr_with_a_human_fix_is_impact(mined: list[Case]) -> None:
    case = next(c for c in mined if c.package == "httpx")
    assert case.label == Label.IMPACT
    assert case.pr == 7
    assert case.merge_style == "merge"
    assert (case.from_version, case.to_version) == ("0.27.2", "0.28.0")
    [label] = case.files
    assert label.path == "app/main.py"
    assert label.lines_known
    assert [(h.start, h.end) for h in label.hunks] == [(3, 3)]
    assert case.import_files == 1
    assert case.url == "https://github.com/acme/app/pull/7"


def test_squash_without_humans_is_no_impact(mined: list[Case]) -> None:
    case = next(c for c in mined if c.package == "requests")
    assert case.label == Label.NO_IMPACT
    assert case.files == []
    assert case.merge_style == "squash"
    assert case.pr == 8


def test_squash_with_a_human_coauthor_is_impact(mined: list[Case]) -> None:
    case = next(c for c in mined if c.package == "rich")
    assert case.label == Label.IMPACT
    assert [f.path for f in case.files] == ["app/ui.py"]
    assert [(h.start, h.end) for h in case.files[0].hunks] == [(3, 3)]


def test_snapshot_is_before_the_fix(mined: list[Case], remote: str, tmp_path: Path) -> None:
    case = next(c for c in mined if c.package == "httpx")
    git = clone("acme/app", tmp_path / "clone", base_url=remote)
    text = git.file_text(case.snapshot, "app/main.py") or ""
    assert "AsyncClient(app=" in text
    assert git.file_text(case.snapshot, "missing.py") is None


def test_dataset_round_trip_and_splits(mined: list[Case], tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    assert dump_cases(mined, path) == 3
    loaded = load_cases(path)
    assert {c.id for c in loaded} == {c.id for c in mined}
    assert all(c.split == split_for("acme/app") for c in loaded)
    assert len(select(loaded, "all")) == 3
    assert len(select(loaded, split_for("acme/app").value)) == 3
    assert {split_for(f"org/repo{i}") for i in range(40)} == {Split.DEV, Split.TEST}


def test_load_cases_reports_the_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('// comment\n\n{"id": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.jsonl:3"):
        load_cases(path)


def test_git_wrapper_errors(tmp_path: Path) -> None:
    git = Git(tmp_path)
    with pytest.raises(GitError):
        git.run("log")


def test_sample_per_label_is_stable(mined: list[Case]) -> None:
    from upgrade_impact.eval.dataset import sample_per_label

    picked = sample_per_label(mined, 1)
    assert sorted(c.label for c in picked) == [Label.IMPACT, Label.NO_IMPACT]
    assert [c.id for c in sample_per_label(list(reversed(mined)), 1)] == [c.id for c in picked]
    assert len(sample_per_label(mined, 5)) == 3
