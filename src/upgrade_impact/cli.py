"""Command-line interface: ``upgrade-impact check <PR URL | diff file | ->``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from upgrade_impact import __version__
from upgrade_impact.bump import BumpError
from upgrade_impact.config import PROVIDERS, ConfigError, load_settings
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return _check(args)
    except (ConfigError, CheckError, BumpError) as exc:
        print(f"upgrade-impact: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except GitHubError as exc:
        print(f"upgrade-impact: GitHub: {exc}", file=sys.stderr)
        return EXIT_ERROR
