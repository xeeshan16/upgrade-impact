from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.test_eval_metrics import make_case
from upgrade_impact.eval.dataset import Case
from upgrade_impact.eval.snapshot import is_source, package_usages
from upgrade_impact.eval.systems import (
    CaseContext,
    ImportGrep,
    NaivePrompt,
    Prediction,
    SymbolGrep,
    import_pattern,
    note_identifiers,
    report_places,
)
from upgrade_impact.models import (
    BumpReport,
    Claim,
    ClaimKind,
    ClaimResult,
    ClaimStatus,
    Ecosystem,
    Match,
    MatchStrength,
    NotesSection,
    ReleaseNotes,
    Usage,
    UsageKind,
)
from upgrade_impact.providers.base import Completion, Message, ProviderError
from upgrade_impact.providers.fake import ScriptedChat

NOTES = ReleaseNotes.from_sections(
    "httpx",
    [
        NotesSection(
            version="0.28.0",
            source="changelog",
            lines=[
                "The deprecated `app` argument has now been removed.",
                "The `proxies` argument has been removed; use `proxy`.",
                "Added `httpx.SSLContext` support.",  # not a breaking line
            ],
        )
    ],
)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def ctx(tmp_path: Path) -> CaseContext:
    write(
        tmp_path,
        "app/main.py",
        "import httpx\n\nclient = httpx.AsyncClient(app=object())\nproxy = 1\n",
    )
    write(tmp_path, "app/util.py", "from httpx import get\n\nget('x', proxies={})\n")
    write(tmp_path, "app/other.py", "import requests\napp = 1\n")
    case = make_case("acme/a#1:httpx", "acme/a")
    return CaseContext(
        case=case, root=tmp_path, notes=NOTES, usages=package_usages(tmp_path, case.bump)
    )


def test_import_grep_flags_every_import_line(ctx: CaseContext) -> None:
    prediction = ImportGrep().predict(ctx)
    assert prediction.affected == {("app/main.py", 1), ("app/util.py", 1)}
    assert prediction.places("lenient") == prediction.places("strict")


def test_note_identifiers_only_from_breaking_lines() -> None:
    assert note_identifiers(NOTES, "httpx", ["httpx"]) == {"app", "proxies", "proxy"}


def test_symbol_grep_greps_note_identifiers_in_importing_files(ctx: CaseContext) -> None:
    prediction = SymbolGrep().predict(ctx)
    # "proxy" also matches the unrelated variable on main.py:4; app/other.py does not import httpx.
    assert prediction.affected == {("app/main.py", 3), ("app/main.py", 4), ("app/util.py", 3)}


def test_import_pattern_for_npm() -> None:
    case = make_case("acme/web#2:axios", "acme/web").model_copy(
        update={"ecosystem": Ecosystem.NPM, "package": "axios"}
    )
    pattern = import_pattern(case, ["axios"])
    for line in (
        "import axios from 'axios'",
        'const axios = require("axios")',
        "import { AxiosError } from 'axios/lib/core'",
        "const m = await import('axios')",
        "import 'axios'",
    ):
        assert pattern.search(line), line
    assert not pattern.search("import x from 'axios-retry'")


def test_naive_prompt_keeps_only_shown_lines(ctx: CaseContext) -> None:
    reply = {
        "affected": [
            {"path": "app/main.py", "line": 3, "reason": "app= removed"},
            {"path": "app/main.py", "line": 99, "reason": "past the end"},
            {"path": "app/other.py", "line": 1, "reason": "not shown"},
            {"path": "app/util.py", "line": "3", "reason": "line is a string"},
            "junk",
        ]
    }
    chat = ScriptedChat([json.dumps(reply)])
    prediction = NaivePrompt(chat).predict(ctx)
    assert prediction.affected == {("app/main.py", 3)}
    assert prediction.runs[0].calls == 1
    prompt = chat.requests[0][1].content
    assert "<<<FILE app/main.py" in prompt
    assert "app/other.py" not in prompt
    assert "3| client = httpx.AsyncClient(app=object())" in prompt


def test_naive_prompt_truncates_to_budget(ctx: CaseContext) -> None:
    naive = NaivePrompt(ScriptedChat([]), notes_tokens=10, code_tokens=5)
    prompt, shown = naive.build_prompt(ctx)
    assert "[... notes truncated]" in prompt
    assert list(shown) == ["app/main.py"]


class FailingChat(ScriptedChat):
    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion:
        raise ProviderError("down")


def test_naive_prompt_records_provider_errors_and_bad_json(ctx: CaseContext) -> None:
    failed = NaivePrompt(FailingChat([])).predict(ctx)
    assert failed.error == "down"
    garbage = NaivePrompt(ScriptedChat(["not json"])).predict(ctx)
    assert garbage.error is None
    assert garbage.affected == set()


def _usage(path: str, line: int, kind: UsageKind = UsageKind.CALL) -> Usage:
    return Usage(path=path, line=line, column=0, symbol="httpx.AsyncClient", kind=kind, snippet="")


def test_report_places_splits_affected_and_possible() -> None:
    claim = Claim(id="c1", kind=ClaimKind.REMOVED, summary="s")
    report = BumpReport(
        bump=make_case("x#1:httpx", "acme/x").bump,
        results=[
            ClaimResult(
                claim=claim,
                status=ClaimStatus.AFFECTED,
                matches=[
                    Match(usage=_usage("a.py", 1), strength=MatchStrength.DIRECT, reason=""),
                    Match(usage=_usage("a.py", 5), strength=MatchStrength.SYMBOL, reason=""),
                ],
            ),
            ClaimResult(
                claim=claim,
                status=ClaimStatus.NEEDS_REVIEW,
                matches=[
                    Match(
                        usage=_usage("b.py", 1, UsageKind.IMPORT),
                        strength=MatchStrength.IMPORT,
                        reason="",
                    )
                ],
            ),
            ClaimResult(
                claim=claim,
                status=ClaimStatus.POSSIBLE,
                matches=[
                    Match(usage=_usage("a.py", 1), strength=MatchStrength.SYMBOL, reason=""),
                    Match(
                        usage=_usage("c.py", 2, UsageKind.IMPORT),
                        strength=MatchStrength.IMPORT,
                        reason="",
                    ),
                ],
            ),
        ],
    )
    affected, possible = report_places(report)
    assert affected == {("a.py", 1)}
    assert possible == {("a.py", 5)}


def test_prediction_modes() -> None:
    p = Prediction(affected={("a", 1)}, possible={("b", 2)})
    assert p.places("strict") == {("a", 1)}
    assert p.places("lenient") == {("a", 1), ("b", 2)}


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("app/main.py", True),
        ("web/src/index.tsx", True),
        ("web/types/index.d.ts", False),
        ("static/app.min.js", False),
        ("node_modules/x/index.js", False),
        ("README.md", False),
    ],
)
def test_is_source(path: str, source: bool) -> None:
    assert is_source(path) is source


def test_case_bump_property() -> None:
    case: Case = make_case("x#1:httpx", "acme/x")
    assert case.bump.label == "httpx 0.27.2 -> 0.28.0 (pypi)"
    assert case.label_paths == set()
