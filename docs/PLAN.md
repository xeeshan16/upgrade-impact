# Plan: upgrade-impact (7 days)

Goal: a CLI, GitHub Action and MCP server that reads a Dependabot or Renovate
bump, extracts the breaking changes from the release notes as cited claims,
finds the code they reach, and answers "affected / possible / no impact" with
every claim cited to a release-note line and a code line, both verified.
Measured on real bump PRs against grep and naive-prompt baselines.

One milestone per day. The plan was posted with the proposal on 2026-11-15
and started on 2026-11-21.

## Day 1: first end-to-end slice
Set up uv, ruff, mypy strict, pytest and CI. Add the provider interface:
OpenAI-compatible (Ollama/vLLM), OpenAI, Azure and Anthropic, plus a
deterministic fake for tests only. Resolve a bump from a PR URL or a manifest
diff (PyPI + npm). Fetch the release notes between the two versions from
GitHub Releases or CHANGELOG. Extract breaking changes as schema-validated
claims, each cited to a verified release-note line. Build a Python import and
usage index. `upgrade-impact check` runs on a real public bump PR with a real
model. README skeleton, PLAN, PROGRESS, ADR 0001, LICENSE, CONTRIBUTING,
CHANGELOG and templates.

## Day 2: usage analysis core
Tree-sitter indexing for Python and JS/TS, covering aliases and re-exports,
attribute, kwarg and option usage, and config files. Candidate matching uses
symbols plus optional embeddings. The model then adjudicates each candidate
with its code context. Both sides of every citation are verified. Results are
cached on disk.

*Adjusted on day 2 (see PROGRESS):* Python stays on the stdlib `ast` (it is
exact for Python and adds no dependency); tree-sitter is used for JS/TS.
Embedding-based candidate ranking and config-file indexing move to day 4,
where they can be measured on the dev split instead of added blind; day 2
ranks no-symbol candidates lexically.

## Day 3: dataset and eval harness
A miner that runs in CI and builds a labelled set from real Dependabot/Renovate
PRs: follow-up human code commits mark impact, clean merges with green CI mark
no impact. Splits are by repo. Baselines are import-grep, symbol-grep from the
notes, and a naive single prompt. Metrics are file and line precision/recall
and the false-alarm rate, with bootstrap CIs. Adds `upgrade-impact eval` and a
CI gate.

*Adjusted on day 3 (see PROGRESS):* the miner reads git history (partial
clones) instead of the GitHub API, so it needs no token and has no rate limit;
"green CI" is therefore not checked, and a clean bump is excluded instead when
a later human commit names the package or reverts it (ADR 0003). Real-model
eval runs are a manual `real-model` workflow input from day 3 on, so day 4's
changes can be measured on the dev split with a real model.

## Day 4: quality and safety
Treat release notes as untrusted input: fence them, check for injection, and
validate output. Chunk long changelogs and slice them by version range. Follow
migration-guide links. Calibrate "no impact" and confidence. Optional
embedding ranking of review candidates and config-file usages (moved from
day 2). Deprecation history as review context: for "the deprecated X has now
been removed", the earlier deprecation notice (outside the bump's range)
usually names the API and its replacement. Everything measured on the dev split.

*Adjusted on day 4 (see PROGRESS):* the day 3 real-model sample showed the
bottleneck was calibration, not ranking: every clean bump came back "review".
Day 4 went there: the runtime floor check, a guarded second reading of
unclaimed lines, headings skipped, changed defaults matched to the calls that
rely on them, injection tripwires, and per-case "why review" in the eval.
Slicing changelogs by version range already existed (day 1). Following
migration-guide links and the deprecation-history context move to day 6 if
the real-model runs there show misses they would fix; embedding ranking and
config-file usages move to the roadmap, since no measured case needed them.

## Day 5: where developers work
A GitHub Action for bump PRs that writes the job summary and annotations and
outputs JSON. Grouped PRs with many packages. An MCP server (`assess_bump`,
`find_usages`, `get_release_notes`) with configs for Claude Code, Cursor and
VS Code.

## Day 6: production concerns, and the eval harness finished
OpenTelemetry traces and metrics, token and cost accounting, concurrent
package analysis, timeouts and retries, a Docker image smoke-tested in CI, a
threat-model ADR, and real-model eval runs (Ollama in CI).

## Day 7: finish
Measured results on the held-out split against the baselines (dataset,
provider, model and settings stated), README polish, a Mermaid architecture
diagram, ROADMAP, a clean-install check and the `v0.1.0` tag.
