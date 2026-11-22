from __future__ import annotations

from pathlib import Path

import pytest

from upgrade_impact.index.javascript import index_js_repository
from upgrade_impact.match import match_claims
from upgrade_impact.models import Claim, ClaimKind, ClaimStatus, Usage, UsageKind


@pytest.fixture
def jsapp(fixtures: Path) -> Path:
    return fixtures / "repos" / "jsapp"


def _by_place(usages: list[Usage]) -> dict[tuple[str, int, str, UsageKind], Usage]:
    return {(u.path, u.line, u.symbol, u.kind): u for u in usages}


def test_esm_imports_calls_options_and_types(jsapp: Path) -> None:
    index = index_js_repository(jsapp, ["axios"])
    by_place = _by_place(index.usages)

    assert ("src/api.ts", 1, "axios", UsageKind.IMPORT) in by_place
    assert ("src/api.ts", 1, "axios.AxiosError", UsageKind.IMPORT) in by_place
    # A TypeScript type annotation is a use of the type.
    assert ("src/api.ts", 4, "axios.AxiosInstance", UsageKind.NAME) in by_place
    create = by_place[("src/api.ts", 4, "axios.create", UsageKind.CALL)]
    assert create.keywords == ["baseURL", "timeout"]
    # Object-literal keys are the options passed; a spread may pass anything.
    get = by_place[("src/api.ts", 8, "axios.get", UsageKind.CALL)]
    assert get.keywords == ["**", "withCredentials"]
    assert get.snippet.startswith("const res = await axios.get(")
    # An aliased named import resolves to the package's name for it.
    assert ("src/api.ts", 11, "axios.AxiosError", UsageKind.NAME) in by_place
    # Re-exporting from the package is a use of it.
    assert ("src/api.ts", 18, "axios.isCancel", UsageKind.IMPORT) in by_place


def test_commonjs_require_and_destructuring(jsapp: Path) -> None:
    by_place = _by_place(index_js_repository(jsapp, ["axios"]).usages)
    post = by_place[("src/legacy.cjs", 5, "axios.post", UsageKind.CALL)]
    assert post.keywords == ["data", "proxy"]
    assert ("src/legacy.cjs", 6, "axios.put", UsageKind.CALL) in by_place  # renamed binding
    assert ("src/legacy.cjs", 7, "axios.CancelToken.source", UsageKind.CALL) in by_place
    assert ("src/legacy.cjs", 7, "axios.all", UsageKind.CALL) in by_place


def test_barrel_reexports_are_followed(jsapp: Path) -> None:
    by_place = _by_place(index_js_repository(jsapp, ["axios"]).usages)
    call = by_place[("src/components/App.tsx", 9, "axios.isCancel", UsageKind.CALL)]
    assert call.via == "re-exported by src/api.ts"


def test_scoped_package_jsx_instances_and_nested_options(jsapp: Path) -> None:
    index = index_js_repository(jsapp, ["@tanstack/react-query"])
    by_place = _by_place(index.usages)
    pkg = "@tanstack/react-query"
    client = by_place[("src/components/App.tsx", 4, f"{pkg}.QueryClient", UsageKind.CALL)]
    assert client.keywords == ["defaultOptions", "queries", "cacheTime"]
    hook = by_place[("src/components/App.tsx", 7, f"{pkg}.useQuery", UsageKind.CALL)]
    assert hook.keywords == ["queryKey", "queryFn", "cacheTime"]
    method = by_place[
        ("src/components/App.tsx", 8, f"{pkg}.QueryClient.invalidateQueries", UsageKind.CALL)
    ]
    assert method.via == f"queryClient = new {pkg}.QueryClient at line 4"
    provider = by_place[
        ("src/components/App.tsx", 14, f"{pkg}.QueryClientProvider", UsageKind.CALL)
    ]
    assert provider.keywords == ["client", "**"]
    assert {u.path for u in index.usages} == {"src/components/App.tsx"}


def test_skips_node_modules_and_survives_syntax_errors(jsapp: Path) -> None:
    index = index_js_repository(jsapp, ["axios"])
    assert not any("node_modules" in u.path for u in index.usages)
    assert index.stats.files_scanned == 4
    assert index.stats.parse_errors == 1  # broken.js, still indexed up to the error
    assert ("src/broken.js", 1, "axios.get", UsageKind.CALL) in _by_place(index.usages)
    assert index.stats.usages == len(index.usages)


def test_option_claims_match_js_calls(jsapp: Path) -> None:
    pkg = "@tanstack/react-query"
    usages = index_js_repository(jsapp, [pkg]).usages
    renamed = Claim(
        id="C1",
        kind=ClaimKind.RENAMED,
        summary="cacheTime was renamed to gcTime",
        symbols=["useQuery"],
        parameters=["cacheTime"],
    )
    removed_hook = Claim(
        id="C2", kind=ClaimKind.REMOVED, summary="useQueries removed", symbols=["useQueries"]
    )
    results = {r.claim.id: r for r in match_claims([renamed, removed_hook], usages, [pkg])}
    assert results["C1"].status == ClaimStatus.AFFECTED
    assert [(m.usage.line, m.reason) for m in results["C1"].matches] == [(7, "passes cacheTime")]
    assert results["C2"].status == ClaimStatus.NOT_FOUND
