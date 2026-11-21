# Progress

## Day 1 — 2026-11-21: first end-to-end slice

Built:

- Packaging (uv, hatchling, console script), ruff, mypy strict, pytest with an
  85% coverage gate, CI on Python 3.11–3.13 with live API tests on 3.12, and a
  clean-venv wheel install that checks a real Dependabot PR.
- Providers: Ollama / any OpenAI-compatible server, OpenAI, Azure OpenAI,
  Anthropic, with retries, backoff and structured-output fallback; a labelled
  fake for tests.
- Bump resolution from PR titles (Dependabot, Renovate), Renovate body tables
  and manifest diffs (requirements, pyproject incl. Poetry, Pipfile,
  package.json).
- Release notes in `(from, to]` from GitHub Releases, filled from CHANGELOG
  sections (markdown and rst-style headings), PR body as the last resort.
- Claim extraction with a JSON schema, chunking, one repair retry, and
  verification of quotes and symbols (ADR 0001).
- Python AST usage index and deterministic, parameter-aware matching.
- `upgrade-impact check` with text, Markdown and JSON output, `--ref`, `--no-llm`.

Found while building:

- The best demo case came from mining merged Dependabot PRs: in
  `uriyyo/fastapi-pagination#1373` (httpx 0.27.2 → 0.28.0) the maintainer pushed
  `81a3960` onto the bump PR to replace `AsyncClient(app=app, ...)`. That is the
  label shape day 3's miner will use: the bump commit is the input, the human
  follow-up commit marks the affected lines. It also showed that analysing the
  PR *head* can hide the impact (the fix is already there), hence `--ref`.
- The GitHub API and codeload are not reachable from the build sandbox, so PR
  fetching and tarball download are tested with mocked HTTP locally and live in CI.
