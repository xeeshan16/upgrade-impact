from __future__ import annotations

from pathlib import Path

from upgrade_impact.index.python import import_names_for, index_repository
from upgrade_impact.models import UsageKind


def test_index_resolves_aliases_calls_and_keywords(sample_repo: Path) -> None:
    index = index_repository(sample_repo, ["httpx"])
    by_place = {(u.path, u.line, u.symbol, u.kind): u for u in index.usages}

    proxies_call = by_place[("app/http.py", 9, "httpx.Client", UsageKind.CALL)]
    assert proxies_call.keywords == ["proxies", "timeout"]
    assert proxies_call.snippet == "return httpx.Client(proxies=proxy_url, timeout=10)"
    assert proxies_call.column == 12

    aliased = by_place[("app/http.py", 13, "httpx.AsyncClient", UsageKind.CALL)]
    assert aliased.keywords == ["**"]

    assert ("app/http.py", 4, "httpx.AsyncClient", UsageKind.IMPORT) in by_place
    assert ("app/http.py", 17, "httpx.HTTPStatusError", UsageKind.NAME) in by_place
    assert ("app/http.py", 8, "httpx.Client", UsageKind.ATTRIBUTE) in by_place  # annotation
    nested = by_place[("app/http.py", 23, "httpx.Timeout", UsageKind.CALL)]
    assert nested.keywords == []
    assert by_place[("app/http.py", 23, "httpx.get", UsageKind.CALL)].keywords == ["timeout"]

    module_alias = by_place[("tests/test_app.py", 8, "httpx.Client", UsageKind.CALL)]
    assert module_alias.keywords == ["transport"]

    # `async with AC(**options) as client` makes client.get a call to AsyncClient.get.
    method = by_place[("app/http.py", 14, "httpx.AsyncClient.get", UsageKind.CALL)]
    assert method.via == "client is a httpx.AsyncClient at line 13"


def test_index_skips_vendored_dirs_unrelated_files_and_reports_parse_errors(
    sample_repo: Path,
) -> None:
    index = index_repository(sample_repo, ["httpx"])
    paths = {u.path for u in index.usages}
    assert paths == {"app/http.py", "tests/test_app.py"}
    assert index.stats.parse_errors == 1  # app/broken.py
    assert index.stats.files_importing == 2
    assert index.stats.files_scanned == 4  # site-packages is skipped
    assert index.stats.usages == len(index.usages)


def test_dotted_import_names(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "import google.protobuf.message\n"
        "from google.protobuf import json_format as jf\n"
        "import google.auth\n"
        "jf.MessageToDict(google.protobuf.message.Message(), preserving_proto_field_name=True)\n"
        "google.auth.default()\n"
    )
    index = index_repository(tmp_path, ["google.protobuf"])
    calls = [(u.symbol, u.keywords) for u in index.usages if u.kind == UsageKind.CALL]
    assert ("google.protobuf.json_format.MessageToDict", ["preserving_proto_field_name"]) in calls
    assert ("google.protobuf.message.Message", []) in calls
    assert not any(u.symbol.startswith("google.auth") for u in index.usages)


def test_star_imports_relative_imports_and_shadowing(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "from yaml import *\nfrom . import yaml\nfrom .yaml import load\nload('x')\n"
    )
    index = index_repository(tmp_path, ["yaml"])
    assert [(u.symbol, u.kind) for u in index.usages] == [("yaml.*", UsageKind.IMPORT)]


def test_import_names_for() -> None:
    assert import_names_for("PyYAML") == ["yaml"]
    assert import_names_for("httpx") == ["httpx"]
    assert import_names_for("python-dateutil") == ["dateutil"]
    assert import_names_for("typing_extensions") == ["typing_extensions"]
    assert import_names_for("python-gitlab") == ["python_gitlab", "gitlab", "python.gitlab"]
    assert import_names_for("zope-interface") == ["zope_interface", "zope.interface"]


def test_instance_method_calls_are_resolved_from_visible_classes(tmp_path: Path) -> None:
    (tmp_path / "svc.py").write_text(
        "import httpx\n"
        "from typing import Optional\n"
        "\n"
        "def make() -> httpx.Client:\n"
        "    return httpx.Client()\n"
        "\n"
        "class Api:\n"
        "    def __init__(self) -> None:\n"
        "        self.http = httpx.AsyncClient(base_url='x')\n"
        "\n"
        "    async def go(self, other: Optional[httpx.Client], third: 'httpx.Client | None'):\n"
        "        await self.http.post('/a', data={'k': 1})\n"
        "        other.get('/b', allow_redirects=True)\n"
        "        third.stream('GET', '/c')\n"
        "\n"
        "c = make()\n"
        "c.request('GET', '/d', **opts)\n"
        "httpx.Client().close()\n"
        "send(c)\n"
        "unrelated.get('/e')\n"
    )
    index = index_repository(tmp_path, ["httpx"])
    calls = {(u.line, u.symbol): u for u in index.usages if u.kind == UsageKind.CALL}
    assert calls[(12, "httpx.AsyncClient.post")].keywords == ["data"]
    assert calls[(12, "httpx.AsyncClient.post")].via == ("self.http = httpx.AsyncClient at line 9")
    assert calls[(13, "httpx.Client.get")].keywords == ["allow_redirects"]
    assert calls[(13, "httpx.Client.get")].via == "other is annotated httpx.Client at line 11"
    assert (14, "httpx.Client.stream") in calls
    assert calls[(17, "httpx.Client.request")].keywords == ["**"]
    assert calls[(17, "httpx.Client.request")].via == "c = httpx.Client at line 16"
    assert (18, "httpx.Client.close") in calls
    # Passing the instance around is not a use of the class name.
    assert not any(u.line == 19 for u in index.usages)
    assert not any(u.line == 20 for u in index.usages)


def test_local_reexports_are_followed(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "app"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "compat.py").write_text("from httpx import Client as HttpClient, Timeout\n")
    (pkg / "service.py").write_text(
        "from .compat import HttpClient\n"
        "from app.compat import Timeout as T\n"
        "client = HttpClient(proxies='http://p', timeout=T(5))\n"
    )
    index = index_repository(tmp_path, ["httpx"])
    service = [u for u in index.usages if u.path == "src/app/service.py"]
    calls = {(u.symbol, tuple(u.keywords)) for u in service if u.kind == UsageKind.CALL}
    assert ("httpx.Client", ("proxies", "timeout")) in calls
    assert ("httpx.Timeout", ()) in calls
    imports = [u for u in service if u.kind == UsageKind.IMPORT]
    assert {u.via for u in imports} == {"re-exported by src/app/compat.py"}
