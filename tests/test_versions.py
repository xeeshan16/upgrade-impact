from __future__ import annotations

import pytest

from upgrade_impact.versions import Ver, find_version, in_range, parse, version_from_spec


def test_ordering_semver_and_pep440() -> None:
    assert Ver("0.27.2") < Ver("0.28.0") < Ver("v0.28.1")
    assert Ver("1.2.3-beta.1") < Ver("1.2.3")
    assert Ver("1.2.3-beta.1").is_prerelease
    assert Ver("2.0") == Ver("v2.0.0")
    assert len({Ver("2.0"), Ver("2.0.0")}) == 1
    assert Ver("1.0.0-canary.20241105.abc").valid is False
    assert Ver("1.0.0-canary.20241105.abc").release[:3] == (1, 0, 0)


def test_in_range() -> None:
    assert in_range("0.28.0", "0.27.0", "0.28.1")
    assert in_range("0.28.1", "0.27.0", "0.28.1")
    assert not in_range("0.27.0", "0.27.0", "0.28.1")
    assert not in_range("0.29.0", "0.27.0", "0.28.1")
    assert not in_range("0.28.1rc1", "0.27.0", "0.28.1")
    assert in_range("2.0.0rc1", "1.0.0", "2.0.0rc2")
    assert in_range("5.0.1", None, "5.0.1")
    assert not in_range("5.0.0", None, "5.0.1")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("==0.27.0", "0.27.0"),
        (">=0.27,<0.29", "0.27"),
        ("^4.21.2", "4.21.2"),
        ("~1.2", "1.2"),
        ("^1.2.0 || ^2.0.0", "2.0.0"),
        ("1.4.*", "1.4"),
        ("<3", None),
        ("latest", None),
        ("v5", "5"),
    ],
)
def test_version_from_spec(spec: str, expected: str | None) -> None:
    assert version_from_spec(spec) == expected


def test_find_version_and_parse() -> None:
    assert find_version("## 0.28.0 (28th November, 2024)") == "0.28.0"
    assert find_version("Version 8.1.0") == "8.1.0"
    assert find_version("@scope/pkg@1.2.3") == "1.2.3"
    assert find_version("Unreleased") is None
    assert parse(None) is None
    assert parse("main") is None
    assert parse("1.0") == Ver("1.0")
