# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added

- `upgrade-impact check` for a GitHub PR URL, a manifest diff (file or stdin),
  or `--package/--from/--to`; `--ref` analyses the PR's code at a given commit.
- Bump resolution from Dependabot and Renovate titles, Renovate body tables and
  manifest diffs (`requirements*.txt`, `pyproject.toml` incl. Poetry, `Pipfile`,
  `package.json`), for PyPI and npm.
- Release notes between two versions from GitHub Releases, filled in from the
  CHANGELOG for versions without a useful release body, with the PR body as a
  last resort. Monorepo tags (`pkg@1.2.3`) are filtered by package.
- Breaking-change extraction with a JSON schema, chunking of long notes, one
  repair retry, and verification of every quoted citation and named symbol.
- Python usage index (imports with aliases, calls with keyword names,
  attributes, names) and deterministic matching with direct / symbol / import
  strengths.
- Providers: Ollama and any OpenAI-compatible server, OpenAI, Azure OpenAI,
  Anthropic, and a labelled fake for tests.
- Text, Markdown and JSON output.
- Model review of uncertain matches: each "possible" match (and, for claims
  that name no symbol, each call into the package) is read by the model in a
  numbered code window and judged affected / not affected / unclear, citing a
  code line and a note line that are both verified. Unverified verdicts count
  as unclear, and for a claim that names no symbol an "affected" verdict must
  name, from the notes, the API or option the line uses. Cleared lines stay in
  the report. `--no-review` turns it off; `UPGRADE_IMPACT_MAX_CANDIDATES` and
  `UPGRADE_IMPACT_MAX_REVIEW_CALLS` cap it per claim and per bump (ADR 0002).
- JavaScript and TypeScript usage index with tree-sitter for npm bumps: ESM,
  type and namespace imports, `require` with destructuring, dynamic `import()`,
  re-exports and local barrel files, calls and `new` with the object-literal
  keys they pass, JSX elements with their props, type references, and method
  calls on instances made with `new`.
- Python: method calls on instances whose class is visible in the same file
  (`c = httpx.Client()`, `with ... as c`, `self.c = ...`, annotated parameters
  and return types) and names re-exported by local modules are resolved; each
  such usage says how it was resolved.
- On-disk cache of model replies keyed by the full request (`--no-cache`,
  `UPGRADE_IMPACT_LLM_CACHE=off`).
- `upgrade-impact mine`: builds a labelled dataset from the git history of
  public repositories (partial clones, no API token). A merged bump PR where a
  human pushed code is an impact case labelled with the changed lines; one
  that merged with manifest changes only is a no-impact case. Majors and 0.x
  minors of packages the code imports; generated-looking fixes dropped; split
  by repository (ADR 0003).
- `data/cases.jsonl`: 259 real bumps from 51 repositories (61 impact, 198
  no-impact), dev and held-out test splits, with the list of 154 repositories
  scanned.
- `upgrade-impact eval`: runs upgrade-impact, import-grep, symbol-grep and a
  naive single prompt on the same cached notes and code; reports file and line
  precision/recall, detection and false-alarm rates (strict and lenient) with
  repository-level bootstrap intervals, token and call counts, and per-case
  predictions; `--gate` fails on thresholds; `--per-label` takes a fixed sample.
- CI runs the eval on the dev split with the fake provider behind a gate; the
  real-model workflow can run it on a dev sample with Ollama.
- Runtime-support changes ("Drop support for Node.js 18", "requires Python
  3.10 or later") are compared with the floor the project declares
  (`engines.node`, `requires-python`, Poetry's `python`, `python_requires`)
  and cite the manifest line: met, the claim is cleared; not met or not
  declared, it stays "needs review" with the reason (ADR 0004).
- Second reading of unclaimed note lines: breaking-looking lines no claim
  cites go back to the model in a short prompt, one verified answer per line.
  A "yes" becomes a claim; a "no" dismisses the line unless it names
  something your code uses from the package. Dismissed lines are listed with
  the reason. `UPGRADE_IMPACT_RECHECK=off` turns it off (ADR 0004).
- Release-note lines that read like instructions to a model (prompt
  injection) are reported, are never dismissed, and keep the answer from
  "no impact".
- Reports say why a bump is "review" (possible matches, claims naming no API,
  unclaimed lines, injection-like lines).
- The eval counts which causes held cases at "review" and the note lines
  ruled out, lists injection-flagged lines, and for runs of up to 40 cases
  lists every case and its claims in `report.md`.

### Changed

- Section headings such as `### ⚠ BREAKING CHANGES` no longer count as
  unclaimed breaking changes.
- A changed default (or a behaviour change whose summary is about a default)
  now matches the calls that do *not* pass the parameter; calls that set it
  are unaffected. Before, it matched only calls passing it, the rule for a
  removed argument (found by the eval on mdn/yari's dotenv 17 bump).
- Changed-default phrasings ("Default `quiet` to false", "now defaults to")
  mark a note line as breaking-looking.
- Eval snapshots and downloaded PR code now include the root manifests
  (`package.json`, `pyproject.toml`, `setup.cfg`, `setup.py`); eval snapshots
  moved to `snapshots-v2` in the cache.
- Prompt fence markers are derived from the fenced content instead of being
  random, so identical requests are identical and can be cached.
