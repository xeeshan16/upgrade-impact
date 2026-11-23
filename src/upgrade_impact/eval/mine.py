"""Mine labelled cases from the git history of public repositories.

Only git is needed (no API token, no rate limit). For each repository the
default branch's first-parent history is scanned for merged Dependabot and
Renovate PRs:

* **merge commits** ("Merge pull request #N from owner/dependabot/...") give the
  PR's own commits: the bot's bump commit, then any commits a human pushed to
  the same branch. Human commits that change source code are the fix, and the
  last bot commit before them is the snapshot the tool analyses.
* **squash merges** authored by the bot ("Bump x from 1 to 2 (#N)") carry the
  humans who pushed fixes as ``Co-authored-by`` trailers; the squashed diff's
  source-file changes are the fix and the parent commit is the snapshot.

A PR with only bot commits that changed manifests and lock files is a
``no_impact`` case, unless a later human commit names the package (a fix landed
separately) or reverts the bump. Bumps of packages the snapshot never imports
are skipped: no tool needs to read code there.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from upgrade_impact.bump import infer_ecosystem, parse_title
from upgrade_impact.eval.dataset import Case, Label, LabelFile, LabelHunk, split_for
from upgrade_impact.eval.git import Commit, Git, GitError, Hunk, clone
from upgrade_impact.eval.snapshot import ensure_snapshot, is_source, package_usages
from upgrade_impact.models import Bump, Ecosystem
from upgrade_impact.versions import Ver

log = logging.getLogger(__name__)

BOT = re.compile(r"\[bot\]|^(dependabot|renovate)(-preview|-bot)?$|^renovate bot$", re.I)
UPDATE_BOT = re.compile(r"dependabot|renovate", re.I)
MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+) from (\S+)")
SQUASH_PR = re.compile(r"\s*\(#(\d+)\)\s*$")
CO_AUTHOR = re.compile(r"^Co-authored-by:\s*(?P<name>.+?)\s*<(?P<email>[^>]*)>", re.M | re.I)
REVERT = re.compile(r'^Revert "', re.I)
# Fix commits that only touch these are configuration-only and not code impact.
NON_CODE = re.compile(
    r"(?:^|/)(?:requirements[^/]*\.(?:txt|in)|constraints[^/]*\.txt|pyproject\.toml|setup\.(?:py|cfg)"
    r"|Pipfile(?:\.lock)?|poetry\.lock|uv\.lock|package(?:-lock)?\.json|npm-shrinkwrap\.json"
    r"|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|\.pre-commit-config\.yaml)$",
    re.I,
)
MAX_FIX_HUNKS = 40
MAX_FIX_LINES = 300
LATER_WINDOW = 40  # first-parent commits after a clean bump checked for a separate fix


def is_bot(name: str, email: str) -> bool:
    return bool(BOT.search(name.strip()) or BOT.search(email.split("@", 1)[0]))


def is_update_bot(commit: Commit) -> bool:
    return is_bot(commit.author_name, commit.author_email) and bool(
        UPDATE_BOT.search(commit.author_name) or UPDATE_BOT.search(commit.author_email)
    )


def human_coauthors(body: str) -> list[str]:
    return [
        m.group("name")
        for m in CO_AUTHOR.finditer(body)
        if not is_bot(m.group("name"), m.group("email"))
    ]


def is_notable(from_version: str, to_version: str) -> bool:
    """A major bump, or a minor bump below 1.0 (where semver allows breaking changes)."""
    start, end = Ver(from_version).release, Ver(to_version).release
    if not start or not end or Ver(to_version) <= Ver(from_version):
        return False
    s = (*start, 0, 0)[:3]
    e = (*end, 0, 0)[:3]
    if e[0] > s[0]:
        return True
    if s[0] == 0 and e[0] == 0:
        return e[1] > s[1] or (s[1] == 0 and e[1] == 0 and e[2] > s[2])
    return False


def looks_generated(hunks: Sequence[Hunk]) -> bool:
    """A fix that rewrites hundreds of lines of one file is a rebuilt bundle
    (``lib/*.js`` compiled from ``src/``) or a reformat, not a code change."""
    changed = sum(h.end - h.start + 1 for h in hunks if not h.pure_insert)
    return len(hunks) > MAX_FIX_HUNKS or changed > MAX_FIX_LINES


def _code_paths(paths: Sequence[str]) -> list[str]:
    return [p for p in paths if is_source(p) and not NON_CODE.search(p)]


@dataclass(slots=True)
class MineStats:
    commits_scanned: int = 0
    bot_prs: int = 0
    reasons: Counter[str] = field(default_factory=Counter)

    def skip(self, reason: str) -> None:
        self.reasons[reason] += 1


@dataclass(slots=True)
class _Candidate:
    pr: int | None
    commit: Commit
    bump: Bump
    snapshot: str
    style: str
    label: Label
    fixes: list[Commit]
    index: int  # position in first-parent history (0 = newest)


@dataclass(slots=True)
class MineOptions:
    since: str | None = "3 years ago"
    max_commits: int = 5000
    all_bumps: bool = False
    max_no_impact_per_repo: int = 6
    max_impact_per_repo: int = 25


class RepoMiner:
    def __init__(self, repo: str, git: Git, cache_dir: Path, options: MineOptions) -> None:
        self.repo = repo
        self.git = git
        self.cache_dir = cache_dir
        self.options = options
        self.stats = MineStats()

    # -- candidates ---------------------------------------------------------

    def _bump(self, title: str, commit: Commit, branch: str = "") -> Bump | None:
        parsed = parse_title(SQUASH_PR.sub("", title))
        if parsed is None:
            self.stats.skip("title not a single-package bump")
            return None
        package, start, end = parsed
        if not start:
            self.stats.skip("no starting version")
            return None
        if not commit.parents:
            return None
        try:
            paths = self.git.changed_paths(commit.parents[0], commit.sha)
        except GitError:
            self.stats.skip("diff failed")
            return None
        ecosystem = infer_ecosystem(paths, branch)
        if ecosystem is None and package.startswith("@"):
            ecosystem = Ecosystem.NPM
        if ecosystem is None:
            self.stats.skip("ecosystem not pypi or npm")
            return None
        if not self.options.all_bumps and not is_notable(start, end):
            self.stats.skip("not a major (or 0.x minor) bump")
            return None
        return Bump(
            ecosystem=ecosystem, package=package, from_version=start, to_version=end, source="title"
        )

    def _from_merge(self, commit: Commit, index: int) -> _Candidate | None:
        match = MERGE_SUBJECT.match(commit.subject)
        if not match or not UPDATE_BOT.search(match.group(2)) or len(commit.parents) != 2:
            return None
        self.stats.bot_prs += 1
        branch = match.group(2)
        try:
            pr_commits = self.git.log(f"{commit.parents[0]}..{commit.parents[1]}", reverse=True)
        except GitError:
            self.stats.skip("could not list PR commits")
            return None
        if not pr_commits or not is_update_bot(pr_commits[0]):
            self.stats.skip("first PR commit not by the update bot")
            return None
        first = pr_commits[0]
        bump = self._bump(first.subject, first, branch)
        if bump is None:
            return None
        snapshot = first.sha
        fixes: list[Commit] = []
        for item in pr_commits:
            if is_bot(item.author_name, item.author_email):
                if not fixes:
                    snapshot = item.sha
                continue
            if item.is_merge:
                continue
            fixes.append(item)
        label = Label.IMPACT if fixes else Label.NO_IMPACT
        return _Candidate(int(match.group(1)), commit, bump, snapshot, "merge", label, fixes, index)

    def _from_squash(self, commit: Commit, index: int) -> _Candidate | None:
        if commit.is_merge or not is_update_bot(commit) or not commit.parents:
            return None
        if parse_title(SQUASH_PR.sub("", commit.subject)) is None and not SQUASH_PR.search(
            commit.subject
        ):
            return None
        self.stats.bot_prs += 1
        bump = self._bump(commit.subject, commit)
        if bump is None:
            return None
        pr_match = SQUASH_PR.search(commit.subject)
        humans = human_coauthors(commit.body)
        label = Label.IMPACT if humans else Label.NO_IMPACT
        fixes = [commit] if humans else []
        pr = int(pr_match.group(1)) if pr_match else None
        return _Candidate(pr, commit, bump, commit.parents[0], "squash", label, fixes, index)

    # -- labels ---------------------------------------------------------------

    def _label_files(self, cand: _Candidate) -> list[LabelFile]:
        files: dict[str, LabelFile] = {}
        for fix in cand.fixes:
            parent = fix.parents[0]
            paths = _code_paths(self.git.changed_paths(parent, fix.sha))
            if not paths:
                continue
            hunks = self.git.hunks(parent, fix.sha, paths)
            before = self.git.blob_ids(parent, paths)
            at_snapshot = self.git.blob_ids(cand.snapshot, paths)
            for path in paths:
                if path not in at_snapshot:
                    continue  # a new file is not a line the tool could have flagged
                if looks_generated(hunks.get(path, [])):
                    self.stats.skip("generated-looking fix file dropped")
                    continue
                known = before.get(path) == at_snapshot.get(path)
                entry = files.setdefault(path, LabelFile(path=path, lines_known=known))
                if not known:
                    entry.lines_known = False
                    entry.hunks = []
                    continue
                if entry.lines_known:
                    entry.hunks.extend(
                        LabelHunk(start=h.start, end=h.end, pure_insert=h.pure_insert)
                        for h in hunks.get(path, [])
                    )
        return sorted(files.values(), key=lambda f: f.path)

    def _separately_fixed(self, cand: _Candidate, history: Sequence[Commit]) -> bool:
        """A later human commit names the package or reverts the bump."""
        name = re.escape(cand.bump.package.rsplit("/", 1)[-1])
        mention = re.compile(rf"(?<![\w-]){name}(?![\w-])", re.I)
        for later in history[max(0, cand.index - LATER_WINDOW) : cand.index]:
            if REVERT.match(later.subject) and mention.search(later.subject):
                return True
            if is_bot(later.author_name, later.author_email):
                continue
            if mention.search(later.subject):
                return True
        return False

    def _case(self, cand: _Candidate, files: list[LabelFile], import_files: int) -> Case:
        pr_url = (
            f"https://github.com/{self.repo}/pull/{cand.pr}"
            if cand.pr is not None
            else f"https://github.com/{self.repo}/commit/{cand.commit.sha}"
        )
        key = (
            f"{self.repo}#{cand.pr}"
            if cand.pr is not None
            else f"{self.repo}@{cand.commit.sha[:12]}"
        )
        return Case(
            id=f"{key}:{cand.bump.package}",
            repo=self.repo,
            pr=cand.pr,
            url=pr_url,
            ecosystem=cand.bump.ecosystem,
            package=cand.bump.package,
            from_version=cand.bump.from_version or "",
            to_version=cand.bump.to_version,
            snapshot=cand.snapshot,
            label=cand.label,
            files=files,
            fix_commits=[c.sha for c in cand.fixes],
            merged_at=cand.commit.date,
            merge_style=cand.style,
            split=split_for(self.repo),
            import_files=import_files,
        )

    def _imports(self, cand: _Candidate) -> int:
        root = ensure_snapshot(self.git, self.cache_dir, self.repo, cand.snapshot)
        return package_usages(root, cand.bump).stats.files_importing

    def mine(self) -> list[Case]:
        branch = self.git.default_ref()
        history = self.git.log(
            branch,
            first_parent=True,
            max_count=self.options.max_commits,
            since=self.options.since,
        )
        self.stats.commits_scanned = len(history)
        candidates: list[_Candidate] = []
        for index, commit in enumerate(history):
            cand = self._from_merge(commit, index) or self._from_squash(commit, index)
            if cand is not None:
                candidates.append(cand)

        cases: list[Case] = []
        impact = [c for c in candidates if c.label == Label.IMPACT]
        clean = [c for c in candidates if c.label == Label.NO_IMPACT]
        for cand in impact[: self.options.max_impact_per_repo]:
            files = self._label_files(cand)
            if not files:
                self.stats.skip("fix touched no existing source file")
                continue
            imports = self._imports(cand)
            if imports == 0:
                self.stats.skip("package not imported at the snapshot")
                continue
            cases.append(self._case(cand, files, imports))
        # Deterministic sample of clean bumps, so busy repositories do not dominate.
        clean.sort(key=lambda c: hashlib.sha256(c.commit.sha.encode()).hexdigest())
        kept = 0
        for cand in clean:
            if kept >= self.options.max_no_impact_per_repo:
                break
            if self._separately_fixed(cand, history):
                self.stats.skip("clean bump fixed or reverted later")
                continue
            imports = self._imports(cand)
            if imports == 0:
                self.stats.skip("package not imported at the snapshot")
                continue
            cases.append(self._case(cand, [], imports))
            kept += 1
        return cases


def _mine_one(
    repo: str, cache_dir: Path, options: MineOptions, git_base_url: str
) -> tuple[list[Case], MineStats]:
    git = clone(repo, cache_dir / "git" / repo.replace("/", "__"), base_url=git_base_url)
    miner = RepoMiner(repo, git, cache_dir, options)
    return miner.mine(), miner.stats


def mine_repositories(
    repos: Sequence[str],
    cache_dir: Path,
    options: MineOptions,
    *,
    git_base_url: str = "https://github.com",
    jobs: int = 1,
    progress: Callable[[str], None] = lambda _msg: None,
) -> tuple[list[Case], dict[str, MineStats]]:
    """Mine each repository; ``jobs`` repositories at a time (git does the work)."""
    cases: list[Case] = []
    stats: dict[str, MineStats] = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {
            repo: pool.submit(_mine_one, repo, cache_dir, options, git_base_url) for repo in repos
        }
        for repo, future in futures.items():
            try:
                found, repo_stats = future.result()
            except GitError as exc:
                progress(f"{repo}: skipped ({exc})")
                continue
            stats[repo] = repo_stats
            cases.extend(found)
            impact = sum(1 for c in found if c.label == Label.IMPACT)
            progress(
                f"{repo}: {repo_stats.commits_scanned} commits, {repo_stats.bot_prs} bot PRs, "
                f"{impact} impact + {len(found) - impact} no-impact cases"
            )
    return cases, stats
