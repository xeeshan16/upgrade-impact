"""Thin, typed wrapper over the ``git`` command line.

The miner and the eval runner only need a handful of read-only operations on
public repositories: a partial clone (commits and trees, blobs fetched on
demand), first-parent history, the commits of a merged branch, the old-side
lines of a diff, and an export of the source files at one commit. Plain git
over HTTPS needs no API token and has no rate limit, so both work the same on
a laptop and in CI.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
import tarfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

_FIELD = "\x1f"
_RECORD = "\x1e"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SAFE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitError(RuntimeError):
    """A git command failed."""


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    date: str
    subject: str
    body: str

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


@dataclass(frozen=True, slots=True)
class Hunk:
    """Old-side line range ``[start, end]`` touched by a diff hunk.

    A pure insertion has no old lines; it is anchored to the line it follows
    (``start == end == anchor``), so the place a fix was added still counts.
    """

    start: int
    end: int
    pure_insert: bool = False

    def lines(self) -> list[int]:
        return list(range(self.start, self.end + 1))


def valid_repo_slug(slug: str) -> bool:
    return bool(_SAFE_REPO.match(slug)) and ".." not in slug


def parse_hunks(diff_text: str) -> dict[str, list[Hunk]]:
    """Old-side hunks per old path from ``git diff --unified=0`` output."""
    out: dict[str, list[Hunk]] = {}
    path: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            name = line[4:].strip()
            path = None if name == "/dev/null" else name.removeprefix("a/")
            continue
        if line.startswith("+++ "):
            continue
        match = _HUNK.match(line)
        if not match or path is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count == 0:
            anchor = max(start, 1)
            out.setdefault(path, []).append(Hunk(anchor, anchor, pure_insert=True))
        else:
            out.setdefault(path, []).append(Hunk(start, start + count - 1))
    return out


class Git:
    """Runs git against one bare repository directory."""

    def __init__(self, repo_dir: Path, *, timeout_s: float = 600.0) -> None:
        self.repo_dir = repo_dir
        self._timeout = timeout_s

    def run(self, *args: str, input_text: str | None = None) -> str:
        return self._run_bytes(*args, input_bytes=input_text.encode() if input_text else None)[
            0
        ].decode("utf-8", errors="replace")

    def _run_bytes(self, *args: str, input_bytes: bytes | None = None) -> tuple[bytes, bytes]:
        argv = ["git", "-C", str(self.repo_dir), *args]
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                input=input_bytes,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {' '.join(args[:3])} timed out") from exc
        except OSError as exc:
            raise GitError(f"could not run git: {exc}") from exc
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {' '.join(args[:3])} failed: {message[:300]}")
        return proc.stdout, proc.stderr

    # -- history ------------------------------------------------------------

    def default_ref(self) -> str:
        return self.run("symbolic-ref", "--short", "HEAD").strip() or "HEAD"

    def log(
        self,
        rev: str,
        *,
        first_parent: bool = False,
        max_count: int | None = None,
        since: str | None = None,
        reverse: bool = False,
    ) -> list[Commit]:
        fmt = _FIELD.join(["%H", "%P", "%an", "%ae", "%cI", "%s", "%b"]) + _RECORD
        args = ["log", f"--format={fmt}", "--no-color"]
        if first_parent:
            args.append("--first-parent")
        if max_count is not None:
            args.append(f"--max-count={max_count}")
        if since:
            args.append(f"--since={since}")
        if reverse:
            args.append("--reverse")
        args.append(rev)
        commits: list[Commit] = []
        for record in self.run(*args).split(_RECORD):
            text = record.strip("\n")
            if not text:
                continue
            parts = text.split(_FIELD)
            if len(parts) < 7:
                continue
            sha, parents, name, email, date, subject, body = parts[:7]
            commits.append(
                Commit(
                    sha=sha,
                    parents=tuple(parents.split()),
                    author_name=name,
                    author_email=email,
                    date=date,
                    subject=subject,
                    body=body.strip(),
                )
            )
        return commits

    def changed_paths(self, old: str, new: str) -> list[str]:
        out = self.run("diff", "--name-only", "--no-renames", old, new)
        return [p for p in out.splitlines() if p]

    def hunks(
        self, old: str, new: str, paths: Sequence[str] | None = None
    ) -> dict[str, list[Hunk]]:
        args = ["diff", "--unified=0", "--no-renames", "--no-color", old, new]
        if paths:
            args += ["--", *paths]
        return parse_hunks(self.run(*args))

    def blob_ids(self, sha: str, paths: Iterable[str]) -> dict[str, str]:
        wanted = list(dict.fromkeys(paths))
        if not wanted:
            return {}
        out = self.run("ls-tree", sha, "--", *wanted)
        ids: dict[str, str] = {}
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                ids[path] = parts[2]
        return ids

    def list_files(self, sha: str) -> list[str]:
        return [p for p in self.run("ls-tree", "-r", "--name-only", sha).splitlines() if p]

    def file_text(self, sha: str, path: str) -> str | None:
        try:
            return self.run("show", f"{sha}:{path}")
        except GitError:
            return None

    def export(self, sha: str, dest: Path, paths: Sequence[str]) -> int:
        """Write ``paths`` as they are at ``sha`` under ``dest``. Returns the file count."""
        if not paths:
            dest.mkdir(parents=True, exist_ok=True)
            return 0
        count = 0
        # Batches keep the command line short on big repositories.
        for start in range(0, len(paths), 500):
            batch = list(paths[start : start + 500])
            data, _ = self._run_bytes("archive", "--format=tar", sha, "--", *batch)
            count += _extract(data, dest)
        return count


def _extract(data: bytes, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = PurePosixPath(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                continue
            target = (root / rel).resolve()
            if not target.is_relative_to(root):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read())
            count += 1
    return count


def clone(
    slug: str, dest: Path, *, timeout_s: float = 900.0, base_url: str = "https://github.com"
) -> Git:
    """A bare partial clone of ``owner/repo`` at ``dest`` (fetched again if it exists)."""
    if not valid_repo_slug(slug):
        raise GitError(f"not an owner/repo slug: {slug!r}")
    if (dest / "HEAD").exists():
        git = Git(dest, timeout_s=timeout_s)
        try:
            branch = git.default_ref()
            git.run(
                "fetch",
                "--quiet",
                "--filter=blob:none",
                "origin",
                f"+refs/heads/{branch}:refs/heads/{branch}",
            )
        except GitError as exc:
            log.warning("could not refresh %s: %s", slug, exc)
        return git
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{base_url.rstrip('/')}/{slug}.git"
    argv = ["git", "clone", "--quiet", "--bare", "--filter=blob:none", url, str(dest)]
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            argv, capture_output=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"cloning {slug} timed out") from exc
    if proc.returncode != 0:
        raise GitError(
            f"cloning {slug} failed: {proc.stderr.decode('utf-8', errors='replace')[:300]}"
        )
    return Git(dest, timeout_s=timeout_s)
