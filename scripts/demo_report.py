"""Run upgrade-impact on real Dependabot PRs and write a Markdown report.

Each case is a public PR. ``expected`` lists the lines a maintainer actually had
to change to make the bump work (taken from the fix commit linked in ``label``),
or is empty when the PR merged with manifest changes only. This is a smoke test
on a handful of PRs, not the evaluation (that is ``upgrade-impact eval``).

    uv run python scripts/demo_report.py --model qwen2.5:7b --out reports/demo
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from upgrade_impact.config import load_settings
from upgrade_impact.models import ClaimStatus, Match, MatchStrength, Report, Verdict
from upgrade_impact.pipeline import run_check, target_from_pr
from upgrade_impact.providers.factory import build_chat
from upgrade_impact.render import render_text
from upgrade_impact.sources.github import GitHubClient, parse_pr_url, token_from_env
from upgrade_impact.sources.registry import RegistryClient

Place = tuple[str, int]


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    pr: str
    ref: str | None
    expected: list[tuple[str, int]] = field(default_factory=list)
    label: str = ""


CASES = [
    Case(
        name="fastapi-pagination httpx 0.27.2 -> 0.28.0 (bump commit)",
        pr="https://github.com/uriyyo/fastapi-pagination/pull/1373",
        ref="e14c865aebed311824f192cefe16e9e64a9ade19",
        expected=[("tests/conftest.py", 271)],
        label="fixed in 81a3960 'Remove usage of deprecated app param' on the same PR",
    ),
    Case(
        name="govbr_auth httpx 0.27 -> 0.28.1",
        pr="https://github.com/cereja-project/govbr_auth/pull/53",
        ref=None,
        expected=[],
        label="merged with manifest changes only",
    ),
]


def _confirmed(match: Match) -> bool:
    if match.adjudication is not None:
        return match.adjudication.effective == Verdict.AFFECTED
    return match.strength == MatchStrength.DIRECT


def _lines(report: Report) -> tuple[list[Place], list[Place], list[Place]]:
    """Affected (direct or model-confirmed), still-possible and model-cleared lines."""
    affected: set[Place] = set()
    possible: set[Place] = set()
    cleared: set[Place] = set()
    for bump in report.bumps:
        for result in bump.results:
            cleared.update((m.usage.path, m.usage.line) for m in result.cleared)
            if result.status not in (ClaimStatus.AFFECTED, ClaimStatus.POSSIBLE):
                continue
            for m in result.matches:
                if m.strength == MatchStrength.IMPORT and m.adjudication is None:
                    continue
                place = (m.usage.path, m.usage.line)
                (affected if _confirmed(m) else possible).add(place)
    possible -= affected
    cleared -= affected | possible
    return sorted(affected), sorted(possible), sorted(cleared)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    settings = load_settings(overrides={"provider": args.provider, "model": args.model})
    chat = build_chat(settings)
    github = GitHubClient(token=token_from_env(os.environ))
    registry = RegistryClient()
    rows = [
        f"## upgrade-impact on real Dependabot PRs ({chat.provider} `{chat.model}`)",
        "",
        "| PR | verdict | affected lines | possible lines | cleared by review | expected "
        "(human fix) | found | claims (dropped) | model time | tokens in/out |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    summary = []
    for index, case in enumerate(CASES, start=1):
        ref = parse_pr_url(case.pr)
        assert ref is not None
        target = target_from_pr(ref, github, None)
        if case.ref:
            target.head_sha = case.ref
        started = time.perf_counter()
        report = run_check(target, chat=chat, registry=registry, github=github, settings=settings)
        elapsed = time.perf_counter() - started
        text = render_text(report)
        (args.out / f"case-{index}.txt").write_text(text, encoding="utf-8")
        (args.out / f"case-{index}.json").write_text(report.model_dump_json(indent=2))
        direct, possible, cleared = _lines(report)
        hits = direct + possible
        found = all(e in hits for e in case.expected) if case.expected else not direct
        [bump] = report.bumps
        run, review = bump.model, bump.adjudication
        tokens_in = sum(r.usage.input_tokens for r in (run, review) if r)
        tokens_out = sum(r.usage.output_tokens for r in (run, review) if r)
        tokens = f"{tokens_in:,}/{tokens_out:,}" if run else "-"
        expected = ", ".join(f"`{p}:{n}`" for p, n in case.expected) or "none"
        shown = ", ".join(f"`{p}:{n}`" for p, n in direct) or "none"
        maybe = ", ".join(f"`{p}:{n}`" for p, n in possible[:6]) or "none"
        gone = ", ".join(f"`{p}:{n}`" for p, n in cleared[:6]) or "none"
        claims = f"{len(bump.results)} ({bump.claims_dropped})" if run else "-"
        model_s = sum(r.latency_s for r in (run, review) if r)
        latency = f"{model_s:.0f} s" if run else f"{elapsed:.0f} s"
        rows.append(
            f"| [{case.name}]({case.pr}) | {bump.verdict} | {shown} | {maybe} | {gone} | "
            f"{expected} | {'yes' if found else 'no'} | {claims} | {latency} | {tokens} |"
        )
        summary.append(
            {
                "case": case.name,
                "pr": case.pr,
                "ref": case.ref,
                "verdict": bump.verdict,
                "affected_lines": direct,
                "possible_lines": possible,
                "cleared_lines": cleared,
                "expected": case.expected,
                "found": found,
                "label": case.label,
            }
        )
        print(text)
        for claim in bump.dropped_claims:
            cites = "; ".join(f"N{ev.line} {ev.quote!r}" for ev in claim.evidence) or "no evidence"
            print(f"dropped: {claim.kind.value}: {claim.summary[:120]} <- {cites[:300]}")
    rows.append("")
    for index, case in enumerate(CASES, start=1):
        rows.append(f"<details><summary>{case.name}: full output</summary>\n")
        rows.append("```text")
        rows.append((args.out / f"case-{index}.txt").read_text().rstrip())
        rows.append("```\n</details>\n")
    (args.out / "report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
