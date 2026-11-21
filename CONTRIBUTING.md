# Contributing

Thanks for helping. The most useful thing you can send is a real bump PR where
upgrade-impact was wrong: the PR URL, the commit to analyse (`--ref`), and the
lines you expected it to flag (or not flag).

## Development setup

```bash
git clone https://github.com/xeeshan16/upgrade-impact && cd upgrade-impact
uv sync                      # Python 3.11+; installs dev tools too
uv run pytest                # unit and integration tests, coverage gate 85%
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run upgrade-impact check --package httpx --from 0.27.0 --to 0.28.1 \
  --repo tests/fixtures/repos/sample --provider fake
```

Tests never need a model or network access. The fake provider
(`--provider fake`) is a deterministic keyword heuristic for tests only; its
output is labelled `[fake provider]` everywhere. To try a real model locally,
install [Ollama](https://ollama.com), run `ollama pull qwen2.5:7b`, and drop
`--provider fake`.

Live tests against PyPI and raw.githubusercontent.com:
`UPGRADE_IMPACT_LIVE=1 uv run pytest -m live`. Add
`UPGRADE_IMPACT_LIVE_GITHUB=1 GITHUB_TOKEN=...` to include the GitHub API.

## Pull requests

- One change per PR, with tests. CI must be green.
- Describe the user-visible effect. If it changes what gets flagged, run
  `scripts/demo_report.py` before and after (and the eval once it exists) and
  paste both tables.
- New providers implement `ChatModel` in `src/upgrade_impact/providers/` and
  get a branch in `factory.py` plus mocked-HTTP tests.
- New import-name mappings (distribution name differs from import name) go in
  `KNOWN_IMPORT_NAMES` in `src/upgrade_impact/index/python.py`.
