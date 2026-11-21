from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx
import pytest

from upgrade_impact.sources.github import (
    GitHubClient,
    GitHubError,
    PullRef,
    extract_source,
    parse_github_repo,
    parse_pr_url,
    token_from_env,
)


def test_parse_pr_url() -> None:
    assert parse_pr_url("https://github.com/encode/httpx/pull/3000") == PullRef(
        "encode", "httpx", 3000
    )
    assert parse_pr_url("https://github.com/o/r.js/pull/7/files?x=1") == PullRef("o", "r.js", 7)
    assert parse_pr_url("https://github.com/o/r/issues/7") is None
    assert parse_pr_url("https://gitlab.com/o/r/-/merge_requests/1") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/encode/httpx", ("encode", "httpx")),
        ("git+https://github.com/expressjs/express.git", ("expressjs", "express")),
        ("git@github.com:pallets/click.git", ("pallets", "click")),
        ("github:vitest-dev/vitest", ("vitest-dev", "vitest")),
        ("https://github.com/encode/httpx/blob/master/CHANGELOG.md", ("encode", "httpx")),
        ("https://github.com/sponsors/encode", None),
        ("https://www.encode.io/httpx", None),
        (None, None),
    ],
)
def test_parse_github_repo(url: str | None, expected: tuple[str, str] | None) -> None:
    assert parse_github_repo(url) == expected


def test_token_from_env() -> None:
    assert token_from_env({"GH_TOKEN": "b"}) == "b"
    assert token_from_env({"GITHUB_TOKEN": "a", "GH_TOKEN": "b"}) == "a"
    assert token_from_env({}) is None


def _client(handler, token: str | None = None) -> GitHubClient:  # type: ignore[no-untyped-def]
    return GitHubClient(transport=httpx.MockTransport(handler), token=token, sleep=lambda _: None)


def test_files_and_releases_paginate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = 100 if page == 1 else 3
        return httpx.Response(200, json=[{"filename": f"f{page}-{i}"} for i in range(size)])

    client = _client(handler)
    assert len(client.pull_request_files(PullRef("o", "r", 1))) == 103
    assert len(client.releases("o", "r")) == 103


def test_retries_server_errors_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(502)
        return httpx.Response(200, json={"title": "t"})

    assert _client(handler).pull_request(PullRef("o", "r", 1)) == {"title": "t"}
    assert len(calls) == 3


def test_errors_are_explained() -> None:
    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={})

    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        _client(rate_limited).pull_request(PullRef("o", "r", 1))

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    with pytest.raises(GitHubError, match="not found") as info:
        _client(missing).releases("o", "r")
    assert info.value.status == 404
    assert _client(missing).raw_file("o", "r", "CHANGELOG.md") is None

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(GitHubError, match="boom"):
        _client(broken).pull_request(PullRef("o", "r", 1))


def test_token_only_goes_to_the_api_host() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("authorization")
        return (
            httpx.Response(200, text="# Changelog", json=None)
            if request.url.host.startswith("raw")
            else httpx.Response(200, json={})
        )

    client = _client(handler, token="secret")
    client.pull_request(PullRef("o", "r", 1))
    client.raw_file("o", "r", "CHANGELOG.md")
    assert seen == {"api.github.com": "Bearer secret", "raw.githubusercontent.com": None}


def _tar(entries: dict[str, bytes], *, link: bool = False) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo("top/evil.py")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
    return buffer.getvalue()


def test_extract_source_is_safe_and_filtered(tmp_path: Path) -> None:
    data = _tar(
        {
            "top/pkg/a.py": b"import httpx\n",
            "top/pkg/b.txt": b"not code",
            "top/../../escape.py": b"x",
            "top/pkg/stubs.pyi": b"x: int\n",
        },
        link=True,
    )
    count = extract_source(data, tmp_path / "out", (".py", ".pyi"))
    assert count == 2
    assert (tmp_path / "out" / "pkg" / "a.py").read_text() == "import httpx\n"
    assert not (tmp_path / "out" / "pkg" / "b.txt").exists()
    assert not (tmp_path / "escape.py").exists()
    assert not (tmp_path / "out" / "evil.py").exists()


def test_download_source(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/r/tarball/abc":
            return httpx.Response(200, content=_tar({"o-r-abc/m.py": b"import yaml\n"}))
        return httpx.Response(404)

    client = _client(handler)
    assert client.download_source("o", "r", "abc", tmp_path, (".py",)) == 1
    with pytest.raises(GitHubError, match="HTTP 404"):
        client.download_source("o", "r", "nope", tmp_path, (".py",))
