from __future__ import annotations

import pytest

from upgrade_impact.bump import (
    BumpError,
    FileDiff,
    bumps_from_manifest,
    infer_ecosystem,
    parse_body_table,
    parse_title,
    patch_lines,
    resolve_bumps,
    split_unified_diff,
)
from upgrade_impact.models import Ecosystem


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bump httpx from 0.27.0 to 0.28.1", ("httpx", "0.27.0", "0.28.1")),
        ("chore(deps): bump httpx from 0.27 to 0.28.1", ("httpx", "0.27", "0.28.1")),
        (
            "build(deps-dev): bump vitest from 1.6.0 to 2.1.8 in /web",
            ("vitest", "1.6.0", "2.1.8"),
        ),
        (
            "Bump: Update httpx requirement from >=0.27 to >=0.28.1",
            ("httpx", "0.27", "0.28.1"),
        ),
        (
            "Update pydantic requirement from <2,>=1.10 to >=1.10,<3 in /backend",
            ("pydantic", "1.10", "1.10"),
        ),
        ("fix(deps): update dependency express to v5", ("express", None, "5")),
        (
            "chore(deps): update dependency @types/node to v22.10.1",
            ("@types/node", None, "22.10.1"),
        ),
        ("Update dependency fastapi to ^0.115.0 [SECURITY]", ("fastapi", None, "0.115.0")),
        ("Update ruff to 0.8.0", ("ruff", None, "0.8.0")),
        ("[SKIP CI] [pre-commit] bump httpx from 0.27 to 0.28.1", ("httpx", "0.27", "0.28.1")),
        ("Bump `httpx` from ~=0.27 to >=0.27,<0.29", ("httpx", "0.27", "0.27")),
    ],
)
def test_parse_title(title: str, expected: tuple[str, str | None, str]) -> None:
    assert parse_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Bump the pip group across 1 directory with 3 updates",
        "Fix flaky test",
        "Update README.md",
    ],
)
def test_titles_without_a_single_bump(title: str) -> None:
    assert parse_title(title) is None


def test_renovate_body_table() -> None:
    body = """This PR contains the following updates:

| Package | Change | Age | Confidence |
|---|---|---|---|
| [express](https://expressjs.com/) ([source](https://github.com/expressjs/e)) | [`4.21.2` -> `5.0.1`](https://renovatebot.com/diffs/npm/express/4.21.2/5.0.1) |
| [@types/express](https://github.com/DefinitelyTyped) | `4.17.21` -> `5.0.0` | | |
"""
    assert parse_body_table(body) == [
        ("express", "4.21.2", "5.0.1"),
        ("@types/express", "4.17.21", "5.0.0"),
    ]


def test_requirements_diff() -> None:
    bumps = bumps_from_manifest(
        "requirements/base.txt",
        ["httpx==0.27.0", "rich>=13  # pretty output"],
        ["httpx==0.28.1", "rich>=13  # pretty output"],
    )
    assert [(b.package, b.from_version, b.to_version) for b in bumps] == [
        ("httpx", "0.27.0", "0.28.1")
    ]
    assert bumps[0].ecosystem == Ecosystem.PYPI
    assert bumps[0].source == "manifest-diff"


def test_pyproject_pep621_and_poetry() -> None:
    pep621 = bumps_from_manifest(
        "pyproject.toml",
        ['    "httpx>=0.27,<0.28",', '    "Pydantic[email]~=1.10",'],
        ['    "httpx>=0.28.1,<0.29",', '    "pydantic[email]~=2.9",'],
    )
    assert sorted((b.package, b.from_version, b.to_version) for b in pep621) == [
        ("httpx", "0.27", "0.28.1"),
        ("pydantic", "1.10", "2.9"),
    ]
    poetry = bumps_from_manifest(
        "backend/pyproject.toml",
        ['fastapi = "^0.110.0"', 'sqlalchemy = { version = "^1.4", extras = ["asyncio"] }'],
        ['fastapi = "^0.115.2"', 'sqlalchemy = { version = "^2.0", extras = ["asyncio"] }'],
    )
    assert sorted((b.package, b.from_version, b.to_version) for b in poetry) == [
        ("fastapi", "0.110.0", "0.115.2"),
        ("sqlalchemy", "1.4", "2.0"),
    ]


def test_package_json_diff_ignores_own_version_and_scripts() -> None:
    bumps = bumps_from_manifest(
        "web/package.json",
        ['  "version": "1.0.0",', '    "express": "^4.21.2",', '    "build": "tsc -p ."'],
        ['  "version": "1.0.1",', '    "express": "^5.0.1",', '    "build": "tsc -b ."'],
    )
    assert [(b.package, b.from_version, b.to_version, b.ecosystem) for b in bumps] == [
        ("express", "4.21.2", "5.0.1", Ecosystem.NPM)
    ]


def test_lock_files_are_ignored() -> None:
    assert bumps_from_manifest("poetry.lock", ['version = "1"'], ['version = "2"']) == []


def test_split_unified_diff_git_and_plain() -> None:
    text = """diff --git a/requirements.txt b/requirements.txt
index 1..2 100644
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,3 +1,3 @@
 click==8.1.7
-httpx==0.27.0
+httpx==0.28.1
 rich==13.9.4
diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -2,2 +2,2 @@
-    "express": "^4.21.2",
+    "express": "^5.0.1",
     "zod": "^3.23.8"
"""
    files = split_unified_diff(text)
    assert [f.path for f in files] == ["requirements.txt", "package.json"]
    assert files[0].removed == ["httpx==0.27.0"]
    assert files[1].added == ['    "express": "^5.0.1",']

    plain = """--- old/requirements.txt\t2024-01-01
+++ new/requirements.txt\t2024-01-02
@@ -1 +1 @@
-httpx==0.27.0
+httpx==0.28.1
--- a/requirements-dev.txt
+++ b/requirements-dev.txt
@@ -1 +1 @@
--pytest==8.0.0
+-pytest==8.3.0
"""
    files = split_unified_diff(plain)
    assert [f.path for f in files] == ["new/requirements.txt", "requirements-dev.txt"]
    assert files[1].removed == ["-pytest==8.0.0"]


def test_patch_lines() -> None:
    removed, added = patch_lines("@@ -1,2 +1,2 @@\n-httpx==0.27.0\n+httpx==0.28.1\n click\n")
    assert (removed, added) == (["httpx==0.27.0"], ["httpx==0.28.1"])


def test_infer_ecosystem() -> None:
    assert infer_ecosystem([], "dependabot/npm_and_yarn/express-5.0.1") == Ecosystem.NPM
    assert infer_ecosystem([], "dependabot/pip/httpx-0.28.1") == Ecosystem.PYPI
    assert infer_ecosystem(["web/package-lock.json"]) == Ecosystem.NPM
    assert infer_ecosystem(["uv.lock", "pyproject.toml"]) == Ecosystem.PYPI
    assert infer_ecosystem(["package.json", "requirements.txt"]) is None
    assert infer_ecosystem(["README.md"]) is None


def test_resolve_prefers_manifest_and_fills_from_title() -> None:
    files = [FileDiff("requirements.txt", ["httpx==0.27.0"], ["httpx==0.28.1"])]
    bumps = resolve_bumps(title="Bump httpx from 0.26 to 0.28.1", files=files)
    assert len(bumps) == 1
    assert (bumps[0].from_version, bumps[0].source) == ("0.27.0", "manifest-diff")

    # Lock-file-only Dependabot PR: the title is the only source.
    lock_only = resolve_bumps(
        title="Bump idna from 3.6 to 3.7",
        files=[FileDiff("poetry.lock", [], [])],
    )
    assert [(b.package, b.from_version, b.to_version, b.source) for b in lock_only] == [
        ("idna", "3.6", "3.7", "title")
    ]

    renovate = resolve_bumps(
        title="fix(deps): update dependency express to v5",
        body="| [express](https://x) | `4.21.2` -> `5.0.1` |",
        head_ref="renovate/express-5.x",
        files=[FileDiff("package-lock.json", [], [])],
    )
    assert [(b.package, b.from_version, b.to_version) for b in renovate] == [
        ("express", "4.21.2", "5.0.1")
    ]


def test_resolve_needs_an_ecosystem() -> None:
    with pytest.raises(BumpError, match="--ecosystem"):
        resolve_bumps(title="Bump foo from 1.0 to 2.0")
    bumps = resolve_bumps(title="Bump foo from 1.0 to 2.0", ecosystem=Ecosystem.PYPI)
    assert bumps[0].ecosystem == Ecosystem.PYPI
    scoped = resolve_bumps(title="Bump @babel/core from 7.25.0 to 7.26.0")
    assert scoped[0].ecosystem == Ecosystem.NPM


def test_explicit_ecosystem_filters_manifest_bumps() -> None:
    files = [
        FileDiff("requirements.txt", ["httpx==0.27.0"], ["httpx==0.28.1"]),
        FileDiff("package.json", ['"express": "^4.0.0",'], ['"express": "^5.0.0",']),
    ]
    assert [b.package for b in resolve_bumps(files=files, ecosystem=Ecosystem.NPM)] == ["express"]
    assert len(resolve_bumps(files=files)) == 2
