from __future__ import annotations

import json
from pathlib import Path

import httpx

from upgrade_impact.models import Bump, Ecosystem, NotesSection, ReleaseNotes
from upgrade_impact.notes import (
    changelog_sections,
    chunk_notes,
    clean_lines,
    collect_notes,
    is_trivial,
    tag_version,
)
from upgrade_impact.sources.github import GitHubClient
from upgrade_impact.sources.registry import PackageInfo

BUMP = Bump(
    ecosystem=Ecosystem.PYPI,
    package="httpx",
    from_version="0.27.0",
    to_version="0.28.1",
    source="flags",
)


def test_httpx_changelog_sections(fixtures: Path) -> None:
    text = (fixtures / "notes" / "httpx-CHANGELOG.md").read_text()
    sections = changelog_sections(text, "0.27.0", "0.28.1")
    assert [v for v, _ in sections] == ["0.27.1", "0.27.2", "0.28.0", "0.28.1"]
    notes_028 = dict(sections)["0.28.0"]
    assert "* The deprecated `proxies` argument has now been removed." in notes_028
    # The unreleased 1.0.0 section and 0.27.0 itself are outside the range.
    flat = [line for _, lines in sections for line in lines]
    assert not any("FunctionAuth" in line for line in flat)


def test_rst_style_changelog(fixtures: Path) -> None:
    text = (fixtures / "notes" / "requests-HISTORY.md").read_text()
    sections = changelog_sections(text, "2.33.1", "2.34.2")
    assert [v for v, _ in sections] == ["2.34.0", "2.34.1", "2.34.2"]
    assert all(not line.startswith("---") for _, lines in sections for line in lines)


def test_unknown_start_takes_only_the_target() -> None:
    text = "# Changes\n\n## 2.0.0\n\n- Removed `foo`.\n\n## 1.0.0\n\n- First.\n"
    assert changelog_sections(text, None, "2.0.0") == [("2.0.0", ["- Removed `foo`."])]


def test_clean_lines_drops_noise() -> None:
    body = (
        "<!-- Release notes generated using configuration in .github/release.yml -->\n"
        "## What's Changed\n\n"
        "* Drop Python 3.8 by @someone in https://github.com/o/r/pull/1\n"
        "<details>\n<summary>Commits</summary>\n"
        "* @newbie made their first contribution in https://github.com/o/r/pull/2\n"
        "**Full Changelog**: https://github.com/o/r/compare/v1...v2\n" + "x" * 500
    )
    lines = clean_lines(body)
    assert lines[:2] == [
        "## What's Changed",
        "* Drop Python 3.8 by @someone in https://github.com/o/r/pull/1",
    ]
    assert lines[2].endswith("[+100 chars]")
    assert len(lines) == 3


def test_is_trivial() -> None:
    assert is_trivial([])
    assert is_trivial(["See CHANGELOG.md for details."])
    assert not is_trivial(["- Removed the `proxies` argument.", "- Dropped Python 3.8."])


def test_tag_version_and_monorepos() -> None:
    assert tag_version("0.28.1", "httpx") == "0.28.1"
    assert tag_version("v5.0.1", "express") == "5.0.1"
    assert tag_version("release-2.0.0", "foo") == "2.0.0"
    assert tag_version("@babel/core@7.26.0", "@babel/core") == "7.26.0"
    assert tag_version("pydantic-core-v2.27.1", "pydantic-core") == "2.27.1"
    assert tag_version("@babel/parser@7.26.0", "@babel/core") is None
    assert tag_version("nightly", "x") is None


def _github(handler) -> GitHubClient:  # type: ignore[no-untyped-def]
    return GitHubClient(transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_collect_prefers_releases_and_fills_from_changelog(fixtures: Path) -> None:
    changelog = (fixtures / "notes" / "httpx-CHANGELOG.md").read_text()
    releases = [
        {
            "tag_name": "0.28.0",
            "html_url": "https://github.com/encode/httpx/releases/tag/0.28.0",
            "body": "* The deprecated `proxies` argument has now been removed.\n"
            "* The deprecated `app` argument has now been removed.",
        },
        {"tag_name": "0.28.1", "body": "See CHANGELOG."},
        {"tag_name": "0.26.0", "body": "* Old stuff that is out of range."},
        {"tag_name": "0.29.0rc1", "body": "* A pre-release."},
        {"tag_name": "0.27.2", "draft": True, "body": "* Draft."},
    ]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/repos/encode/httpx/releases":
            return httpx.Response(200, json=releases)
        if request.url.path == "/encode/httpx/HEAD/CHANGELOG.md":
            return httpx.Response(200, text=changelog)
        return httpx.Response(404)

    info = PackageInfo(
        ecosystem=Ecosystem.PYPI,
        name="httpx",
        repository=("encode", "httpx"),
        versions=["0.26.0", "0.27.0", "0.27.1", "0.27.2", "0.28.0", "0.28.1"],
    )
    notes = collect_notes(BUMP, info, _github(handler))
    assert notes.versions == ["0.27.1", "0.27.2", "0.28.0", "0.28.1"]
    sources = {s.version: s.source for s in notes.sections}
    assert sources == {
        "0.27.1": "changelog",
        "0.27.2": "changelog",
        "0.28.0": "github-release",
        "0.28.1": "changelog",
    }
    assert notes.repository == "encode/httpx"
    assert [line.n for line in notes.lines] == list(range(1, len(notes.lines) + 1))
    assert notes.warnings == []


def test_collect_falls_back_to_pr_body_and_warns() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    info = PackageInfo(ecosystem=Ecosystem.PYPI, name="x", repository=("o", "x"))
    notes = collect_notes(
        BUMP, info, _github(handler), pr_body="Release notes\n- Removed `Client.foo`."
    )
    assert [s.source for s in notes.sections] == ["pr-body"]
    assert any("PR body" in w for w in notes.warnings)
    assert any("could not list GitHub releases" in w for w in notes.warnings)

    nothing = collect_notes(BUMP, None, _github(handler))
    assert nothing.lines == []
    assert any("no GitHub repository" in w for w in nothing.warnings)
    assert any("no release notes found" in w for w in nothing.warnings)


def test_chunking_respects_budget_and_version_boundaries() -> None:
    sections = [
        NotesSection(
            version=f"1.{i}.0",
            source="changelog",
            lines=[f"- change {i}.{j} " * 8 for j in range(10)],
        )
        for i in range(4)
    ]
    notes = ReleaseNotes.from_sections("pkg", sections)
    chunks = chunk_notes(notes, budget_tokens=500)
    assert sum(len(c) for c in chunks) == len(notes.lines)
    assert all(c == sorted(c) for c in chunks)
    # Each section is about 300 tokens, so sections are never split here.
    for chunk in chunks:
        assert len({notes.lines[n - 1].version for n in chunk}) == 1
    big = chunk_notes(notes, budget_tokens=100)
    assert len(big) > len(chunks)


def test_release_notes_json_round_trip() -> None:
    notes = ReleaseNotes.from_sections(
        "pkg", [NotesSection(version="2.0", source="changelog", lines=["- a", "- b"])]
    )
    again = ReleaseNotes.model_validate(json.loads(notes.model_dump_json()))
    assert again.line(2) is not None
    assert again.line(3) is None


def test_body_for_package_slices_a_grouped_body() -> None:
    from upgrade_impact.notes import body_for_package

    body = (
        "Bumps the pip group with 2 updates: [a](x) and [b](y).\n\n"
        "Updates `a` from 1.0 to 2.0\n<details><blockquote>a removed `foo`</blockquote></details>\n"
        "Updates `b` from 3.0 to 4.0\n<details><blockquote>b renamed `bar`</blockquote></details>\n"
    )
    assert "a removed" in body_for_package(body, "A")
    assert "b renamed" not in body_for_package(body, "a")
    assert "b renamed" in body_for_package(body, "b")
    assert "Updates `b`" not in body_for_package(body, "b")
    assert body_for_package(body, "c") == ""
    single = "Bumps [a](x) from 1.0 to 2.0.\n<details>notes</details>"
    assert body_for_package(single, "a") == single
