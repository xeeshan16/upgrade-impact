"""Collect the release notes between two versions.

Per version, a GitHub release body is preferred; a CHANGELOG section fills in
versions whose release is missing or trivial ("See CHANGELOG"); the PR body
(Dependabot embeds truncated notes) is the last resort.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from upgrade_impact.models import Bump, NotesSection, ReleaseNotes
from upgrade_impact.sources.github import GitHubClient, GitHubError
from upgrade_impact.sources.registry import PackageInfo
from upgrade_impact.versions import Ver, find_version, in_range

log = logging.getLogger(__name__)

MAX_LINE_CHARS = 400
MAX_NOTES_LINES = 4000
CHANGELOG_PATHS = (
    "CHANGELOG.md",
    "CHANGES.md",
    "HISTORY.md",
    "NEWS.md",
    "CHANGELOG.rst",
    "CHANGES.rst",
    "HISTORY.rst",
    "NEWS.rst",
    "CHANGELOG",
    "CHANGES",
    "docs/changelog.md",
    "docs/CHANGELOG.md",
    "docs/changelog.rst",
    "docs/changes.rst",
    "docs/history.rst",
    "docs/release-notes.md",
)

_NOISE = re.compile(
    r"(made their first contribution|^\**full changelog\**:|^<!--.*-->$|^</?(details|summary|p|br|"
    r"div|sup|sub|blockquote)\b[^>]*>$|^-{3,}$|^={3,}$)",
    re.I,
)
_TAGS = re.compile(r"</?(?:details|summary|p|br|div|blockquote|sup|sub|em|strong|code|a)\b[^>]*>")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_VERSION_LINE = re.compile(r"^\s*(?:\[|v|version\s+|release\s+)?v?\d+\.\d+", re.I)


def clean_lines(text: str) -> list[str]:
    """Release-note text as non-empty, de-noised lines suitable for citation."""
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    text = re.sub(r"<summary>.*?</summary>", "", text, flags=re.S | re.I)
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = html.unescape(_TAGS.sub("", raw)).rstrip()
        stripped = line.strip()
        if not stripped or (stripped.startswith(">") and not stripped.lstrip("> ").strip()):
            continue
        if _NOISE.search(stripped):
            continue
        if len(line) > MAX_LINE_CHARS:
            line = f"{line[:MAX_LINE_CHARS]} … [+{len(line) - MAX_LINE_CHARS} chars]"
        lines.append(line)
    return lines


def is_trivial(lines: list[str]) -> bool:
    """A body that only points elsewhere ("See CHANGELOG.md")."""
    if not lines:
        return True
    if len(lines) > 2:
        return False
    joined = " ".join(lines)
    words = len(re.findall(r"[A-Za-z]{3,}", joined))
    pointer = re.search(
        r"\b(see|refer to|full|details in)\b.*\b(changelog|changes|history|release notes|news)",
        joined,
        re.I,
    )
    return bool(pointer) or words <= 3


def tag_version(tag: str, package: str) -> str | None:
    """The version in a release tag, or ``None`` when the tag belongs to another package.

    Handles ``1.2.3``, ``v1.2.3``, ``release-1.2.3``, ``pkg@1.2.3``, ``@scope/pkg@1.2.3``
    and ``pkg-v1.2.3``. In a monorepo, a tag naming a different package is skipped.
    """
    version = find_version(tag)
    if not version:
        return None
    prefix = tag[: tag.rfind(version)].lower()
    prefix = re.sub(r"(release|version|v)[-_/@.]*$", "", prefix)
    prefix = re.sub(r"^(release|version|v)[-_/@.]*", "", prefix)
    prefix = re.sub(r"[^a-z0-9]", "", prefix)
    if not prefix:
        return version
    short = re.sub(r"[^a-z0-9]", "", package.lower().rsplit("/", 1)[-1])
    whole = re.sub(r"[^a-z0-9]", "", package.lower())
    if prefix in (short, whole) or short in prefix:
        return version
    return None


@dataclass(frozen=True, slots=True)
class _Heading:
    index: int
    version: str | None
    level: int


def _headings(lines: list[str]) -> list[_Heading]:
    headings: list[_Heading] = []
    for i, line in enumerate(lines):
        md = _HEADING.match(line)
        if md:
            headings.append(_Heading(i, find_version(md.group(2)), len(md.group(1))))
            continue
        if i + 1 < len(lines) and _RST_UNDERLINE.match(lines[i + 1]) and line.strip():
            if not _RST_UNDERLINE.match(line):
                headings.append(_Heading(i, find_version(line), 2))
            continue
        if _VERSION_LINE.match(line) and len(line) < 80 and not line.startswith((" ", "\t", "-")):
            version = find_version(line)
            # "1.2.3 (2024-01-01)" style headings with no markup.
            if version and line.strip().lstrip("[v").startswith(version.lstrip("v")[:3]):
                headings.append(_Heading(i, version, 7))
    return headings


def changelog_sections(text: str, start: str | None, end: str) -> list[tuple[str, list[str]]]:
    """``(version, lines)`` for each changelog section in ``(start, end]``, oldest first."""
    raw = (text or "").replace("\r\n", "\n").split("\n")
    headings = [h for h in _headings(raw) if h.version]
    if not headings:
        return []
    level = min(h.level for h in headings)
    boundaries = [h for h in _headings(raw) if h.level <= level or h.version]
    sections: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for pos, heading in enumerate(boundaries):
        if not heading.version or not in_range(heading.version, start, end):
            continue
        key = Ver(heading.version).raw.lstrip("v")
        if key in seen:
            continue
        seen.add(key)
        stop = boundaries[pos + 1].index if pos + 1 < len(boundaries) else len(raw)
        body = raw[heading.index + 1 : stop]
        if body and _RST_UNDERLINE.match(body[0]):
            body = body[1:]
        lines = clean_lines("\n".join(body))
        if lines:
            sections.append((heading.version, lines))
    sections.sort(key=lambda item: Ver(item[0]))
    return sections


def _changelog_candidates(info: PackageInfo) -> list[str]:
    paths: list[str] = []
    if info.changelog_url:
        match = re.search(r"github\.com/[^/]+/[^/]+/blob/[^/]+/(.+?)(?:#.*)?$", info.changelog_url)
        if match:
            paths.append(match.group(1))
    if info.directory:
        paths.extend(f"{info.directory}/{p}" for p in CHANGELOG_PATHS[:6])
    paths.extend(CHANGELOG_PATHS)
    return list(dict.fromkeys(paths))


def _limit(sections: list[NotesSection], warnings: list[str]) -> list[NotesSection]:
    total = sum(len(s.lines) for s in sections)
    if total <= MAX_NOTES_LINES:
        return sections
    kept: list[NotesSection] = []
    used = 0
    for section in sections:
        room = MAX_NOTES_LINES - used
        if room <= 0:
            break
        kept.append(section.model_copy(update={"lines": section.lines[:room]}))
        used += min(room, len(section.lines))
    warnings.append(f"release notes cut to {MAX_NOTES_LINES} of {total} lines")
    return kept


def _key(version: str) -> str:
    return Ver(version).raw.lstrip("v")


def _release_sections(bump: Bump, releases: list[dict[str, object]]) -> dict[str, NotesSection]:
    found: dict[str, NotesSection] = {}
    for release in releases:
        if release.get("draft"):
            continue
        version = tag_version(str(release.get("tag_name") or ""), bump.package)
        if version is None:
            version = tag_version(str(release.get("name") or ""), bump.package)
        if version is None or not in_range(version, bump.from_version, bump.to_version):
            continue
        lines = clean_lines(str(release.get("body") or ""))
        if is_trivial(lines):
            continue
        url = release.get("html_url")
        found.setdefault(
            _key(version),
            NotesSection(
                version=version,
                source="github-release",
                url=url if isinstance(url, str) else None,
                lines=lines,
            ),
        )
    return found


def _changelog_fill(
    bump: Bump,
    info: PackageInfo,
    github: GitHubClient,
    by_version: dict[str, NotesSection],
    warnings: list[str],
) -> None:
    """Add CHANGELOG sections for versions the releases did not cover."""
    assert info.repository is not None
    owner, name = info.repository
    wanted = [v for v in info.versions if in_range(v, bump.from_version, bump.to_version)]
    if by_version and all(_key(v) in by_version for v in wanted):
        return
    for path in _changelog_candidates(info):
        try:
            text = github.raw_file(owner, name, path)
        except GitHubError as exc:
            warnings.append(f"could not read {path}: {exc}")
            continue
        found = changelog_sections(text or "", bump.from_version, bump.to_version)
        if not found:
            continue
        url = f"https://github.com/{owner}/{name}/blob/HEAD/{path}"
        for version, lines in found:
            by_version.setdefault(
                _key(version),
                NotesSection(version=version, source="changelog", url=url, lines=lines),
            )
        return


def collect_notes(
    bump: Bump,
    info: PackageInfo | None,
    github: GitHubClient,
    *,
    pr_body: str | None = None,
) -> ReleaseNotes:
    warnings: list[str] = []
    if bump.from_version is None:
        warnings.append(
            "the starting version is unknown, so only the target version's notes are used"
        )
    repo = info.repository if info else None
    by_version: dict[str, NotesSection] = {}
    if info is not None and repo is not None:
        try:
            by_version = _release_sections(bump, github.releases(*repo))
        except GitHubError as exc:
            warnings.append(f"could not list GitHub releases of {repo[0]}/{repo[1]}: {exc}")
        _changelog_fill(bump, info, github, by_version, warnings)
    else:
        warnings.append(f"no GitHub repository is listed for {bump.package} in its registry")

    sections = sorted(by_version.values(), key=lambda s: Ver(s.version))
    if not sections and pr_body:
        lines = clean_lines(pr_body)
        if lines:
            warnings.append("release notes taken from the PR body, which may be truncated")
            sections = [NotesSection(version=bump.to_version, source="pr-body", lines=lines)]
    if not sections:
        warnings.append(
            f"no release notes found for {bump.package} between "
            f"{bump.from_version or '?'} and {bump.to_version}"
        )
    sections = _limit(sections, warnings)
    return ReleaseNotes.from_sections(
        bump.package,
        sections,
        repository=f"{repo[0]}/{repo[1]}" if repo else None,
        warnings=warnings,
    )


def chunk_notes(notes: ReleaseNotes, budget_tokens: int) -> list[list[int]]:
    """Group note line numbers into chunks under ``budget_tokens``, split at version
    boundaries when possible."""
    chunks: list[list[int]] = []
    current: list[int] = []
    used = 0
    for section_lines in _section_line_numbers(notes):
        cost = sum(_line_cost(notes.lines[n - 1].text) for n in section_lines)
        if current and used + cost > budget_tokens:
            chunks.append(current)
            current, used = [], 0
        if cost <= budget_tokens:
            current.extend(section_lines)
            used += cost
            continue
        for n in section_lines:
            line_cost = _line_cost(notes.lines[n - 1].text)
            if current and used + line_cost > budget_tokens:
                chunks.append(current)
                current, used = [], 0
            current.append(n)
            used += line_cost
    if current:
        chunks.append(current)
    return chunks


def _line_cost(text: str) -> int:
    return len(text) // 3 + 4


def _section_line_numbers(notes: ReleaseNotes) -> Iterable[list[int]]:
    start = 1
    for section in notes.sections:
        yield list(range(start, start + len(section.lines)))
        start += len(section.lines)
