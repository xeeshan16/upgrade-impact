from __future__ import annotations

import pytest

from upgrade_impact.bump import (
    BumpError,
    FileDiff,
    bumps_from_manifest,
    infer_ecosystem,
    is_group_title,
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


DEPENDABOT_GROUP_LINES = """Bumps the pip group with 2 updates in the / directory: \
[httpx](https://github.com/encode/httpx) and [pydantic](https://github.com/pydantic/pydantic).

Updates `httpx` from 0.27.2 to 0.28.0
<details>
<summary>Release notes</summary>
<blockquote>
Updates `h11` from 0.14.0 to 0.16.0
| [anyio](https://github.com/agronholm/anyio) | `3.7.1` | `4.0.0` |
</blockquote>
</details>
<details>
<summary>Commits</summary>
<ul><li>Version 0.28.0</li></ul>
</details>
<br />

Updates `pydantic` from 2.9.2 to 2.10.0
<details>
<summary>Changelog</summary>
</details>
"""

DEPENDABOT_GROUP_TABLE = """Bumps the npm_and_yarn group with 3 updates in the / directory:

| Package | From | To |
| --- | --- | --- |
| [@babel/core](https://github.com/babel/babel/tree/HEAD/packages/babel-core) | `7.24.0` | `7.26.0` |
| [vite](https://github.com/vitejs/vite/tree/HEAD/packages/vite) | `5.4.11` | `6.0.1` |
| [typescript](https://github.com/microsoft/TypeScript) | `5.6.3` | `5.7.2` |
"""


def test_dependabot_group_updates_lines_skip_quoted_notes() -> None:
    # The h11 and anyio lines sit inside the quoted release notes: not this PR's bumps.
    assert parse_body_table(DEPENDABOT_GROUP_LINES) == [
        ("httpx", "0.27.2", "0.28.0"),
        ("pydantic", "2.9.2", "2.10.0"),
    ]


def test_dependabot_group_table() -> None:
    assert parse_body_table(DEPENDABOT_GROUP_TABLE) == [
        ("@babel/core", "7.24.0", "7.26.0"),
        ("vite", "5.4.11", "6.0.1"),
        ("typescript", "5.6.3", "5.7.2"),
    ]


@pytest.mark.parametrize(
    "title",
    [
        "Bump the pip group across 1 directory with 3 updates",
        "chore(deps): bump the npm_and_yarn group in /web with 12 updates",
        "Bump the minor-and-patch group with 1 update",
    ],
)
def test_group_titles(title: str) -> None:
    assert is_group_title(title)
    assert not is_group_title("Bump httpx from 0.27.0 to 0.28.1")


def test_grouped_pr_gives_one_bump_per_package() -> None:
    bumps = resolve_bumps(
        title="Bump the pip group across 1 directory with 2 updates",
        body=DEPENDABOT_GROUP_LINES,
        files=[FileDiff("requirements.txt", ["httpx==0.27.2"], ["httpx==0.28.0"])],
        head_ref="dependabot/pip/pip-abc123",
    )
    # httpx from the manifest diff, pydantic (bumped in a lock file only) from the body.
    assert [(b.package, b.from_version, b.to_version, b.source) for b in bumps] == [
        ("httpx", "0.27.2", "0.28.0", "manifest-diff"),
        ("pydantic", "2.9.2", "2.10.0", "pr-body"),
    ]
    assert {b.ecosystem for b in bumps} == {Ecosystem.PYPI}


def test_grouped_pr_with_no_packages_found_says_so() -> None:
    with pytest.raises(BumpError, match="grouped update"):
        resolve_bumps(
            title="Bump the pip group with 2 updates",
            body="Bumps the pip group with 2 updates.",
            files=[FileDiff("uv.lock", [], [])],
        )


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
