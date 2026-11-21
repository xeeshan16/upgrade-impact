"""Read-only GitHub access: pull requests, releases, raw files and source tarballs.

Uses ``GITHUB_TOKEN`` (or ``GH_TOKEN``) when set; public repositories work
without one, within GitHub's unauthenticated rate limit.
"""

from __future__ import annotations

import io
import logging
import re
import tarfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from upgrade_impact import __version__

log = logging.getLogger(__name__)

PR_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)"
    r"(?:[/?#].*)?$"
)
GITHUB_REPO = re.compile(
    r"(?:github\.com[/:]|^github:)(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?(?:[/#?].*)?$"
)
_NOT_OWNERS = frozenset({"sponsors", "orgs", "users", "apps", "marketplace", "features"})
RETRYABLE = frozenset({429, 500, 502, 503, 504})

MAX_TARBALL_BYTES = 200 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class PullRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_pr_url(url: str) -> PullRef | None:
    match = PR_URL.match(url.strip())
    if not match:
        return None
    return PullRef(match.group("owner"), match.group("repo"), int(match.group("number")))


def parse_github_repo(url: str | None) -> tuple[str, str] | None:
    """``owner, repo`` from any GitHub URL form (https, git+https, ssh, ``github:o/r``)."""
    if not url:
        return None
    match = GITHUB_REPO.search(url.strip())
    if not match or match.group("owner").lower() in _NOT_OWNERS:
        return None
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return match.group("owner"), repo


def token_from_env(env: Mapping[str, str]) -> str | None:
    return env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or None


class GitHubClient:
    def __init__(
        self,
        *,
        api_url: str = "https://api.github.com",
        raw_url: str = "https://raw.githubusercontent.com",
        token: str | None = None,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"upgrade-impact/{__version__}",
        }
        self._token = token
        self._api = api_url.rstrip("/")
        self._raw = raw_url.rstrip("/")
        self._client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
            follow_redirects=True,
        )
        self._max_retries = max_retries
        self._sleep = sleep

    def _auth(self, url: str) -> dict[str, str]:
        # The token only ever goes to the configured API host, never to raw or redirect hosts.
        if self._token and url.startswith(self._api):
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _get(self, url: str, *, params: Mapping[str, str | int] | None = None) -> httpx.Response:
        last: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(url, params=params, headers=self._auth(url))
            except httpx.HTTPError as exc:
                if attempt == self._max_retries:
                    raise GitHubError(f"GET {url} failed: {exc}") from exc
                self._sleep(min(8.0, 0.5 * 2**attempt))
                continue
            if response.status_code not in RETRYABLE or attempt == self._max_retries:
                return response
            last = response
            self._sleep(min(8.0, 0.5 * 2**attempt))
        assert last is not None
        return last

    def _json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        url = f"{self._api}{path}"
        response = self._get(url, params=params)
        if response.status_code == 404:
            raise GitHubError(f"not found: {url}", status=404)
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError(
                "GitHub API rate limit reached; set GITHUB_TOKEN to raise it", status=403
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"GET {url} returned HTTP {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )
        return response.json()

    def pull_request(self, ref: PullRef) -> dict[str, Any]:
        data = self._json(f"/repos/{ref.slug}/pulls/{ref.number}")
        if not isinstance(data, dict):
            raise GitHubError("unexpected pull request payload")
        return data

    def pull_request_files(self, ref: PullRef, *, max_pages: int = 10) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self._json(
                f"/repos/{ref.slug}/pulls/{ref.number}/files",
                params={"per_page": 100, "page": page},
            )
            if not isinstance(batch, list) or not batch:
                break
            files.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
        return files

    def releases(self, owner: str, repo: str, *, max_pages: int = 3) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self._json(
                f"/repos/{owner}/{repo}/releases", params={"per_page": 100, "page": page}
            )
            if not isinstance(batch, list) or not batch:
                break
            out.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
        return out

    def raw_file(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None:
        """A file's text from raw.githubusercontent.com, or ``None`` if it does not exist."""
        url = f"{self._raw}/{owner}/{repo}/{ref}/{path.lstrip('/')}"
        response = self._get(url)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GitHubError(
                f"GET {url} returned HTTP {response.status_code}", status=response.status_code
            )
        return response.text

    def download_source(
        self, owner: str, repo: str, ref: str, dest: Path, suffixes: Iterable[str]
    ) -> int:
        """Extract the files with these suffixes from the repository tarball at ``ref``.

        Returns the number of files written. Only regular files with safe relative
        paths are extracted; the tarball's top-level directory is stripped.
        """
        url = f"{self._api}/repos/{owner}/{repo}/tarball/{ref}"
        response = self._get(url)
        if response.status_code >= 400:
            raise GitHubError(
                f"could not download {owner}/{repo}@{ref}: HTTP {response.status_code}",
                status=response.status_code,
            )
        if len(response.content) > MAX_TARBALL_BYTES:
            raise GitHubError(f"{owner}/{repo}@{ref} is larger than {MAX_TARBALL_BYTES} bytes")
        return extract_source(response.content, dest, suffixes)

    def close(self) -> None:
        self._client.close()


def extract_source(data: bytes, dest: Path, suffixes: Iterable[str]) -> int:
    wanted = tuple(suffixes)
    written = 0
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive:
            if not member.isfile() or member.size > MAX_SOURCE_FILE_BYTES:
                continue
            parts = PurePosixPath(member.name).parts[1:]
            if not parts or any(p in ("..", "") or p.startswith("/") for p in parts):
                continue
            relative = PurePosixPath(*parts)
            if not relative.name.endswith(wanted):
                continue
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read())
            written += 1
    return written
