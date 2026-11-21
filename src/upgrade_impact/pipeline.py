"""Glue: source -> bumps -> release notes -> claims -> code usages -> report."""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from upgrade_impact.bump import FileDiff, patch_lines, resolve_bumps, split_unified_diff
from upgrade_impact.config import Settings
from upgrade_impact.extract import extract_claims, uncovered_lines
from upgrade_impact.index.python import import_names_for, index_repository
from upgrade_impact.match import match_claims
from upgrade_impact.models import Bump, BumpReport, Ecosystem, Report
from upgrade_impact.notes import collect_notes
from upgrade_impact.providers.base import ChatModel
from upgrade_impact.sources.github import (
    GitHubClient,
    GitHubError,
    PullRef,
    parse_github_repo,
)
from upgrade_impact.sources.registry import RegistryClient, RegistryError

log = logging.getLogger(__name__)

SOURCE_SUFFIXES = (".py", ".pyi")


class CheckError(RuntimeError):
    """The check could not run (bad input, nothing to analyse)."""


@dataclass(slots=True)
class Target:
    """What to check: the bumps, plus context the notes and code lookups need."""

    source: str
    bumps: list[Bump]
    pr: PullRef | None = None
    pr_body: str | None = None
    head_sha: str | None = None
    warnings: list[str] = field(default_factory=list)


def target_from_pr(ref: PullRef, github: GitHubClient, ecosystem: Ecosystem | None) -> Target:
    pr = github.pull_request(ref)
    files = []
    for item in github.pull_request_files(ref):
        removed, added = patch_lines(str(item.get("patch") or ""))
        files.append(FileDiff(str(item.get("filename") or ""), removed, added))
    head = pr.get("head") or {}
    bumps = resolve_bumps(
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
        files=files,
        head_ref=str(head.get("ref") or ""),
        ecosystem=ecosystem,
    )
    return Target(
        source=f"https://github.com/{ref.slug}/pull/{ref.number}",
        bumps=bumps,
        pr=ref,
        pr_body=str(pr.get("body") or ""),
        head_sha=head.get("sha"),
    )


def target_from_diff(text: str, source: str, ecosystem: Ecosystem | None) -> Target:
    return Target(
        source=source,
        bumps=resolve_bumps(files=split_unified_diff(text), ecosystem=ecosystem),
    )


def _git_remote(root: Path) -> tuple[str, str] | None:
    try:
        url = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), "remote", "get-url", "origin"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_github_repo(url)


def default_cache_dir(settings: Settings, env: Mapping[str, str]) -> Path:
    if settings.cache_dir:
        return Path(settings.cache_dir).expanduser()
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "upgrade-impact"


def resolve_code_root(
    target: Target,
    repo: Path | None,
    github: GitHubClient,
    settings: Settings,
    env: Mapping[str, str],
) -> tuple[Path, list[str]]:
    """The directory whose code is analysed.

    ``--repo`` wins. For a PR URL, the current directory is used when it is a
    checkout of the same repository; otherwise the PR head's source files are
    downloaded into the cache.
    """
    if repo is not None:
        if not repo.is_dir():
            raise CheckError(f"--repo {repo} is not a directory")
        return repo.resolve(), []
    cwd = Path.cwd()
    if target.pr is None:
        return cwd, []
    remote = _git_remote(cwd)
    if (
        remote
        and remote[0].lower() == target.pr.owner.lower()
        and (remote[1].lower() == target.pr.repo.lower())
    ):
        return cwd, [f"using the local checkout in {cwd} for {target.pr.slug}"]
    ref = target.head_sha or f"refs/pull/{target.pr.number}/head"
    key = hashlib.sha256(f"{target.pr.slug}@{ref}".encode()).hexdigest()[:16]
    dest = default_cache_dir(settings, env) / "repos" / f"{target.pr.owner}-{target.pr.repo}-{key}"
    marker = dest / ".complete"
    if not marker.exists():
        try:
            count = github.download_source(
                target.pr.owner, target.pr.repo, ref, dest, SOURCE_SUFFIXES
            )
        except GitHubError as exc:
            raise CheckError(f"could not download the PR's code: {exc}") from exc
        marker.write_text(str(count), encoding="utf-8")
    return dest, [f"analysed {target.pr.slug} at {ref[:12]} (downloaded source files)"]


def check_bump(
    bump: Bump,
    *,
    target: Target,
    code_root: Path,
    chat: ChatModel | None,
    registry: RegistryClient,
    github: GitHubClient,
    settings: Settings,
    import_names: list[str] | None = None,
) -> BumpReport:
    report = BumpReport(bump=bump)
    try:
        info = registry.package(bump.ecosystem, bump.package)
    except RegistryError as exc:
        report.warnings.append(f"registry lookup failed: {exc}")
        info = None
    notes = collect_notes(bump, info, github, pr_body=target.pr_body)
    report.warnings.extend(notes.warnings)
    report.notes_versions = notes.versions
    report.notes_sources = sorted({s.source for s in notes.sections})
    report.notes_lines = len(notes.lines)

    if bump.ecosystem == Ecosystem.PYPI:
        names = import_names or import_names_for(bump.package)
        index = index_repository(code_root, names)
        usages = index.usages
        report.index = index.stats
    else:
        names = import_names or [bump.package]
        usages = []
        report.index.import_names = names
        report.warnings.append(
            "JavaScript/TypeScript code is not indexed yet, so usages are not shown for npm "
            "bumps (planned next)"
        )

    if chat is None or not notes.lines:
        # Without a model, the lines that read like breaking changes are the summary.
        report.uncovered = [
            line for n in uncovered_lines(notes, []) if (line := notes.line(n)) is not None
        ]
        return report
    extraction = extract_claims(
        chat,
        bump,
        notes,
        budget_tokens=settings.notes_budget_tokens,
        max_output_tokens=settings.max_output_tokens,
        repair_attempts=settings.repair_attempts,
    )
    report.model = extraction.run
    report.claims_proposed = extraction.proposed
    report.claims_dropped = extraction.dropped
    report.warnings.extend(extraction.warnings)
    report.results = match_claims(extraction.claims, usages, names)
    report.uncovered = [
        line
        for n in uncovered_lines(notes, extraction.claims)
        if (line := notes.line(n)) is not None
    ]
    return report


def run_check(
    target: Target,
    *,
    chat: ChatModel | None,
    registry: RegistryClient,
    github: GitHubClient,
    settings: Settings,
    repo: Path | None = None,
    import_names: list[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Report:
    env = os.environ if env is None else env
    if not target.bumps:
        raise CheckError(
            f"no dependency bump found in {target.source}; pass --package, --from and --to"
        )
    code_root, notes = resolve_code_root(target, repo, github, settings, env)
    report = Report(
        source=target.source, repository_root=str(code_root), warnings=[*target.warnings, *notes]
    )
    for bump in target.bumps:
        log.info("checking bump", extra={"bump": bump.label})
        report.bumps.append(
            check_bump(
                bump,
                target=target,
                code_root=code_root,
                chat=chat,
                registry=registry,
                github=github,
                settings=settings,
                import_names=import_names,
            )
        )
    return report
