"""Command-line interface: ``upgrade-impact check <PR URL | diff file | ->``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from upgrade_impact import __version__
from upgrade_impact.bump import BumpError
from upgrade_impact.config import PROVIDERS, ConfigError, load_settings
from upgrade_impact.eval.dataset import (
    Case,
    Label,
    dump_cases,
    load_cases,
    sample_per_label,
    select,
)
from upgrade_impact.eval.mine import MineOptions, mine_repositories
from upgrade_impact.eval.report import render_summary
from upgrade_impact.eval.runner import check_gate, load_gate, run_eval
from upgrade_impact.eval.systems import ImportGrep, NaivePrompt, SymbolGrep, System, UpgradeImpact
from upgrade_impact.models import Bump, Ecosystem
from upgrade_impact.pipeline import (
    CheckError,
    Target,
    default_cache_dir,
    run_check,
    target_from_diff,
    target_from_pr,
)
from upgrade_impact.providers.cache import CachingChat
from upgrade_impact.providers.factory import build_chat
from upgrade_impact.render import render_markdown, render_text
from upgrade_impact.sources.github import GitHubClient, GitHubError, parse_pr_url, token_from_env
from upgrade_impact.sources.registry import RegistryClient

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
SYSTEM_NAMES = ("upgrade-impact", "import-grep", "symbol-grep", "naive-prompt")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upgrade-impact",
        description="Tell whether a dependency bump touches your code, where, and why.",
    )
    parser.add_argument("--version", action="version", version=f"upgrade-impact {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser(
        "check",
        help="check a Dependabot/Renovate PR, a manifest diff, or an explicit bump",
        description=(
            "SOURCE is a GitHub pull request URL, a unified diff file of your manifests, "
            "or '-' to read a diff from stdin. Or pass --package, --from and --to."
        ),
    )
    check.add_argument("source", nargs="?", help="PR URL, diff file, or '-'")
    check.add_argument("--package", help="the dependency, when not reading a PR or diff")
    check.add_argument("--from", dest="from_version", help="the current version")
    check.add_argument("--to", dest="to_version", help="the target version")
    check.add_argument(
        "--ecosystem", choices=[e.value for e in Ecosystem], help="pypi or npm (default: inferred)"
    )
    check.add_argument(
        "--repo",
        type=Path,
        help="code to analyse (default: current directory; for a PR URL, the PR's own code)",
    )
    check.add_argument(
        "--ref",
        help="for a PR URL: analyse the code at this commit instead of the PR head "
        "(for example the bump commit, before any follow-up fixes)",
    )
    check.add_argument(
        "--import-name",
        action="append",
        dest="import_names",
        help="import name(s) of the package, if not the obvious one (repeatable)",
    )
    check.add_argument("--provider", choices=PROVIDERS, help="model provider (default: auto)")
    check.add_argument("--model", help="model name")
    check.add_argument("--base-url", help="OpenAI-compatible base URL")
    check.add_argument(
        "--no-llm",
        action="store_true",
        help="skip claim extraction: show notes coverage and import sites only",
    )
    check.add_argument(
        "--no-review",
        action="store_true",
        help="skip the step where the model reads each uncertain match in its code context",
    )
    check.add_argument(
        "--no-cache", action="store_true", help="do not read or write the model reply cache"
    )
    check.add_argument(
        "--format", choices=("text", "markdown", "json"), default="text", help="output format"
    )
    check.add_argument("-o", "--output", type=Path, help="write the report here instead of stdout")
    check.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")

    mine = sub.add_parser(
        "mine",
        help="build a labelled dataset from merged Dependabot/Renovate PRs (git only)",
        description=(
            "Scan the history of public GitHub repositories for merged bump PRs. A PR where "
            "a human pushed code changes is an impact case (the changed lines are the "
            "labels); a PR that merged with manifest changes only is a no-impact case."
        ),
    )
    mine.add_argument("repos", nargs="*", help="owner/repo slugs")
    mine.add_argument("--repos-file", type=Path, help="file with one owner/repo per line")
    mine.add_argument("-o", "--output", type=Path, required=True, help="JSONL file to write")
    mine.add_argument("--since", default="3 years ago", help="history window (git --since)")
    mine.add_argument("--max-commits", type=int, default=5000, help="first-parent commits per repo")
    mine.add_argument(
        "--all-bumps", action="store_true", help="keep minor and patch bumps (default: majors only)"
    )
    mine.add_argument("--max-no-impact", type=int, default=6, help="no-impact cases per repo")
    mine.add_argument("--max-impact", type=int, default=25, help="impact cases per repo")
    mine.add_argument("--jobs", type=int, default=4, help="repositories mined in parallel")
    mine.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")

    evaluate = sub.add_parser(
        "eval",
        help="score upgrade-impact against baselines on a labelled dataset",
        description=(
            "Runs upgrade-impact and the import-grep, symbol-grep and naive-prompt baselines "
            "on every case and reports file and line precision/recall, detection and "
            "false-alarm rates with repository-level bootstrap intervals."
        ),
    )
    evaluate.add_argument("dataset", type=Path, help="JSONL dataset (from 'upgrade-impact mine')")
    evaluate.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    evaluate.add_argument(
        "--systems",
        default=",".join(SYSTEM_NAMES),
        help=f"comma-separated subset of: {', '.join(SYSTEM_NAMES)}",
    )
    evaluate.add_argument("--limit", type=int, help="only the first N cases (after --label)")
    evaluate.add_argument(
        "--per-label",
        type=int,
        help="a fixed sample: N impact and N no-impact cases, picked by a hash of the case id",
    )
    evaluate.add_argument("--label", choices=("impact", "no_impact"), help="only these cases")
    evaluate.add_argument("--window", type=int, default=2, help="line tolerance around a hunk")
    evaluate.add_argument("--bootstrap", type=int, default=1000, help="bootstrap samples")
    evaluate.add_argument("--out", type=Path, default=Path("reports/eval"), help="output directory")
    evaluate.add_argument("--gate", type=Path, help="JSON thresholds; exit 1 if any fails")
    evaluate.add_argument("--provider", choices=PROVIDERS, help="model provider (default: auto)")
    evaluate.add_argument("--model", help="model name")
    evaluate.add_argument("--base-url", help="OpenAI-compatible base URL")
    evaluate.add_argument(
        "--no-llm", action="store_true", help="run only the systems that need no model"
    )
    evaluate.add_argument(
        "--no-cache", action="store_true", help="do not read or write the model reply cache"
    )
    evaluate.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    return parser


def _target(args: argparse.Namespace, github: GitHubClient) -> Target:
    ecosystem = Ecosystem(args.ecosystem) if args.ecosystem else None
    if args.package:
        if not args.to_version:
            raise CheckError("--package needs --to (and usually --from)")
        bump = Bump(
            ecosystem=ecosystem or Ecosystem.PYPI,
            package=args.package,
            from_version=args.from_version,
            to_version=args.to_version,
            source="flags",
        )
        return Target(source=f"{bump.label}", bumps=[bump])
    if not args.source:
        raise CheckError("give a PR URL, a diff file or '-', or pass --package/--from/--to")
    ref = parse_pr_url(args.source)
    if ref is not None:
        target = target_from_pr(ref, github, ecosystem)
        if args.ref:
            target.head_sha = args.ref
        return target
    if args.ref:
        raise CheckError("--ref only applies to a pull request URL")
    if args.source.startswith(("http://", "https://")):
        raise CheckError(f"not a GitHub pull request URL: {args.source}")
    if args.source == "-":
        return target_from_diff(sys.stdin.read(), "stdin", ecosystem)
    path = Path(args.source)
    if not path.is_file():
        raise CheckError(f"{path} does not exist")
    return target_from_diff(
        path.read_text(encoding="utf-8", errors="replace"), str(path), ecosystem
    )


def _check(args: argparse.Namespace) -> int:
    settings = load_settings(
        overrides={"provider": args.provider, "model": args.model, "base_url": args.base_url}
    )
    github = GitHubClient(
        api_url=settings.github_api_url,
        raw_url=settings.github_raw_url,
        token=token_from_env(os.environ),
    )
    registry = RegistryClient(pypi_url=settings.pypi_url, npm_url=settings.npm_registry_url)
    try:
        target = _target(args, github)
        chat = None if args.no_llm else build_chat(settings)
        if chat is not None and settings.llm_cache == "on" and not args.no_cache:
            chat = CachingChat(chat, default_cache_dir(settings, os.environ) / "llm")
        report = run_check(
            target,
            chat=chat,
            registry=registry,
            github=github,
            settings=settings,
            repo=args.repo,
            import_names=args.import_names,
            review=not args.no_review,
        )
    finally:
        github.close()
        registry.close()
    if args.format == "json":
        text = report.model_dump_json(indent=2) + "\n"
    elif args.format == "markdown":
        text = render_markdown(report)
    else:
        text = render_text(report)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return EXIT_OK


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _mine(args: argparse.Namespace) -> int:
    repos = list(args.repos)
    if args.repos_file:
        for line in args.repos_file.read_text(encoding="utf-8").splitlines():
            slug = line.split("#", 1)[0].strip()
            if slug:
                repos.append(slug)
    if not repos:
        raise CheckError("give owner/repo slugs or --repos-file")
    settings = load_settings()
    options = MineOptions(
        since=args.since or None,
        max_commits=args.max_commits,
        all_bumps=args.all_bumps,
        max_no_impact_per_repo=args.max_no_impact,
        max_impact_per_repo=args.max_impact,
    )
    cases, _ = mine_repositories(
        list(dict.fromkeys(repos)),
        default_cache_dir(settings, os.environ),
        options,
        git_base_url=os.environ.get("UPGRADE_IMPACT_GIT_URL", "https://github.com"),
        jobs=args.jobs,
        progress=_progress,
    )
    count = dump_cases(cases, args.output)
    impact = sum(c.label == Label.IMPACT for c in cases)
    _progress(f"wrote {count} cases ({impact} impact, {count - impact} no-impact) to {args.output}")
    return EXIT_OK


def _eval_cases(args: argparse.Namespace) -> list[Case]:
    if not args.dataset.is_file():
        raise CheckError(f"{args.dataset} does not exist")
    try:
        cases = select(load_cases(args.dataset), args.split)
    except (ValueError, OSError) as exc:
        raise CheckError(str(exc)) from exc
    if args.label:
        cases = [c for c in cases if c.label.value == args.label]
    if args.per_label is not None:
        cases = sample_per_label(cases, args.per_label)
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise CheckError(f"no cases in {args.dataset} for split {args.split!r}")
    return cases


def _eval(args: argparse.Namespace) -> int:
    wanted = [s.strip() for s in args.systems.split(",") if s.strip()]
    unknown = sorted(set(wanted) - set(SYSTEM_NAMES))
    if unknown:
        raise CheckError(f"unknown system(s): {', '.join(unknown)}")
    cases = _eval_cases(args)
    try:
        gate = load_gate(args.gate) if args.gate else None
    except (ValueError, OSError) as exc:
        raise CheckError(str(exc)) from exc
    settings = load_settings(
        overrides={"provider": args.provider, "model": args.model, "base_url": args.base_url}
    )
    cache_dir = default_cache_dir(settings, os.environ)
    github = GitHubClient(
        api_url=settings.github_api_url,
        raw_url=settings.github_raw_url,
        token=token_from_env(os.environ),
    )
    registry = RegistryClient(pypi_url=settings.pypi_url, npm_url=settings.npm_registry_url)
    chat = None if args.no_llm else build_chat(settings)
    if chat is not None and settings.llm_cache == "on" and not args.no_cache:
        chat = CachingChat(chat, cache_dir / "llm")
    factories: dict[str, System | None] = {
        "upgrade-impact": UpgradeImpact(chat, settings, registry, github) if chat else None,
        "import-grep": ImportGrep(),
        "symbol-grep": SymbolGrep(),
        "naive-prompt": NaivePrompt(chat, max_output_tokens=settings.max_output_tokens)
        if chat
        else None,
    }
    systems = [s for name in wanted if (s := factories[name]) is not None]
    try:
        result = run_eval(
            cases,
            systems,
            cache_dir=cache_dir,
            registry=registry,
            github=github,
            window=args.window,
            git_base_url=os.environ.get("UPGRADE_IMPACT_GIT_URL", "https://github.com"),
            progress=_progress,
        )
    finally:
        github.close()
        registry.close()
    result.meta.update(
        {
            "dataset": args.dataset.as_posix(),
            "split": args.split,
            "provider": chat.provider if chat else "none",
            "model": chat.model if chat else "none",
            "settings": {
                "notes_budget_tokens": settings.notes_budget_tokens,
                "max_candidates": settings.max_candidates,
                "max_review_calls": settings.max_review_calls,
                "max_output_tokens": settings.max_output_tokens,
                "recheck": settings.recheck,
            },
        }
    )
    summary = result.summary(samples=args.bootstrap)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    with (args.out / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for row in result.case_rows():
            handle.write(json.dumps(row) + "\n")
    markdown = render_summary(summary, result.case_rows())
    (args.out / "report.md").write_text(markdown, encoding="utf-8")
    sys.stdout.write(markdown)
    if gate is not None:
        failures = check_gate(summary, gate)
        for failure in failures:
            print(f"gate: {failure}", file=sys.stderr)
        if failures:
            return EXIT_ERROR
        _progress(f"gate: {len(gate['checks'])} checks passed")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    commands = {"check": _check, "mine": _mine, "eval": _eval}
    try:
        return commands[args.command](args)
    except (ConfigError, CheckError, BumpError) as exc:
        print(f"upgrade-impact: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except GitHubError as exc:
        print(f"upgrade-impact: GitHub: {exc}", file=sys.stderr)
        return EXIT_ERROR
