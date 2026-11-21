from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from tests.mock_services import NPM_EXPRESS, PYPI_HTTPX
from upgrade_impact.models import Ecosystem
from upgrade_impact.sources.registry import RegistryClient, RegistryError, npm_info, pypi_info


def test_pypi_info_prefers_the_source_url_over_funding() -> None:
    info = pypi_info(PYPI_HTTPX, "httpx")
    assert info.repository == ("encode", "httpx")
    assert info.changelog_url == "https://github.com/encode/httpx/blob/master/CHANGELOG.md"
    assert "0.28.1" in info.versions


def test_pypi_info_falls_back_to_home_page_and_any_github_url() -> None:
    home: dict[str, Any] = {
        "info": {"name": "x", "home_page": "https://github.com/o/x", "project_urls": None}
    }
    assert pypi_info(home, "x").repository == ("o", "x")
    tracker: dict[str, Any] = {
        "info": {"name": "x", "project_urls": {"Bug Tracker": "https://github.com/o/x/issues"}}
    }
    assert pypi_info(tracker, "x").repository == ("o", "x")
    assert pypi_info({"info": {"name": "x"}}, "x").repository is None


def test_npm_info_variants() -> None:
    assert npm_info(NPM_EXPRESS, "express").repository == ("expressjs", "express")
    mono = {
        "name": "@babel/core",
        "repository": {
            "url": "https://github.com/babel/babel.git",
            "directory": "packages/babel-core",
        },
    }
    info = npm_info(mono, "@babel/core")
    assert (info.repository, info.directory) == (("babel", "babel"), "packages/babel-core")
    assert npm_info({"repository": "github:o/r"}, "r").repository == ("o", "r")
    assert npm_info({"homepage": "https://github.com/o/r#readme"}, "r").repository == ("o", "r")


def test_client_urls_and_errors() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "missing" in request.url.path:
            return httpx.Response(404)
        if "flaky" in request.url.path and len(seen) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"name": "n", "info": {"name": "n"}})

    client = RegistryClient(transport=httpx.MockTransport(handler), sleep=lambda _: None)
    client.package(Ecosystem.NPM, "@types/node")
    client.package(Ecosystem.PYPI, "httpx")
    assert seen == [
        "https://registry.npmjs.org/@types%2Fnode",
        "https://pypi.org/pypi/httpx/json",
    ]
    with pytest.raises(RegistryError, match="not found"):
        client.package(Ecosystem.PYPI, "missing")
    seen.clear()
    client.package(Ecosystem.PYPI, "flaky")
    assert len(seen) == 3


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("UPGRADE_IMPACT_LIVE"), reason="UPGRADE_IMPACT_LIVE not set")
def test_live_registries() -> None:
    client = RegistryClient()
    assert client.package(Ecosystem.PYPI, "httpx").repository == ("encode", "httpx")
    assert client.package(Ecosystem.NPM, "express").repository == ("expressjs", "express")
