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

    # Instance method calls are not resolved without type information.
    assert not any(u.symbol.endswith(".get") and u.line == 14 for u in index.usages)


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
