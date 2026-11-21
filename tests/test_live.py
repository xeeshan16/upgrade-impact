"""Tests against the real services. Skipped unless the matching variable is set.

``UPGRADE_IMPACT_LIVE=1``: PyPI and raw.githubusercontent.com (no token needed).
``UPGRADE_IMPACT_LIVE_GITHUB=1``: also the GitHub API (set ``GITHUB_TOKEN``); CI runs these.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from upgrade_impact.config import Settings
from upgrade_impact.models import Bump, Ecosystem
from upgrade_impact.notes import collect_notes
from upgrade_impact.pipeline import run_check, target_from_pr
from upgrade_impact.providers.fake import FakeChat
from upgrade_impact.sources.github import GitHubClient, parse_pr_url, token_from_env
from upgrade_impact.sources.registry import RegistryClient

live = pytest.mark.skipif(
    not (os.environ.get("UPGRADE_IMPACT_LIVE") or os.environ.get("UPGRADE_IMPACT_LIVE_GITHUB")),
    reason="UPGRADE_IMPACT_LIVE not set",
)
live_github = pytest.mark.skipif(
    not os.environ.get("UPGRADE_IMPACT_LIVE_GITHUB"), reason="UPGRADE_IMPACT_LIVE_GITHUB not set"
)


@pytest.mark.live
@live
def test_httpx_notes_from_the_real_changelog() -> None:
    registry, github = RegistryClient(), GitHubClient(token=token_from_env(os.environ))
    info = registry.package(Ecosystem.PYPI, "httpx")
    bump = Bump(
        ecosystem=Ecosystem.PYPI,
        package="httpx",
        from_version="0.27.0",
        to_version="0.28.1",
        source="flags",
    )
    notes = collect_notes(bump, info, github)
    assert {"0.28.0", "0.28.1"} <= set(notes.versions)
    assert any("`proxies` argument" in line.text for line in notes.lines)


# A merged Dependabot PR in a public repository: httpx 0.27 -> 0.28.1.
DEMO_PR = "https://github.com/cereja-project/govbr_auth/pull/53"


@pytest.mark.live
@live_github
def test_check_a_real_dependabot_pr(tmp_path: Path) -> None:
    ref = parse_pr_url(DEMO_PR)
    assert ref is not None
    github = GitHubClient(token=token_from_env(os.environ))
    registry = RegistryClient()
    target = target_from_pr(ref, github, None)
    [bump] = target.bumps
    assert (bump.package.lower(), bump.to_version) == ("httpx", "0.28.1")
    report = run_check(
        target,
        chat=FakeChat(),
        registry=registry,
        github=github,
        settings=Settings(cache_dir=str(tmp_path)),
        env={},
    )
    [result] = report.bumps
    assert "0.28.0" in result.notes_versions
    assert result.index.files_importing > 0
    assert result.results, "the fake extractor should find the removals in the 0.28.0 notes"
