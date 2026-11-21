"""Find a package's source repository from the PyPI or npm registry."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from upgrade_impact import __version__
from upgrade_impact.models import Ecosystem
from upgrade_impact.sources.github import parse_github_repo

# PyPI ``project_urls`` keys that usually point at the code, most specific first.
_SOURCE_KEYS = (
    "source",
    "source code",
    "sourcecode",
    "repository",
    "code",
    "github",
    "git",
    "homepage",
    "home",
)
_CHANGELOG_KEYS = ("changelog", "changes", "release notes", "releasenotes", "history", "news")


class RegistryError(RuntimeError):
    pass


@dataclass(slots=True)
class PackageInfo:
    ecosystem: Ecosystem
    name: str
    repository: tuple[str, str] | None = None
    directory: str | None = None
    changelog_url: str | None = None
    versions: list[str] = field(default_factory=list)


def _pick_repo(urls: dict[str, str]) -> tuple[str, str] | None:
    lowered = {k.strip().lower(): v for k, v in urls.items() if isinstance(v, str)}
    for key in _SOURCE_KEYS:
        repo = parse_github_repo(lowered.get(key))
        if repo:
            return repo
    for value in lowered.values():
        repo = parse_github_repo(value)
        if repo:
            return repo
    return None


def pypi_info(data: dict[str, Any], name: str) -> PackageInfo:
    info = data.get("info") or {}
    urls: dict[str, str] = dict(info.get("project_urls") or {})
    if info.get("home_page"):
        urls.setdefault("homepage", info["home_page"])
    changelog = next(
        (v for k, v in urls.items() if k.strip().lower() in _CHANGELOG_KEYS and v), None
    )
    return PackageInfo(
        ecosystem=Ecosystem.PYPI,
        name=info.get("name") or name,
        repository=_pick_repo(urls),
        changelog_url=changelog,
        versions=list((data.get("releases") or {}).keys()),
    )


def npm_info(data: dict[str, Any], name: str) -> PackageInfo:
    repository = data.get("repository")
    url: str | None = None
    directory: str | None = None
    if isinstance(repository, str):
        url = repository
    elif isinstance(repository, dict):
        url = repository.get("url")
        directory = repository.get("directory")
    repo = parse_github_repo(url)
    if repo is None:
        repo = parse_github_repo(str(data.get("homepage") or ""))
    return PackageInfo(
        ecosystem=Ecosystem.NPM,
        name=data.get("name") or name,
        repository=repo,
        directory=directory.strip("/") if isinstance(directory, str) else None,
        versions=list((data.get("versions") or {}).keys()),
    )


class RegistryClient:
    def __init__(
        self,
        *,
        pypi_url: str = "https://pypi.org",
        npm_url: str = "https://registry.npmjs.org",
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pypi = pypi_url.rstrip("/")
        self._npm = npm_url.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": f"upgrade-impact/{__version__}"},
        )
        self._sleep = sleep

    def _get_json(self, url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        for attempt in range(3):
            try:
                response = self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise RegistryError(f"GET {url} failed: {exc}") from exc
                self._sleep(0.5 * 2**attempt)
                continue
            if response.status_code == 404:
                raise RegistryError(f"package not found: {url}")
            if response.status_code >= 500 and attempt < 2:
                self._sleep(0.5 * 2**attempt)
                continue
            if response.status_code >= 400:
                raise RegistryError(f"GET {url} returned HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict):
                raise RegistryError(f"unexpected payload from {url}")
            return data
        raise RegistryError(f"GET {url} failed")  # pragma: no cover - loop always returns

    def package(self, ecosystem: Ecosystem, name: str) -> PackageInfo:
        if ecosystem == Ecosystem.PYPI:
            return pypi_info(self._get_json(f"{self._pypi}/pypi/{quote(name)}/json"), name)
        # The abbreviated document is much smaller than the full one but has no
        # "repository" field, so ask for the full one.
        encoded = quote(name, safe="@")
        encoded = encoded.replace("/", "%2F")
        return npm_info(self._get_json(f"{self._npm}/{encoded}"), name)

    def close(self) -> None:
        self._client.close()
