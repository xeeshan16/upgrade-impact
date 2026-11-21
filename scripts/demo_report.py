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
from upgrade_impact.models import ClaimStatus, MatchStrength, Report
from upgrade_impact.pipeline import run_check, target_from_pr
from upgrade_impact.providers.factory import build_chat
from upgrade_impact.render import render_text
from upgrade_impact.sources.github import GitHubClient, parse_pr_url, token_from_env
from upgrade_impact.sources.registry import RegistryClient


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


def _hits(report: Report, strengths: set[MatchStrength]) -> list[tuple[str, int]]:
    return sorted(
        {
            (m.usage.path, m.usage.line)
            for bump in report.bumps
            for result in bump.results
            if result.status in (ClaimStatus.AFFECTED, ClaimStatus.POSSIBLE)
            for m in result.matches
            if m.strength in strengths
        }
    )


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
        "| PR | verdict | affected lines | possible lines | expected (human fix) | found "
        "| claims (dropped) | model time | tokens in/out |",
        "|---|---|---|---|---|---|---|---|---|",
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
        direct = _hits(report, {MatchStrength.DIRECT})
        hits = _hits(report, {MatchStrength.DIRECT, MatchStrength.SYMBOL})
        possible = [h for h in hits if h not in direct]
        found = all(e in hits for e in case.expected) if case.expected else not direct
        [bump] = report.bumps
        run = bump.model
        tokens = f"{run.usage.input_tokens:,}/{run.usage.output_tokens:,}" if run else "-"
        expected = ", ".join(f"`{p}:{n}`" for p, n in case.expected) or "none"
        shown = ", ".join(f"`{p}:{n}`" for p, n in direct) or "none"
        maybe = ", ".join(f"`{p}:{n}`" for p, n in possible[:6]) or "none"
        claims = f"{len(bump.results)} ({bump.claims_dropped})" if run else "-"
        latency = f"{run.latency_s:.0f} s" if run else f"{elapsed:.0f} s"
        rows.append(
            f"| [{case.name}]({case.pr}) | {bump.verdict} | {shown} | {maybe} | {expected} | "
            f"{'yes' if found else 'no'} | {claims} | {latency} | {tokens} |"
        )
        summary.append(
            {
                "case": case.name,
                "pr": case.pr,
                "ref": case.ref,
                "verdict": bump.verdict,
                "affected_lines": direct,
                "possible_lines": possible,
                "expected": case.expected,
                "found": found,
                "label": case.label,
            }
        )
        print(text)
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
