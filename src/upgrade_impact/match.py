"""Match verified claims to code usages, deterministically.

Strengths:

- ``direct``: the code uses exactly what the claim is about (a removed symbol,
  or a call that passes the affected keyword);
- ``symbol``: the code uses the symbol, but whether the change bites depends on
  how (behaviour changes, calls that forward ``**kwargs``, references);
- ``import``: the claim names no symbol, so every import site is a candidate.

A parameter claim that names no API ("the `app` argument was removed") only
gives ``symbol`` matches: many APIs share parameter names.

A claim about a parameter is *not* matched by calls that visibly don't pass it:
``httpx.AsyncClient(timeout=10)`` is not affected by "the proxies argument was
removed". That is what lets the tool say "no impact" with a reason.
"""

from __future__ import annotations

from collections.abc import Iterable

from upgrade_impact.models import (
    Claim,
    ClaimKind,
    ClaimResult,
    ClaimStatus,
    Match,
    MatchStrength,
    Usage,
    UsageKind,
)

_DIRECT_KINDS = frozenset({ClaimKind.REMOVED, ClaimKind.RENAMED})
_RUNTIME_KINDS = frozenset({ClaimKind.DROPPED_SUPPORT})


def _components(symbol: str, import_names: Iterable[str]) -> list[str]:
    """``httpx._client.Client.get`` -> ``["Client", "get"]``.

    The package prefix and private modules are dropped.
    """

    def dotted(name: str) -> str:
        return name.replace("::", ".").replace("#", ".").replace("/", ".")

    text = dotted(symbol)
    for prefix in sorted((dotted(n) for n in import_names), key=len, reverse=True):
        if text == prefix:
            return []
        if text.startswith(f"{prefix}."):
            text = text[len(prefix) + 1 :]
            break
    parts = [p for p in text.split(".") if p]
    while len(parts) > 1 and parts[0].startswith("_") and not parts[0].startswith("__"):
        parts = parts[1:]
    return parts


def symbol_matches(claim_symbol: str, usage_symbol: str, import_names: Iterable[str]) -> bool:
    names = list(import_names)
    wanted = _components(claim_symbol, names)
    have = _components(usage_symbol, names)
    if not wanted or not have:
        return False
    if have[-len(wanted) :] == wanted:
        return True
    # The claim is more qualified than the usage through module names only:
    # claim "sub.Thing", code "from pkg.sub import Thing" -> usage "pkg.sub.Thing".
    if len(wanted) > len(have) and wanted[-len(have) :] == have:
        return all(part[:1].islower() for part in wanted[: -len(have)])
    return False


def _match_usage(claim: Claim, usage: Usage, import_names: list[str]) -> Match | None:
    params = set(claim.parameters)
    symbol_hit = next(
        (s for s in claim.symbols if symbol_matches(s, usage.symbol, import_names)), None
    )
    if claim.symbols and symbol_hit is None:
        return None
    if params:
        if usage.kind == UsageKind.IMPORT:
            return None
        if usage.kind == UsageKind.CALL:
            passed = params.intersection(usage.keywords)
            if passed:
                names = ", ".join(sorted(passed))
                if symbol_hit is None:
                    # The notes name the parameter but not the API it belonged to, and
                    # many APIs share parameter names (Client(app=) vs ASGITransport(app=)).
                    return Match(
                        usage=usage,
                        strength=MatchStrength.SYMBOL,
                        reason=f"passes {names} to {usage.symbol}; the notes do not say "
                        "which API lost it",
                    )
                return Match(usage=usage, strength=MatchStrength.DIRECT, reason=f"passes {names}")
            if "**" in usage.keywords:
                return Match(
                    usage=usage,
                    strength=MatchStrength.SYMBOL,
                    reason="forwards **kwargs, which may include " + ", ".join(sorted(params)),
                )
            return None
        if symbol_hit is None:
            return None
        return Match(
            usage=usage,
            strength=MatchStrength.SYMBOL,
            reason=f"references {usage.symbol} without a visible call",
        )
    if symbol_hit is None:
        return None
    if claim.kind in _DIRECT_KINDS:
        return Match(usage=usage, strength=MatchStrength.DIRECT, reason=f"uses {symbol_hit}")
    return Match(usage=usage, strength=MatchStrength.SYMBOL, reason=f"uses {symbol_hit}")


def _with_via(match: Match | None) -> Match | None:
    if match is None or not match.usage.via:
        return match
    return match.model_copy(update={"reason": f"{match.reason}; {match.usage.via}"})


def match_claims(
    claims: list[Claim], usages: list[Usage], import_names: list[str]
) -> list[ClaimResult]:
    results: list[ClaimResult] = []
    imports = [u for u in usages if u.kind == UsageKind.IMPORT]
    for claim in claims:
        if not claim.symbols and not claim.parameters:
            if not usages:
                status = ClaimStatus.NOT_FOUND
                matches: list[Match] = []
            else:
                status = ClaimStatus.NEEDS_REVIEW
                reason = (
                    "runtime support changed; check your supported versions"
                    if claim.kind in _RUNTIME_KINDS
                    else "the claim names no symbol; every import is a candidate"
                )
                matches = [
                    Match(usage=u, strength=MatchStrength.IMPORT, reason=reason) for u in imports
                ]
            results.append(ClaimResult(claim=claim, status=status, matches=matches))
            continue
        matches = [
            m for u in usages if (m := _with_via(_match_usage(claim, u, import_names))) is not None
        ]
        if any(m.strength == MatchStrength.DIRECT for m in matches):
            status = ClaimStatus.AFFECTED
        elif matches:
            status = ClaimStatus.POSSIBLE
        else:
            status = ClaimStatus.NOT_FOUND
        results.append(ClaimResult(claim=claim, status=status, matches=matches))
    order = {
        ClaimStatus.AFFECTED: 0,
        ClaimStatus.POSSIBLE: 1,
        ClaimStatus.NEEDS_REVIEW: 2,
        ClaimStatus.NOT_FOUND: 3,
    }
    results.sort(key=lambda r: order[r.status])
    return results
