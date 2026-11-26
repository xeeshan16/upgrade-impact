from __future__ import annotations

import pytest

from upgrade_impact.models import (
    Bump,
    BumpReport,
    Ecosystem,
    ModelRun,
    Report,
    TokenUsage,
)
from upgrade_impact.pricing import FREE, Price, account, price_for
from upgrade_impact.render import render_markdown, render_text, total_line


def _run(provider: str, model: str, calls: int, cached: int, tin: int, tout: int) -> ModelRun:
    return ModelRun(
        provider=provider,
        model=model,
        calls=calls,
        cached_calls=cached,
        usage=TokenUsage(input_tokens=tin, output_tokens=tout),
        latency_s=1.5,
    )


def _bump(package: str) -> BumpReport:
    return BumpReport(
        bump=Bump(
            ecosystem=Ecosystem.PYPI, package=package, from_version="1", to_version="2", source="t"
        ),
        notes_lines=3,
    )


def test_longest_table_key_wins() -> None:
    price, basis = price_for("openai", "gpt-4.1-mini-2025-04-14", {})
    assert price == Price(0.40, 1.60)
    assert basis == "list price"
    assert price_for("openai", "gpt-4.1", {})[0] == Price(2.00, 8.00)


def test_local_providers_are_free_and_unknown_models_unpriced() -> None:
    assert price_for("ollama", "qwen2.5:7b", {}) == (FREE, "local")
    assert price_for("fake", "fake-extractor", {}) == (FREE, "local")
    assert price_for("openai-compatible", "some-vllm-model", {}) == (None, "unknown")


def test_env_override_wins_even_for_local() -> None:
    env = {"UPGRADE_IMPACT_PRICE_IN": "1", "UPGRADE_IMPACT_PRICE_OUT": "2"}
    assert price_for("ollama", "qwen2.5:7b", env) == (Price(1.0, 2.0), "override")


@pytest.mark.parametrize(
    "env",
    [
        {"UPGRADE_IMPACT_PRICE_IN": "cheap"},
        {"UPGRADE_IMPACT_PRICE_IN": "-1", "UPGRADE_IMPACT_PRICE_OUT": "1"},
    ],
)
def test_bad_override_is_ignored(env: dict[str, str]) -> None:
    assert price_for("openai", "gpt-4o-mini", env) == (Price(0.15, 0.60), "list price")


def test_account_sums_every_stage_of_every_bump() -> None:
    first, second = _bump("httpx"), _bump("rich")
    first.model = _run("openai", "gpt-4.1-mini", 3, 1, 10_000, 1_000)
    first.adjudication = _run("openai", "gpt-4.1-mini", 2, 0, 4_000, 500)
    second.recheck = _run("openai", "gpt-4.1-mini", 1, 0, 1_000, 100)
    report = Report(source="s", repository_root="/r", bumps=[first, second])
    acc = account(report, {})
    assert acc is not None
    assert (acc.calls, acc.cached_calls) == (6, 1)
    assert (acc.input_tokens, acc.output_tokens) == (15_000, 1_600)
    assert acc.model_latency_s == 4.5
    # 15k * 0.40 / 1M + 1.6k * 1.60 / 1M
    assert acc.cost_usd == pytest.approx(0.00856)
    assert acc.price_basis == "list price"


def test_no_model_means_no_accounting() -> None:
    report = Report(source="s", repository_root="/r", bumps=[_bump("httpx")])
    assert account(report, {}) is None
    assert total_line(report) is None
    assert "total:" not in render_text(report)


def test_total_line_in_text_and_markdown() -> None:
    bump = _bump("httpx")
    bump.model = _run("ollama", "qwen2.5:7b", 4, 2, 9_000, 800)
    report = Report(source="s", repository_root="/r", bumps=[bump], elapsed_s=12.34)
    report.accounting = account(report, {})
    line = total_line(report)
    assert line == (
        "total: 4 model call(s) (2 from cache), 9,000 in / 800 out tokens, $0 (local model), 12.3 s"
    )
    assert render_text(report).rstrip().endswith(line)
    assert f"<sub>{line}</sub>" in render_markdown(report)


def test_unknown_price_says_how_to_set_one() -> None:
    bump = _bump("httpx")
    bump.model = _run("openai-compatible", "my-model", 1, 0, 100, 10)
    report = Report(source="s", repository_root="/r", bumps=[bump])
    report.accounting = account(report, {})
    assert "cost unknown (set UPGRADE_IMPACT_PRICE_IN/_OUT)" in (total_line(report) or "")
    assert report.accounting is not None
    assert report.accounting.cost_usd is None
