"""Runtime-support changes, checked against the runtime the project declares.

"Drop support for Node.js 18" names no API, so symbol matching can only say
"needs review". But it has an exact answer: a project whose ``package.json``
says ``"engines": {"node": ">= 20"}`` is not affected, and one that still
declares ``>= 18`` has to raise its floor (or stay on the old version).

This module reads the declared floor (``engines.node`` in ``package.json``;
``requires-python`` / Poetry's ``python`` in ``pyproject.toml``,
``python_requires`` in ``setup.cfg`` or ``setup.py``) and the requirement a
note line states, and compares them at the precision the note uses: dropping
"Node 18" means every 18.x, so a floor of ``>=18.17`` is still affected.

Everything here is deterministic and cites the manifest line it read.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from upgrade_impact.models import (
    ClaimResult,
    ClaimStatus,
    DismissedLine,
    Ecosystem,
    ReleaseNotes,
    RuntimeCheck,
)

RUNTIME_NAMES = {Ecosystem.NPM: "node", Ecosystem.PYPI: "python"}
MANIFEST_FILES = ("package.json", "pyproject.toml", "setup.cfg", "setup.py")

_RUNTIME_WORD = {
    "node": r"node(?:\.?js)?",
    "python": r"(?:c?python|py)",
}
_VERSION = r"v?(\d+(?:\.\d+)?)(?:\.x|\.\d+)*"
_LIST_SEP = r"(?:\s*(?:,|/|and|or|&)\s*(?:v|node(?:\.?js)?\s*|python\s*)?)"


@dataclass(frozen=True, slots=True)
class Floor:
    """The lowest runtime version a project says it supports."""

    runtime: str
    version: tuple[int, ...]
    spec: str
    path: str
    line: int


@dataclass(frozen=True, slots=True)
class Requirement:
    """What a note line demands of the runtime.

    ``above`` is the highest version the release dropped (the floor must be
    above it); ``at_least`` is a stated new minimum (the floor must reach it).
    """

    runtime: str
    above: tuple[int, ...] | None = None
    at_least: tuple[int, ...] | None = None

    def describe(self) -> str:
        if self.at_least is not None:
            return f"{self.runtime} >= {_fmt(self.at_least)}"
        assert self.above is not None
        return f"{self.runtime} > {_fmt(self.above)}"

    def satisfied_by(self, floor: tuple[int, ...]) -> bool:
        if self.at_least is not None:
            return _pad(floor, len(self.at_least)) >= self.at_least
        assert self.above is not None
        return floor[: len(self.above)] > self.above


def _fmt(version: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in version)


def _pad(version: tuple[int, ...], size: int) -> tuple[int, ...]:
    return (version + (0,) * size)[:size] if len(version) < size else version[:size]


def _tuple(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in text.split(".") if n.isdigit())


# -- the project's floor -------------------------------------------------------


def spec_floor(spec: str) -> tuple[int, ...] | None:
    """The lowest version a specifier allows: ``>= 20`` -> (20,), ``^18 || ^20`` -> (18,),
    ``~3.9`` -> (3, 9), ``>=3.9,<4`` -> (3, 9). ``None`` without a lower bound."""
    floors: list[tuple[int, ...]] = []
    for alternative in spec.split("||"):
        lows: list[tuple[int, ...]] = []
        for raw in re.split(r"\s*,\s*|\s+(?=[<>=~^!])", alternative.strip()):
            part = raw.strip()
            if not part or part.startswith(("<", "!")):
                continue
            match = re.match(r"^(?:>=|==|===|~=|\^|~|>|=)?\s*v?(\d+(?:\.\d+)*)", part)
            if match:
                version = _tuple(match.group(1))
                if part.startswith(">") and not part.startswith(">="):
                    version = (*version[:-1], version[-1] + 1) if len(version) > 1 else version
                lows.append(version)
        if not lows:
            return None  # one alternative with no lower bound: anything goes
        floors.append(max(lows))
    return min(floors) if floors else None


def _line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return 1


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _node_floor(root: Path) -> Floor | None:
    text = _read(root / "package.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    engines = data.get("engines") if isinstance(data, dict) else None
    spec = engines.get("node") if isinstance(engines, dict) else None
    if not isinstance(spec, str) or (version := spec_floor(spec)) is None:
        return None
    return Floor("node", version, spec, "package.json", _line_of(text, '"node"'))


def _python_floor(root: Path) -> Floor | None:
    text = _read(root / "pyproject.toml")
    if text is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = {}
        project = data.get("project")
        spec = project.get("requires-python") if isinstance(project, dict) else None
        needle = "requires-python"
        if not isinstance(spec, str):
            poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            spec = poetry.get("python") if isinstance(poetry, dict) else None
            needle = "python"
        if isinstance(spec, str) and (version := spec_floor(spec)) is not None:
            line = _line_of(text, needle) if needle != "python" else _poetry_line(text)
            return Floor("python", version, spec, "pyproject.toml", line)
    for name, pattern in (
        ("setup.cfg", r"^\s*python_requires\s*=\s*(.+?)\s*$"),
        ("setup.py", r"python_requires\s*=\s*['\"]([^'\"]+)['\"]"),
    ):
        text = _read(root / name)
        if text is None:
            continue
        match = re.search(pattern, text, re.M)
        if match and (version := spec_floor(match.group(1))) is not None:
            line = text.count("\n", 0, match.start()) + 1
            if name == "setup.cfg":
                line = _line_of(text, "python_requires")
            return Floor("python", version, match.group(1), name, line)
    return None


def _poetry_line(text: str) -> int:
    for number, line in enumerate(text.splitlines(), start=1):
        if re.match(r"^\s*python\s*=", line):
            return number
    return 1


def project_floor(root: Path, ecosystem: Ecosystem) -> Floor | None:
    """The runtime floor the project at ``root`` declares, if it declares one."""
    return _node_floor(root) if ecosystem == Ecosystem.NPM else _python_floor(root)


# -- what the notes require ------------------------------------------------------


def note_requirement(text: str, ecosystem: Ecosystem) -> Requirement | None:
    """The runtime requirement a note line states, or ``None``.

    Recognises "drop(ped) support for Node.js 18 (and 19)", "Python 3.8 is no
    longer supported", "requires Node.js 20 or higher", "minimum supported
    Python version is now 3.10" and "Python >= 3.10 is required".
    """
    runtime = RUNTIME_NAMES[ecosystem]
    word = _RUNTIME_WORD[runtime]
    versions = rf"{_VERSION}(?:{_LIST_SEP}{_VERSION})*"
    lowered = text.lower()

    def _all(span: str) -> list[tuple[int, ...]]:
        return [_tuple(v) for v in re.findall(r"(?<![\d.])v?(\d+(?:\.\d+)?)", span)]

    def _plausible(version: tuple[int, ...]) -> bool:
        if runtime == "python":
            return len(version) >= 2 and version[0] in (2, 3)
        return 4 <= version[0] <= 40

    at_least = re.search(
        rf"(?:requires?|required|minimum(?: supported)?(?: {word})?(?: version)?(?: is)?(?: now)?"
        rf"|supports? only|only supports?)\s+(?:{word}\s*)?(?:version\s+)?"
        rf"(?:>=\s*|v)?{_VERSION}(?:\s*(?:\+|or (?:higher|later|newer|above)|and (?:above|up)))?",
        lowered,
    )
    at_least_alt = re.search(rf"{word}\s*(?:>=|≥)\s*{_VERSION}", lowered) or re.search(
        rf"{word}\s*(?:version\s*)?{_VERSION}\s*(?:\+|or (?:higher|later|newer|above))\s*"
        rf"(?:is|are)?\s*(?:now\s+)?(?:required|needed|the minimum)",
        lowered,
    )
    if (at_least or at_least_alt) and re.search(word, lowered):
        found = at_least or at_least_alt
        assert found is not None
        version = _tuple(found.group(1))
        if _plausible(version):
            return Requirement(runtime, at_least=version)

    dropped = re.search(
        rf"(?:drop(?:ped|s)?|remove[ds]?|end(?:ed)?)\s+(?:support\s+for\s+|the\s+)?"
        rf"{word}\s*(?:version\s*)?({versions})",
        lowered,
    ) or re.search(
        rf"{word}\s*({versions})\s*(?:is|are)\s+no\s+longer\s+supported",
        lowered,
    )
    if dropped:
        candidates = [v for v in _all(dropped.group(1)) if _plausible(v)]
        if candidates:
            return Requirement(runtime, above=max(candidates))
    return None


def check_requirement(requirement: Requirement, floor: Floor | None) -> RuntimeCheck:
    """Compare a note's requirement with the project's declared floor."""
    if floor is None:
        return RuntimeCheck(
            runtime=requirement.runtime,
            required=requirement.describe(),
            satisfied=None,
            reason=f"the project declares no {requirement.runtime} version, so check which "
            "versions you support",
        )
    ok = requirement.satisfied_by(floor.version)
    declared = f"{floor.runtime} {floor.spec.strip()}"
    reason = (
        f"your {floor.path} requires {declared}, which already meets {requirement.describe()}"
        if ok
        else f"your {floor.path} still allows {declared}; the new version needs "
        f"{requirement.describe()}"
    )
    return RuntimeCheck(
        runtime=requirement.runtime,
        required=requirement.describe(),
        declared=floor.spec.strip(),
        path=floor.path,
        line=floor.line,
        satisfied=ok,
        reason=reason,
    )


# -- applying it to a report -----------------------------------------------------


def _claim_text(result: ClaimResult, notes: ReleaseNotes) -> str:
    cited = [
        line.text
        for ev in result.claim.evidence
        if ev.verified and (line := notes.line(ev.line)) is not None
    ]
    return " ".join([*cited, result.claim.summary])


def apply_runtime(
    results: list[ClaimResult], notes: ReleaseNotes, floor: Floor | None, ecosystem: Ecosystem
) -> None:
    """Check claims that name no API against the declared runtime, in place.

    A claim whose requirement the project's floor already meets is not found
    in this project; one it does not meet (or a project that declares no
    floor) stays "needs review", now with the reason.
    """
    for result in results:
        claim = result.claim
        if claim.symbols or claim.parameters:
            continue
        requirement = note_requirement(_claim_text(result, notes), ecosystem)
        if requirement is None:
            continue
        result.runtime = check_requirement(requirement, floor)
        if result.runtime.satisfied:
            result.status = ClaimStatus.NOT_FOUND
            result.matches = []


def runtime_dismissals(
    notes: ReleaseNotes, line_numbers: list[int], floor: Floor | None, ecosystem: Ecosystem
) -> list[DismissedLine]:
    """Unclaimed note lines whose runtime requirement the declared floor already meets."""
    if floor is None:
        return []
    out: list[DismissedLine] = []
    for n in line_numbers:
        line = notes.line(n)
        if line is None:
            continue
        requirement = note_requirement(line.text, ecosystem)
        if requirement is None:
            continue
        check = check_requirement(requirement, floor)
        if check.satisfied:
            out.append(DismissedLine(line=line, by="runtime", reason=check.reason))
    return out
