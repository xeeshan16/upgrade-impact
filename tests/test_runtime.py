from __future__ import annotations

from pathlib import Path

import pytest

from upgrade_impact.models import (
    Claim,
    ClaimKind,
    ClaimResult,
    ClaimStatus,
    Ecosystem,
    Evidence,
    NotesSection,
    ReleaseNotes,
)
from upgrade_impact.runtime import (
    Requirement,
    apply_runtime,
    check_requirement,
    note_requirement,
    project_floor,
    runtime_dismissals,
    spec_floor,
)


@pytest.mark.parametrize(
    ("spec", "floor"),
    [
        (">= 20", (20,)),
        (">=18.0.0", (18, 0, 0)),
        ("^18 || ^20", (18,)),
        ("20.x", (20,)),
        ("~3.9", (3, 9)),
        (">=3.9,<4", (3, 9)),
        ("^3.10", (3, 10)),
        (">3.8", (3, 9)),
        ("*", None),
        ("<4", None),
    ],
)
def test_spec_floor(spec: str, floor: tuple[int, ...] | None) -> None:
    assert spec_floor(spec) == floor


@pytest.mark.parametrize(
    ("text", "ecosystem", "expected"),
    [
        # eventsource 4.0.0 (real CHANGELOG lines)
        (
            "- Drop support for Node.js v18, as it is end-of-life.",
            Ecosystem.NPM,
            Requirement("node", above=(18,)),
        ),
        (
            "- require node.js v20 or higher ([91a3a48](https://github.com/x/y/commit/91a3a48))",
            Ecosystem.NPM,
            Requirement("node", at_least=(20,)),
        ),
        ("drop node 14/16 support", Ecosystem.NPM, Requirement("node", above=(16,))),
        (
            "BREAKING: Node.js 18+ is now required",
            Ecosystem.NPM,
            Requirement("node", at_least=(18,)),
        ),
        (
            "Dropped support for Python 3.7 and 3.8.",
            Ecosystem.PYPI,
            Requirement("python", above=(3, 8)),
        ),
        ("Python 3.8 is no longer supported", Ecosystem.PYPI, Requirement("python", above=(3, 8))),
        (
            "The minimum supported Python version is now 3.10",
            Ecosystem.PYPI,
            Requirement("python", at_least=(3, 10)),
        ),
        ("Requires Python >= 3.9", Ecosystem.PYPI, Requirement("python", at_least=(3, 9))),
        ("Add support for Python 3.13", Ecosystem.PYPI, None),
        ("Remove sinon.defaultConfig and related modules", Ecosystem.NPM, None),
        ("Bump node from 18 to 20 in CI", Ecosystem.NPM, None),
        ("Removed Python 2 compatibility shims", Ecosystem.PYPI, None),
    ],
)
def test_note_requirement(text: str, ecosystem: Ecosystem, expected: Requirement | None) -> None:
    assert note_requirement(text, ecosystem) == expected


def test_requirement_is_compared_at_the_precision_the_note_uses() -> None:
    dropped_18 = Requirement("node", above=(18,))
    assert dropped_18.satisfied_by((20,))
    assert not dropped_18.satisfied_by((18, 17))  # every 18.x is dropped
    needs_310 = Requirement("python", at_least=(3, 10))
    assert needs_310.satisfied_by((3, 10))
    assert not needs_310.satisfied_by((3, 9))
    assert needs_310.satisfied_by((3, 12, 1))


def test_node_floor_from_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{\n  "name": "x",\n  "engines": {\n    "node": ">= 20"\n  }\n}\n', encoding="utf-8"
    )
    floor = project_floor(tmp_path, Ecosystem.NPM)
    assert floor is not None
    assert (floor.version, floor.path, floor.line) == ((20,), "package.json", 4)


def test_python_floor_sources(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.10"\n', encoding="utf-8"
    )
    floor = project_floor(tmp_path, Ecosystem.PYPI)
    assert floor is not None
    assert (floor.version, floor.line) == ((3, 10), 3)

    poetry = tmp_path / "poetry"
    poetry.mkdir()
    (poetry / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\n\n[tool.poetry.dependencies]\npython = "~3.9"\n',
        encoding="utf-8",
    )
    floor = project_floor(poetry, Ecosystem.PYPI)
    assert floor is not None
    assert (floor.version, floor.line) == ((3, 9), 5)

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "setup.py").write_text(
        "from setuptools import setup\n\nsetup(name='x', python_requires='>=3.8')\n",
        encoding="utf-8",
    )
    floor = project_floor(legacy, Ecosystem.PYPI)
    assert floor is not None
    assert (floor.version, floor.path, floor.line) == ((3, 8), "setup.py", 3)

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "setup.cfg").write_text("[options]\npython_requires = >=3.11\n", encoding="utf-8")
    floor = project_floor(cfg, Ecosystem.PYPI)
    assert floor is not None
    assert (floor.version, floor.line) == ((3, 11), 2)


def test_no_declared_floor(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    assert project_floor(tmp_path, Ecosystem.NPM) is None
    assert project_floor(tmp_path, Ecosystem.PYPI) is None
    check = check_requirement(Requirement("node", above=(18,)), None)
    assert check.satisfied is None
    assert "declares no node version" in check.reason


NOTES = ReleaseNotes.from_sections(
    "eventsource",
    [
        NotesSection(
            version="4.0.0",
            source="changelog",
            lines=[
                "- `FetchLikeInit` is now removed. Use `EventSourceFetchInit`.",
                "- Drop support for Node.js v18, as it is end-of-life.",
            ],
        )
    ],
)


def _runtime_result() -> ClaimResult:
    claim = Claim(
        id="C1",
        kind=ClaimKind.DROPPED_SUPPORT,
        summary="Node.js 18 is no longer supported",
        evidence=[Evidence(line=2, quote="Drop support for Node.js v18", verified=True)],
        verified=True,
    )
    return ClaimResult(claim=claim, status=ClaimStatus.NEEDS_REVIEW)


def test_apply_runtime_clears_a_claim_the_floor_meets(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=20"}}', encoding="utf-8")
    result = _runtime_result()
    apply_runtime([result], NOTES, project_floor(tmp_path, Ecosystem.NPM), Ecosystem.NPM)
    assert result.status == ClaimStatus.NOT_FOUND
    assert result.runtime is not None
    assert result.runtime.satisfied is True
    assert result.runtime.path == "package.json"


def test_apply_runtime_keeps_review_when_the_floor_is_too_low(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=18"}}', encoding="utf-8")
    result = _runtime_result()
    apply_runtime([result], NOTES, project_floor(tmp_path, Ecosystem.NPM), Ecosystem.NPM)
    assert result.status == ClaimStatus.NEEDS_REVIEW
    assert result.runtime is not None
    assert result.runtime.satisfied is False
    assert "still allows node >=18" in result.runtime.reason


def test_apply_runtime_ignores_claims_that_name_an_api(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=20"}}', encoding="utf-8")
    result = _runtime_result()
    result.claim.symbols = ["EventSource"]
    apply_runtime([result], NOTES, project_floor(tmp_path, Ecosystem.NPM), Ecosystem.NPM)
    assert result.status == ClaimStatus.NEEDS_REVIEW
    assert result.runtime is None


def test_runtime_dismissals_of_unclaimed_lines(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=20"}}', encoding="utf-8")
    floor = project_floor(tmp_path, Ecosystem.NPM)
    dismissed = runtime_dismissals(NOTES, [1, 2], floor, Ecosystem.NPM)
    assert [d.line.n for d in dismissed] == [2]
    assert dismissed[0].by == "runtime"
    assert runtime_dismissals(NOTES, [1, 2], None, Ecosystem.NPM) == []
