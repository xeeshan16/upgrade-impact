from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.mock_services import Services
from upgrade_impact import cli
from upgrade_impact.sources.github import GitHubClient
from upgrade_impact.sources.registry import RegistryClient


@pytest.fixture
def services(sample_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Services:
    handler = Services(sample_repo)
    transport = httpx.MockTransport(handler)

    def github(**kwargs: Any) -> GitHubClient:
        return GitHubClient(transport=transport, sleep=lambda _: None, **kwargs)

    def registry(**kwargs: Any) -> RegistryClient:
        return RegistryClient(transport=transport, sleep=lambda _: None, **kwargs)

    monkeypatch.setattr(cli, "GitHubClient", github)
    monkeypatch.setattr(cli, "RegistryClient", registry)
    monkeypatch.setenv("UPGRADE_IMPACT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.chdir(tmp_path)
    return handler


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_check_a_pr_url_end_to_end(services: Services, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(
        capsys,
        "check",
        "https://github.com/owner/repo/pull/9",
        "--provider",
        "fake",
        "--format",
        "json",
    )
    assert code == 0, err
    report = json.loads(out)
    [bump] = report["bumps"]
    assert bump["bump"]["package"] == "httpx"
    assert (bump["bump"]["from_version"], bump["bump"]["to_version"]) == ("0.27.0", "0.28.1")
    assert bump["bump"]["source"] == "manifest-diff"
    # 0.28.0 and 0.28.1 from releases, 0.27.x from the CHANGELOG.
    assert bump["notes_versions"] == ["0.27.1", "0.27.2", "0.28.0", "0.28.1"]
    assert bump["notes_sources"] == ["changelog", "github-release"]
    assert bump["model"]["provider"] == "fake"
    by_params = {tuple(r["claim"]["parameters"]): r for r in bump["results"]}
    proxies = by_params[("proxies",)]
    # The notes name the parameter but not the API, so the match is "possible".
    assert proxies["status"] == "possible"
    assert next((m["usage"]["path"], m["usage"]["line"]) for m in proxies["matches"]) == (
        "app/http.py",
        9,
    )
    assert report["repository_root"].endswith(tuple("0123456789abcdef"))
    # The token went to the API host only.
    for request in services.requests:
        has_token = request.headers.get("authorization") == "Bearer test-token"
        assert has_token == (request.url.host == "api.github.com")


def test_text_and_markdown_output(services: Services, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        capsys, "check", "https://github.com/owner/repo/pull/9", "--provider", "fake"
    )
    assert code == 0
    assert out.startswith("upgrade-impact: httpx 0.27.0 -> 0.28.1 (pypi)\n")
    assert "REVIEW: possible impact" in out
    assert (
        "app/http.py:9  return httpx.Client(proxies=proxy_url, timeout=10)  (passes proxies "
        "to httpx.Client; the notes do not say which API lost it)" in out
    )
    assert "fake provider: heuristic stand-in, not a model" in out

    code, md, _ = run(
        capsys,
        "check",
        "https://github.com/owner/repo/pull/9",
        "--provider",
        "fake",
        "--format",
        "markdown",
    )
    assert code == 0
    assert md.startswith("### `httpx` 0.27.0 → 0.28.1")
    assert "| status | change | notes | your code |" in md


def test_no_llm_lists_notes_and_usages(
    services: Services, capsys: pytest.CaptureFixture[str], sample_repo: Path
) -> None:
    code, out, _ = run(
        capsys,
        "check",
        "--package",
        "httpx",
        "--from",
        "0.27.0",
        "--to",
        "0.28.1",
        "--repo",
        str(sample_repo),
        "--no-llm",
    )
    assert code == 0
    assert "claims: not extracted (--no-llm)" in out
    assert "[unclaimed]" in out
    assert "The deprecated `proxies` argument has now been removed." in out
    assert "usage(s) of httpx in 2 file(s)" in out


def test_diff_from_stdin_and_file(
    services: Services,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    sample_repo: Path,
    tmp_path: Path,
) -> None:
    diff = (
        "--- a/requirements.txt\n+++ b/requirements.txt\n"
        "@@ -1 +1 @@\n-httpx==0.27.0\n+httpx==0.28.1\n"
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(diff))
    code, out, _ = run(capsys, "check", "-", "--repo", str(sample_repo), "--provider", "fake")
    assert code == 0
    assert "httpx 0.27.0 -> 0.28.1" in out
    path = tmp_path / "bump.diff"
    path.write_text(diff)
    output = tmp_path / "report.json"
    code, out, _ = run(
        capsys,
        "check",
        str(path),
        "--repo",
        str(sample_repo),
        "--provider",
        "fake",
        "--format",
        "json",
        "-o",
        str(output),
    )
    assert code == 0
    assert out == ""
    assert json.loads(output.read_text())["source"] == str(path)


def test_npm_bump_indexes_javascript_and_typescript(
    services: Services, capsys: pytest.CaptureFixture[str], fixtures: Path
) -> None:
    code, out, _ = run(
        capsys,
        "check",
        "--package",
        "express",
        "--ecosystem",
        "npm",
        "--from",
        "4.21.2",
        "--to",
        "5.0.1",
        "--repo",
        str(fixtures / "repos" / "jsapp"),
        "--import-name",
        "axios",
        "--no-llm",
    )
    assert code == 0
    assert "express 4.21.2 -> 5.0.1 (npm)" in out
    assert "in 4 file(s); 4 JavaScript/TypeScript file(s) scanned" in out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["check"], "give a PR URL"),
        (["check", "https://gitlab.com/o/r/-/merge_requests/1"], "not a GitHub pull request"),
        (["check", "missing.diff"], "does not exist"),
        (["check", "--package", "httpx"], "--package needs --to"),
        (["check", "x.diff", "--ref", "abc"], "--ref only applies"),
        (["check", "--package", "httpx", "--to", "1", "--repo", "/nope"], "is not a directory"),
    ],
)
def test_usage_errors(
    services: Services, capsys: pytest.CaptureFixture[str], args: list[str], message: str
) -> None:
    code, _, err = run(capsys, *args, "--no-llm")
    assert code == 2
    assert message in err


def test_a_diff_with_no_bump_is_a_usage_error(
    services: Services, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "x.diff"
    path.write_text("--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n")
    code, _, err = run(capsys, "check", str(path), "--no-llm")
    assert code == 2
    assert "no dependency bump found" in err


def test_ref_selects_the_commit_to_analyse(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(
        capsys, "check", "https://github.com/owner/repo/pull/9", "--ref", "c" * 40, "--no-llm"
    )
    assert code == 2
    assert "could not download the PR's code" in err
    assert any(r.url.path.endswith("/tarball/" + "c" * 40) for r in services.requests)


def test_missing_pr_is_reported(services: Services, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "check", "https://github.com/owner/repo/pull/404", "--no-llm")
    assert code == 1
    assert "not found" in err


def test_version_and_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "upgrade_impact", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("upgrade-impact ")


def test_second_run_is_answered_from_the_cache(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ("check", "https://github.com/owner/repo/pull/9", "--provider", "fake")
    code, first, _ = run(capsys, *args)
    assert code == 0
    assert "from cache" not in first
    assert "code review: fake fake-extractor" in first
    code, second, _ = run(capsys, *args)
    assert code == 0
    assert "model: fake fake-extractor (fake provider: heuristic stand-in, not a model), " in second
    assert "4 call(s) (4 from cache), 0 in / 0 out tokens" in second
    assert "code review: fake fake-extractor" in second

    code, uncached, _ = run(capsys, *args, "--no-cache", "--no-review")
    assert code == 0
    assert "from cache" not in uncached
    assert "code review:" not in uncached


GROUP_PR_URL = "https://github.com/owner/repo/pull/10"


def test_grouped_pr_checks_every_package(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "check", GROUP_PR_URL, "--provider", "fake", "--format", "json")
    assert code == 0, err
    report = json.loads(out)
    verdicts = {b["bump"]["package"]: b["verdict"] for b in report["bumps"]}
    # rich has no release notes here (PyPI 404), so it is unknown; httpx has a
    # possible match, so the PR as a whole is "review".
    assert verdicts == {"httpx": "review", "rich": "unknown"}
    assert report["verdict"] == "review"
    assert [b["bump"]["source"] for b in report["bumps"]] == ["manifest-diff", "pr-body"]

    code, out, _ = run(capsys, "check", GROUP_PR_URL, "--provider", "fake")
    assert code == 0
    overview = out.split("\n\n", 1)[0].splitlines()
    assert overview[0] == f"upgrade-impact: 2 bumps in {GROUP_PR_URL}: review"
    assert overview[1].split()[:2] == ["review", "httpx"]
    assert overview[2].split()[:2] == ["unknown", "rich"]


def test_only_selects_packages_of_a_group(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(
        capsys, "check", GROUP_PR_URL, "--provider", "fake", "--only", "Rich", "--format", "json"
    )
    assert code == 0, err
    assert [b["bump"]["package"] for b in json.loads(out)["bumps"]] == ["rich"]
    code, _, err = run(capsys, "check", GROUP_PR_URL, "--provider", "fake", "--only", "nope")
    assert code == cli.EXIT_USAGE
    assert "found: httpx, rich" in err


@pytest.mark.parametrize(
    ("fail_on", "expected"),
    [("never", 0), ("affected", 0), ("review", cli.EXIT_FAIL_ON), ("unknown", cli.EXIT_FAIL_ON)],
)
def test_fail_on_sets_the_exit_code(
    services: Services, capsys: pytest.CaptureFixture[str], fail_on: str, expected: int
) -> None:
    code, _, err = run(
        capsys,
        "check",
        "https://github.com/owner/repo/pull/9",
        "--provider",
        "fake",
        "--fail-on",
        fail_on,
    )
    assert code == expected, err
    assert ("meets --fail-on" in err) == bool(expected)


def test_ci_outputs_json_summary_and_annotations(
    services: Services, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("# earlier step\n", encoding="utf-8")
    code, out, err = run(
        capsys,
        "check",
        "https://github.com/owner/repo/pull/9",
        "--provider",
        "fake",
        "--json-out",
        str(tmp_path / "out" / "report.json"),
        "--summary-file",
        str(summary),
        "--annotate",
    )
    assert code == 0, err
    report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "review"
    written = summary.read_text(encoding="utf-8")
    assert written.startswith("# earlier step\n")  # appended, not replaced
    assert "### `httpx` 0.27.0 → 0.28.1" in written
    notices = [line for line in out.splitlines() if line.startswith("::notice ")]
    assert any(
        n.startswith("::notice file=app/http.py,line=9,title=httpx 0.28.1%3A ") for n in notices
    )
    assert not [line for line in out.splitlines() if line.startswith("::warning ")]
