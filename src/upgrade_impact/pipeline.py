"""Glue: source -> bumps -> release notes -> claims -> code usages -> report."""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from upgrade_impact.adjudicate import SourceFiles, adjudicate
from upgrade_impact.bump import FileDiff, patch_lines, resolve_bumps, split_unified_diff
from upgrade_impact.config import Settings
from upgrade_impact.extract import extract_claims, uncovered_lines, verify_claims
from upgrade_impact.guard import suspicious_lines
from upgrade_impact.index.javascript import JS_SUFFIXES, index_js_repository
from upgrade_impact.index.python import import_names_for, index_repository
from upgrade_impact.match import match_claims
from upgrade_impact.models import (
    Bump,
    BumpReport,
    Claim,
    Ecosystem,
    ReleaseNotes,
    Report,
    Usage,
)
from upgrade_impact.notes import collect_notes
from upgrade_impact.pricing import account
from upgrade_impact.providers.base import ChatModel
from upgrade_impact.providers.bounded import BoundedChat
from upgrade_impact.recheck import code_names, recheck_lines
from upgrade_impact.runtime import (
    MANIFEST_FILES,
    Floor,
    apply_runtime,
    project_floor,
    runtime_dismissals,
)
from upgrade_impact.sources.github import (
    GitHubClient,
    GitHubError,
    PullRef,
    parse_github_repo,
)
from upgrade_impact.sources.registry import RegistryClient, RegistryError
from upgrade_impact.telemetry import get_telemetry

log = logging.getLogger(__name__)

SOURCE_SUFFIXES = (".py", ".pyi", *JS_SUFFIXES)
# What a PR's code download keeps: the source files plus the manifests that
# declare the runtime floor (matched as name suffixes).
DOWNLOAD_SUFFIXES = (*SOURCE_SUFFIXES, *MANIFEST_FILES)


class CheckError(RuntimeError):
    """The check could not run (bad input, nothing to analyse)."""


@dataclass(frozen=True, slots=True)
class Deadline:
    """A wall-clock budget for one check, checked between model stages.

    A stage already running finishes (each model call is bounded by the
    provider timeout); stages not yet started when the budget is spent are
    skipped, and the bump is reported as "review", never "no impact".
    """

    budget_s: float = 0.0
    at: float | None = None
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def after(cls, seconds: float, clock: Callable[[], float] = time.monotonic) -> Deadline:
        return cls(seconds, clock() + seconds if seconds > 0 else None, clock)

    def expired(self) -> bool:
        return self.at is not None and self.clock() >= self.at


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
    # "v2": downloads made before manifests were kept lack them.
    key = hashlib.sha256(f"v2|{target.pr.slug}@{ref}".encode()).hexdigest()[:16]
    dest = default_cache_dir(settings, env) / "repos" / f"{target.pr.owner}-{target.pr.repo}-{key}"
    marker = dest / ".complete"
    if not marker.exists():
        try:
            count = github.download_source(
                target.pr.owner, target.pr.repo, ref, dest, DOWNLOAD_SUFFIXES
            )
        except GitHubError as exc:
            raise CheckError(f"could not download the PR's code: {exc}") from exc
        marker.write_text(str(count), encoding="utf-8")
    return dest, [f"analysed {target.pr.slug} at {ref[:12]} (downloaded source files)"]


def fetch_notes(
    bump: Bump,
    registry: RegistryClient,
    github: GitHubClient,
    *,
    pr_body: str | None = None,
) -> ReleaseNotes:
    try:
        info = registry.package(bump.ecosystem, bump.package)
    except RegistryError as exc:
        notes = collect_notes(bump, None, github, pr_body=pr_body)
        notes.warnings.insert(0, f"registry lookup failed: {exc}")
        return notes
    return collect_notes(bump, info, github, pr_body=pr_body)


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
    review: bool = True,
    notes: ReleaseNotes | None = None,
    deadline: Deadline | None = None,
) -> BumpReport:
    """Check one bump. ``notes`` skips the release-note lookup (the eval passes
    a cached copy so every system reads the same notes). ``deadline`` skips the
    model stages that have not started when it expires."""
    report = BumpReport(bump=bump)
    deadline = deadline or Deadline()
    telemetry = get_telemetry()
    with telemetry.stage("notes", cached=notes is not None) as stage:
        if notes is None:
            notes = fetch_notes(bump, registry, github, pr_body=target.pr_body)
        stage.set("note_lines", len(notes.lines))
        stage.set("versions", len(notes.versions))
    report.warnings.extend(notes.warnings)
    report.notes_versions = notes.versions
    report.notes_sources = sorted({s.source for s in notes.sections})
    report.notes_lines = len(notes.lines)

    names, usages = _index(report, code_root, import_names)
    floor = project_floor(code_root, bump.ecosystem)
    report.suspicious = [
        line for n in suspicious_lines(notes) if (line := notes.line(n)) is not None
    ]
    if report.suspicious:
        report.warnings.append(
            f"{len(report.suspicious)} release-note line(s) read like instructions to a model "
            "(possible prompt injection): "
            + ", ".join(f"N{line.n}" for line in report.suspicious[:5])
        )

    if chat is not None and notes.lines and _skip(report, deadline, "extract"):
        chat = None
    if chat is None or not notes.lines:
        # Without a model, the lines that read like breaking changes are the summary.
        _settle_uncovered(report, notes, uncovered_lines(notes, []), floor, bump)
        return report
    with telemetry.stage("extract", provider=chat.provider, model=chat.model) as stage:
        extraction = extract_claims(
            chat,
            bump,
            notes,
            budget_tokens=settings.notes_budget_tokens,
            max_output_tokens=settings.max_output_tokens,
            repair_attempts=settings.repair_attempts,
        )
        stage.set("calls", extraction.run.calls)
        stage.set("claims", len(extraction.claims))
        stage.set("claims_dropped", extraction.dropped)
    report.model = extraction.run
    report.claims_proposed = extraction.proposed
    report.claims_dropped = extraction.dropped
    report.dropped_claims = extraction.dropped_claims
    report.warnings.extend(extraction.warnings)
    claims = extraction.claims

    if settings.recheck == "on" and not _skip(report, deadline, "recheck"):
        with telemetry.stage("recheck") as stage:
            claims = _second_reading(
                report,
                chat,
                bump=bump,
                notes=notes,
                claims=claims,
                floor=floor,
                usages=usages,
                names=names,
                settings=settings,
            )
            stage.set("calls", report.recheck.calls if report.recheck else 0)
            stage.set("claims", len(claims))

    report.results = match_claims(claims, usages, names)
    apply_runtime(report.results, notes, floor, bump.ecosystem)
    if review and settings.max_candidates > 0 and not _skip(report, deadline, "review"):
        _review(report, chat, notes=notes, usages=usages, code_root=code_root, settings=settings)
    dismissed = {d.line.n for d in report.dismissed}
    _settle_uncovered(
        report,
        notes,
        [n for n in uncovered_lines(notes, claims) if n not in dismissed],
        floor,
        bump,
    )
    return report


def _review(
    report: BumpReport,
    chat: ChatModel,
    *,
    notes: ReleaseNotes,
    usages: list[Usage],
    code_root: Path,
    settings: Settings,
) -> None:
    """Have the model read the uncertain matches in their (redacted) code context."""
    files = SourceFiles(code_root)
    with get_telemetry().stage("review") as stage:
        outcome = adjudicate(
            chat,
            report.bump,
            notes,
            report.results,
            usages=usages,
            files=files,
            max_candidates=settings.max_candidates,
            max_output_tokens=settings.max_output_tokens,
            max_calls=settings.max_review_calls,
        )
        stage.set("calls", outcome.run.calls)
        stage.set("redacted", sum(files.redacted.values()))
    if files.redacted:
        kinds = ", ".join(f"{n} {kind}" for kind, n in sorted(files.redacted.items()))
        report.warnings.append(
            f"redacted secrets in the code read for review before any model saw it ({kinds})"
        )
    if outcome.run.calls:
        report.adjudication = outcome.run
    report.warnings.extend(outcome.warnings)


def _index(
    report: BumpReport, code_root: Path, import_names: list[str] | None
) -> tuple[list[str], list[Usage]]:
    """The package's import names and this code's usages of it (stats on ``report``)."""
    bump = report.bump
    with get_telemetry().stage("index", language=bump.ecosystem.value) as stage:
        if bump.ecosystem == Ecosystem.PYPI:
            names = import_names or import_names_for(bump.package)
            index = index_repository(code_root, names)
            usages, report.index = index.usages, index.stats
        else:
            names = import_names or [bump.package]
            js_index = index_js_repository(code_root, names)
            usages, report.index = js_index.usages, js_index.stats
        stage.set("files_scanned", report.index.files_scanned)
        stage.set("usages", len(usages))
    return names, usages


def _skip(report: BumpReport, deadline: Deadline, stage: str) -> bool:
    """True (and recorded on the report) when the budget is spent before ``stage``."""
    if not deadline.expired():
        return False
    report.skipped.append(stage)
    report.warnings.append(
        f"time budget of {deadline.budget_s:g} s ran out before the {stage} stage; "
        "it was skipped, so this bump needs a human look"
    )
    return True


def _second_reading(
    report: BumpReport,
    chat: ChatModel,
    *,
    bump: Bump,
    notes: ReleaseNotes,
    claims: list[Claim],
    floor: Floor | None,
    usages: list[Usage],
    names: list[str],
    settings: Settings,
) -> list[Claim]:
    """Re-read the unclaimed breaking-looking lines (those the declared runtime
    does not already settle). New claims are verified and appended; lines the
    model rules out (within the guards of :mod:`.recheck`) are dismissed."""
    unclaimed = uncovered_lines(notes, claims)
    settled = {d.line.n for d in runtime_dismissals(notes, unclaimed, floor, bump.ecosystem)}
    second = [n for n in unclaimed if n not in settled]
    if not second:
        return claims
    recheck = recheck_lines(
        chat,
        bump,
        notes,
        second,
        used_names=code_names(usages, names),
        package_names=names,
        protected=[line.n for line in report.suspicious],
        max_output_tokens=settings.max_output_tokens,
    )
    report.recheck = recheck.run
    report.warnings.extend(recheck.warnings)
    report.dismissed.extend(recheck.dismissed)
    if not recheck.claims:
        return claims
    added, dropped = verify_claims(notes, recheck.claims)
    report.claims_proposed += len(recheck.claims)
    report.claims_dropped += len(dropped)
    report.dropped_claims.extend(dropped)
    return _renumber([*claims, *added])


def _renumber(claims: list[Claim]) -> list[Claim]:
    return [c.model_copy(update={"id": f"C{i}"}) for i, c in enumerate(claims, start=1)]


def _settle_uncovered(
    report: BumpReport,
    notes: ReleaseNotes,
    unclaimed: list[int],
    floor: Floor | None,
    bump: Bump,
) -> None:
    """Unclaimed breaking-looking lines: the ones the declared runtime already
    meets are dismissed, the rest stay for a human."""
    by_runtime = runtime_dismissals(notes, unclaimed, floor, bump.ecosystem)
    report.dismissed.extend(by_runtime)
    gone = {d.line.n for d in by_runtime}
    report.uncovered = [
        line for n in unclaimed if n not in gone and (line := notes.line(n)) is not None
    ]


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
    review: bool = True,
    on_bump: Callable[[int, int, Bump], None] | None = None,
) -> Report:
    """Check every bump of ``target``, up to ``settings.jobs`` at once.

    ``on_bump(i, total, bump)`` is called as each bump starts, with ``i``
    counting up from 0 in start order (progress for long grouped checks). With
    more than one job, model calls are capped at ``settings.model_concurrency``
    in flight. Reports come back in the target's order whatever finishes first.
    """
    env = os.environ if env is None else env
    started = time.monotonic()
    if not target.bumps:
        raise CheckError(
            f"no dependency bump found in {target.source}; pass --package, --from and --to"
        )
    code_root, notes = resolve_code_root(target, repo, github, settings, env)
    report = Report(
        source=target.source, repository_root=str(code_root), warnings=[*target.warnings, *notes]
    )
    telemetry = get_telemetry()
    deadline = Deadline.after(settings.max_seconds)
    jobs = min(settings.jobs, len(target.bumps))
    if chat is not None and jobs > 1:
        chat = BoundedChat(chat, settings.model_concurrency)
    started_count = 0
    progress_lock = threading.Lock()

    def one(bump: Bump) -> BumpReport:
        nonlocal started_count
        if on_bump is not None:
            with progress_lock:
                on_bump(started_count, len(target.bumps), bump)
                started_count += 1
        log.info("checking bump", extra={"bump": bump.label})
        bump_started = time.monotonic()
        with telemetry.stage(
            "bump",
            package=bump.package,
            ecosystem=bump.ecosystem.value,
            from_version=bump.from_version,
            to_version=bump.to_version,
        ) as stage:
            bump_report = check_bump(
                bump,
                target=target,
                code_root=code_root,
                chat=chat,
                registry=registry,
                github=github,
                settings=settings,
                import_names=import_names,
                review=review,
                deadline=deadline,
            )
            stage.set("verdict", bump_report.verdict)
            stage.set("skipped", ",".join(bump_report.skipped) or None)
        bump_report.elapsed_s = round(time.monotonic() - bump_started, 3)
        telemetry.record_bump(bump_report)
        return bump_report

    with telemetry.stage("check", bumps=len(target.bumps), jobs=jobs) as stage:
        report.bumps = _run_all(one, target.bumps, jobs)
        report.accounting = account(report, env)
        report.elapsed_s = round(time.monotonic() - started, 3)
        stage.set("verdict", report.verdict)
        if report.accounting is not None:
            stage.set("model", report.accounting.model)
            stage.set("model_calls", report.accounting.calls)
            stage.set("input_tokens", report.accounting.input_tokens)
            stage.set("output_tokens", report.accounting.output_tokens)
            stage.set("cost_usd", report.accounting.cost_usd)
    telemetry.record_report(report)
    return report


def _run_all(one: Callable[[Bump], BumpReport], bumps: list[Bump], jobs: int) -> list[BumpReport]:
    """``one`` over every bump, on ``jobs`` threads, results in input order."""
    if jobs <= 1:
        return [one(bump) for bump in bumps]
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="upgrade-impact") as pool:
        # Each worker runs in a copy of the caller's context, so the current span
        # (and any other context variable) follows the bump onto its thread.
        futures = [pool.submit(contextvars.copy_context().run, one, bump) for bump in bumps]
        try:
            return [future.result() for future in futures]
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
