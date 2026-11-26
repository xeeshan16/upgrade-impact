"""What a check costs: model calls, tokens, an estimated price, and time.

A grouped Dependabot PR can make dozens of model calls. Someone choosing between
a hosted ``gpt-4.1-mini`` and a local ``qwen2.5`` wants the bill in the report,
not a surprise at the end of the month.

The built-in table holds *indicative published list prices* in USD per million
tokens, entered by hand and easy to fall out of date. They are a convenience,
not a quote: ``UPGRADE_IMPACT_PRICE_IN`` and ``UPGRADE_IMPACT_PRICE_OUT`` (USD
per million input / output tokens) price with your own contract and always win.
Local providers (Ollama, the fake test provider) are free. An unknown hosted
model gets no price at all rather than a guessed one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from upgrade_impact.models import Accounting, ModelRun, Report

log = logging.getLogger(__name__)

FREE_PROVIDERS = frozenset({"ollama", "fake"})


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok
        ) / 1_000_000


FREE = Price(0.0, 0.0)

# Indicative published list prices (USD / 1M tokens). Matched case-insensitively
# by the longest key that is a substring of the model name, so "gpt-4.1-mini"
# wins over "gpt-4.1". Override with the env vars above.
DEFAULT_PRICES: dict[str, Price] = {
    "gpt-4.1-mini": Price(0.40, 1.60),
    "gpt-4.1-nano": Price(0.10, 0.40),
    "gpt-4.1": Price(2.00, 8.00),
    "gpt-4o-mini": Price(0.15, 0.60),
    "gpt-4o": Price(2.50, 10.00),
    "o4-mini": Price(1.10, 4.40),
    "claude-haiku-4-5": Price(1.00, 5.00),
    "claude-3-5-haiku": Price(0.80, 4.00),
    "claude-sonnet": Price(3.00, 15.00),
    "claude-opus": Price(15.00, 75.00),
}


def _env_price(env: Mapping[str, str]) -> Price | None:
    raw_in = env.get("UPGRADE_IMPACT_PRICE_IN")
    raw_out = env.get("UPGRADE_IMPACT_PRICE_OUT")
    if raw_in is None and raw_out is None:
        return None
    try:
        price = Price(float(raw_in or 0.0), float(raw_out or 0.0))
    except ValueError:
        log.warning(
            "ignoring non-numeric price override", extra={"price_in": raw_in, "price_out": raw_out}
        )
        return None
    if price.input_per_mtok < 0 or price.output_per_mtok < 0:
        log.warning("ignoring negative price override")
        return None
    return price


def _table_price(model: str) -> Price | None:
    needle = model.casefold()
    best: tuple[int, Price] | None = None
    for key, price in DEFAULT_PRICES.items():
        if key in needle and (best is None or len(key) > best[0]):
            best = (len(key), price)
    return best[1] if best else None


def price_for(provider: str, model: str, env: Mapping[str, str]) -> tuple[Price | None, str]:
    """The price for one model and where it came from.

    Returns ``(price, basis)`` with basis ``override``, ``local``, ``list price`` or
    ``unknown`` (price ``None``: don't guess a cost, which is not the same as free).
    """
    override = _env_price(env)
    if override is not None:
        return override, "override"
    if provider in FREE_PROVIDERS:
        return FREE, "local"
    table = _table_price(model)
    if table is not None:
        return table, "list price"
    return None, "unknown"


def runs_of(report: Report) -> Iterable[ModelRun]:
    """Every model run in a report: extraction, second reading and code review."""
    for bump in report.bumps:
        for run in (bump.model, bump.recheck, bump.adjudication):
            if run is not None:
                yield run


def account(report: Report, env: Mapping[str, str]) -> Accounting | None:
    """Totals over every model call of the check, or ``None`` when no model ran.

    Cached replies count as calls but carry no tokens, so they cost nothing.
    """
    runs = list(runs_of(report))
    if not runs:
        return None
    first = runs[0]
    input_tokens = sum(r.usage.input_tokens for r in runs)
    output_tokens = sum(r.usage.output_tokens for r in runs)
    price, basis = price_for(first.provider, first.model, env)
    return Accounting(
        provider=first.provider,
        model=first.model,
        calls=sum(r.calls for r in runs),
        cached_calls=sum(r.cached_calls for r in runs),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_latency_s=round(sum(r.latency_s for r in runs), 3),
        cost_usd=None if price is None else round(price.cost(input_tokens, output_tokens), 6),
        price_basis=basis,
    )
