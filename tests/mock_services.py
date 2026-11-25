"""An in-memory stand-in for the GitHub API, raw.githubusercontent.com, PyPI and npm.

The payloads have the shape of the real services' responses (trimmed to the
fields the tool reads). The PR mirrors a typical Dependabot pip bump.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"

PR = {
    "number": 9,
    "title": "chore(deps): bump httpx from 0.27.0 to 0.28.1",
    "body": "Bumps [httpx](https://github.com/encode/httpx) from 0.27.0 to 0.28.1.\n"
    "<details>\n<summary>Release notes</summary>\n"
    "<blockquote><h2>Version 0.28.1</h2>\n</blockquote>\n</details>",
    "head": {"ref": "dependabot/pip/httpx-0.28.1", "sha": "a" * 40},
    "base": {"ref": "main", "sha": "b" * 40},
}
PR_FILES = [
    {
        "filename": "requirements.txt",
        "status": "modified",
        "patch": "@@ -1,2 +1,2 @@\n-httpx==0.27.0\n+httpx==0.28.1\n rich==13.9.4",
    }
]
# A Dependabot grouped update: httpx is pinned in requirements.txt, rich only
# moved in the lock file, so it is listed in the body alone.
GROUP_PR = {
    "number": 10,
    "title": "Bump the pip group across 1 directory with 2 updates",
    "body": "Bumps the pip group with 2 updates in the / directory: "
    "[httpx](https://github.com/encode/httpx) and [rich](https://github.com/Textualize/rich).\n\n"
    "Updates `httpx` from 0.27.0 to 0.28.1\n<details>\n<summary>Release notes</summary>\n"
    "<blockquote>\nUpdates `h11` from 0.14.0 to 0.16.0\n</blockquote>\n</details>\n\n"
    "Updates `rich` from 13.9.4 to 14.0.0\n<details>\n<summary>Commits</summary>\n</details>\n",
    "head": {"ref": "dependabot/pip/pip-1a2b3c", "sha": "a" * 40},
    "base": {"ref": "main", "sha": "b" * 40},
}
GROUP_PR_FILES = [
    PR_FILES[0],
    {"filename": "uv.lock", "status": "modified", "patch": "@@ -1 +1 @@\n-x\n+y"},
]
RELEASES = [
    {
        "tag_name": "0.28.0",
        "html_url": "https://github.com/encode/httpx/releases/tag/0.28.0",
        "body": "The 0.28 release includes a limited set of deprecations.\r\n\r\n"
        "* The deprecated `proxies` argument has now been removed.\r\n"
        "* The deprecated `app` argument has now been removed.\r\n"
        "* JSON request bodies use a compact representation. (#3363)",
    },
    {
        "tag_name": "0.28.1",
        "body": "* Fix SSL case where `verify=False` together with client side certificates.",
    },
]
PYPI_HTTPX = {
    "info": {
        "name": "httpx",
        "home_page": None,
        "project_urls": {
            "Changelog": "https://github.com/encode/httpx/blob/master/CHANGELOG.md",
            "Funding": "https://github.com/sponsors/encode",
            "Source": "https://github.com/encode/httpx",
        },
    },
    "releases": {v: [] for v in ["0.26.0", "0.27.0", "0.27.1", "0.27.2", "0.28.0", "0.28.1"]},
}
NPM_EXPRESS = {
    "name": "express",
    "repository": {"type": "git", "url": "git+https://github.com/expressjs/express.git"},
    "versions": {"4.21.2": {}, "5.0.0": {}, "5.0.1": {}},
}


def tarball(root: Path, prefix: str = "owner-repo-abc123") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=f"{prefix}/{path.relative_to(root).as_posix()}")
    return buffer.getvalue()


class Services:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "api.github.com":
            routes: dict[str, object] = {
                "/repos/owner/repo/pulls/9": PR,
                "/repos/owner/repo/pulls/9/files": PR_FILES
                if request.url.params.get("page") == "1"
                else [],
                "/repos/owner/repo/pulls/10": GROUP_PR,
                "/repos/owner/repo/pulls/10/files": GROUP_PR_FILES
                if request.url.params.get("page") == "1"
                else [],
                "/repos/encode/httpx/releases": RELEASES,
            }
            if path in routes:
                return httpx.Response(200, json=routes[path])
            if path == f"/repos/owner/repo/tarball/{'a' * 40}":
                return httpx.Response(200, content=tarball(self.repo))
            return httpx.Response(404, json={"message": "Not Found"})
        if host == "raw.githubusercontent.com":
            if path == "/encode/httpx/HEAD/CHANGELOG.md":
                return httpx.Response(
                    200, text=(FIXTURES / "notes" / "httpx-CHANGELOG.md").read_text()
                )
            return httpx.Response(404, text="404: Not Found")
        if host == "pypi.org" and path == "/pypi/httpx/json":
            return httpx.Response(200, text=json.dumps(PYPI_HTTPX))
        if host == "registry.npmjs.org" and path == "/express":
            return httpx.Response(200, json=NPM_EXPRESS)
        return httpx.Response(404)
