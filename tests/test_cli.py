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
    assert proxies["status"] == "affected"
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
    assert "AFFECTED" in out
    assert (
        "app/http.py:9  return httpx.Client(proxies=proxy_url, timeout=10)  (passes proxies)" in out
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


def test_npm_bump_reports_that_js_is_not_indexed_yet(
    services: Services, capsys: pytest.CaptureFixture[str], tmp_path: Path
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
        "--no-llm",
    )
    assert code == 0
    assert "express 4.21.2 -> 5.0.1 (npm)" in out
    assert "not indexed yet" in out


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
