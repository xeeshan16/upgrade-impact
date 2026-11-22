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

### Changed

- Prompt fence markers are derived from the fenced content instead of being
  random, so identical requests are identical and can be cached.
