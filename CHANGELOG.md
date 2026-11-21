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
