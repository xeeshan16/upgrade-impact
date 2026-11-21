"""Work out which dependency moved, from where to where.

Three sources, most precise first:

1. the manifest diff (``requirements*.txt``, ``pyproject.toml``, ``Pipfile``,
   ``package.json``): exact names and the old and new specifiers;
2. the Renovate PR-body table (``| [pkg](...) | `1.0.0` -> `2.0.0` |``);
3. the PR title (Dependabot ``Bump X from A to B``, Renovate ``Update dependency X to vB``).

Lock files are ignored: they also list transitive dependencies, which would
turn one bump into dozens.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from upgrade_impact.models import Bump, Ecosystem
from upgrade_impact.versions import version_from_spec

_CONVENTIONAL_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)*(?:[\w-]+(?:\([^)]*\))?!?:\s*)?")

_TITLE_PATTERNS = [
    re.compile(
        r"^bump (?P<pkg>\S+) from (?P<from>\S+) to (?P<to>\S+?)(?: in (?P<dir>\S+))?$", re.I
    ),
    re.compile(
        r"^update (?P<pkg>\S+) requirement from (?P<from>.+?) to (?P<to>.+?)(?: in (?P<dir>\S+))?$",
        re.I,
    ),
    re.compile(r"^upgrade (?P<pkg>\S+) from (?P<from>\S+) to (?P<to>\S+)$", re.I),
    re.compile(
        r"^update (?:dependency|module|package) (?P<pkg>\S+) to (?P<to>\S+?)"
        r"(?: \[security\])?$",
        re.I,
    ),
    re.compile(r"^update (?P<pkg>[@\w./-]+) to (?P<to>v?\d\S*?)(?: \[security\])?$", re.I),
]

_BODY_CHANGE = re.compile(
    r"`?(?P<from>v?\d[^`|\s]*)`?\s*(?:->|→|&#8594;)\s*`?(?P<to>v?\d[^`|\s\]]*)`?"
)
_BODY_NAME = re.compile(r"^\s*\[?(?P<name>@?[\w./-]+)")

REQUIREMENTS_FILE = re.compile(
    r"(?:^|/)(?:(?:requirements|constraints)[^/]*|requirements/[^/]+)\.(?:txt|in)$", re.I
)
PYPROJECT_FILE = re.compile(r"(?:^|/)(?:pyproject\.toml|Pipfile|setup\.cfg)$")
PACKAGE_JSON = re.compile(r"(?:^|/)package\.json$")
NPM_HINTS = re.compile(r"(?:^|/)(?:package(?:-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|\.npmrc)$")
PYPI_HINTS = re.compile(
    r"(?:^|/)(?:requirements[^/]*\.(?:txt|in)|pyproject\.toml|poetry\.lock|uv\.lock|Pipfile"
    r"(?:\.lock)?|setup\.(?:py|cfg))$",
    re.I,
)

_PACKAGE_JSON_ENTRY = re.compile(r'^\s*"(?P<name>@?[^"\s]+)"\s*:\s*"(?P<spec>[^"]*)"\s*,?\s*$')
_NPM_SPEC = re.compile(r"^(?:npm:[^@]+@)?[\^~>=<\s]*v?\d")
_PACKAGE_JSON_NON_DEPS = frozenset(
    {"version", "name", "main", "module", "types", "typings", "node", "npm", "engines"}
)
_POETRY_ENTRY = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][\w.-]*)\s*=\s*(?:\"(?P<spec>[^\"]+)\"|"
    r"\{[^}]*version\s*=\s*\"(?P<spec2>[^\"]+)\")"
)


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


class BumpError(ValueError):
    pass


def _clean_version(raw: str) -> str | None:
    raw = raw.strip().strip("`'\".,")
    return version_from_spec(raw) or None


def _clean_package(raw: str) -> str:
    return raw.strip().strip("`'\"*")


def parse_title(title: str) -> tuple[str, str | None, str] | None:
    """``(package, from_version, to_version)`` from a Dependabot or Renovate title."""
    text = _CONVENTIONAL_PREFIX.sub("", title.strip()).strip()
    for pattern in _TITLE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        to_version = _clean_version(match.group("to"))
        if not to_version:
            continue
        groups = match.groupdict()
        from_version = _clean_version(groups["from"]) if groups.get("from") else None
        return _clean_package(match.group("pkg")), from_version, to_version
    return None


def parse_body_table(body: str) -> list[tuple[str, str, str]]:
    """Renovate's ``| Package | Change |`` rows."""
    rows: list[tuple[str, str, str]] = []
    for line in (body or "").splitlines():
        if not line.startswith("|"):
            continue
        cells = line.strip().strip("|").split("|")
        name = _BODY_NAME.match(cells[0]) if cells else None
        change = _BODY_CHANGE.search("|".join(cells[1:]))
        if not name or not change:
            continue
        start = _clean_version(change.group("from"))
        end = _clean_version(change.group("to"))
        if start and end:
            rows.append((_clean_package(name.group("name")), start, end))
    return rows


@dataclass(frozen=True, slots=True)
class FileDiff:
    path: str
    removed: list[str]
    added: list[str]


def split_unified_diff(text: str) -> list[FileDiff]:
    """Split ``git diff`` / ``diff -u`` output into per-file removed and added lines."""
    files: list[FileDiff] = []
    path: str | None = None
    removed: list[str] = []
    added: list[str] = []
    git_header = False
    old_left = new_left = 0

    def flush() -> None:
        if path is not None:
            files.append(FileDiff(path, list(removed), list(added)))

    for line in text.splitlines():
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                added.append(line[1:])
                new_left -= 1
            elif line.startswith("-"):
                removed.append(line[1:])
                old_left -= 1
            elif not line.startswith("\\"):
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("diff --git "):
            flush()
            removed, added = [], []
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            path = match.group(2) if match else None
            git_header = True
        elif line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            if not git_header:
                flush()
                removed, added = [], []
                path = target[2:] if target.startswith("b/") else target
            git_header = False
        elif line.startswith("@@"):
            hunk = _HUNK.match(line)
            if hunk:
                old_left = int(hunk.group(1) or 1)
                new_left = int(hunk.group(2) or 1)
    flush()
    return files


def patch_lines(patch: str) -> tuple[list[str], list[str]]:
    """Removed and added lines of one GitHub API ``patch`` (hunks only, no file header)."""
    removed: list[str] = []
    added: list[str] = []
    for line in (patch or "").splitlines():
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return removed, added


def _python_entries(lines: Iterable[str], *, poetry_style: bool) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for raw in lines:
        line = raw.split(" #", 1)[0].strip().rstrip("\\").strip()
        if not line or line.startswith(("#", "-", "[")):
            continue
        if poetry_style:
            match = _POETRY_ENTRY.match(line)
            if match and match.group("name").lower() not in ("python", "version", "name"):
                spec = match.group("spec") or match.group("spec2") or ""
                version = version_from_spec(spec)
                if version:
                    name = match.group("name")
                    entries[canonicalize_name(name)] = (name, version)
                    continue
        text = line.strip().strip(",").strip().strip("\"'")
        if "==" not in text and not re.search(r"[<>=~!]", text):
            continue
        try:
            requirement = Requirement(text)
        except InvalidRequirement:
            continue
        version = version_from_spec(str(requirement.specifier))
        if version:
            entries[canonicalize_name(requirement.name)] = (requirement.name, version)
    return entries


def _npm_entries(lines: Iterable[str]) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for line in lines:
        match = _PACKAGE_JSON_ENTRY.match(line)
        if not match:
            continue
        name, spec = match.group("name"), match.group("spec")
        if name in _PACKAGE_JSON_NON_DEPS or not _NPM_SPEC.match(spec):
            continue
        version = version_from_spec(spec.split("@")[-1] if spec.startswith("npm:") else spec)
        if version:
            entries[name] = (name, version)
    return entries


def bumps_from_manifest(path: str, removed: list[str], added: list[str]) -> list[Bump]:
    """Bumps visible in one manifest file's diff."""
    if PACKAGE_JSON.search(path):
        ecosystem = Ecosystem.NPM
        before, after = _npm_entries(removed), _npm_entries(added)
    elif REQUIREMENTS_FILE.search(path):
        ecosystem = Ecosystem.PYPI
        before = _python_entries(removed, poetry_style=False)
        after = _python_entries(added, poetry_style=False)
    elif PYPROJECT_FILE.search(path):
        ecosystem = Ecosystem.PYPI
        before = _python_entries(removed, poetry_style=True)
        after = _python_entries(added, poetry_style=True)
    else:
        return []
    bumps: list[Bump] = []
    for key, (name, new) in after.items():
        old = before.get(key)
        if old is None or old[1] == new:
            continue
        bumps.append(
            Bump(
                ecosystem=ecosystem,
                package=name,
                from_version=old[1],
                to_version=new,
                manifest=path,
                source="manifest-diff",
            )
        )
    return bumps


def infer_ecosystem(paths: Iterable[str], head_ref: str | None = None) -> Ecosystem | None:
    ref = (head_ref or "").lower()
    if "/npm_and_yarn/" in ref or ref.startswith("renovate/npm"):
        return Ecosystem.NPM
    if any(f"/{name}/" in ref for name in ("pip", "pip_compile", "pipenv", "poetry", "uv")):
        return Ecosystem.PYPI
    npm = pypi = False
    for path in paths:
        npm = npm or bool(NPM_HINTS.search(path))
        pypi = pypi or bool(PYPI_HINTS.search(path))
    if npm != pypi:
        return Ecosystem.NPM if npm else Ecosystem.PYPI
    return None


def _key(ecosystem: Ecosystem, package: str) -> tuple[Ecosystem, str]:
    name = canonicalize_name(package) if ecosystem == Ecosystem.PYPI else package.lower()
    return ecosystem, name


def resolve_bumps(
    *,
    title: str | None = None,
    body: str | None = None,
    files: list[FileDiff] | None = None,
    head_ref: str | None = None,
    ecosystem: Ecosystem | None = None,
) -> list[Bump]:
    """Merge the three sources. Manifest diffs win; the title fills gaps."""
    files = files or []
    found: dict[tuple[Ecosystem, str], Bump] = {}
    for diff in files:
        for bump in bumps_from_manifest(diff.path, diff.removed, diff.added):
            if ecosystem and bump.ecosystem != ecosystem:
                continue
            found.setdefault(_key(bump.ecosystem, bump.package), bump)

    guessed = ecosystem or infer_ecosystem([f.path for f in files], head_ref)
    fallbacks: list[tuple[str, str | None, str, str]] = [
        (pkg, start, end, "pr-body") for pkg, start, end in parse_body_table(body or "")
    ]
    parsed_title = parse_title(title) if title else None
    if parsed_title:
        fallbacks.append((*parsed_title, "title"))
    for package, start, end, source in fallbacks:
        eco = guessed
        if eco is None:
            eco = Ecosystem.NPM if package.startswith("@") else None
        if eco is None:
            continue
        key = _key(eco, package)
        existing = found.get(key)
        if existing is None:
            found[key] = Bump(
                ecosystem=eco, package=package, from_version=start, to_version=end, source=source
            )
        elif existing.from_version is None and start:
            found[key] = existing.model_copy(update={"from_version": start})
    if not found and parsed_title and guessed is None:
        raise BumpError(
            f"found {parsed_title[0]!r} in the title but could not tell whether it is a PyPI "
            "or npm package; pass --ecosystem"
        )
    return list(found.values())
