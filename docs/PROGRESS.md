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
- The first CI smoke run on a second real PR (`cereja-project/govbr_auth#53`)
  reported `httpx.ASGITransport(app=...)` as *affected* by "the deprecated `app`
  argument has now been removed". That call is the replacement, not the
  problem. The notes name the parameter but not the API, and parameter names
  repeat across APIs, so a parameter-only claim now gives "possible" matches
  with that reason; only a claim with a verified symbol can be "affected".
  Telling `AsyncClient(app=)` from `ASGITransport(app=)` is the day 2
  adjudicator's job.
- The first real-model run (qwen2.5:3b on Ollama, CPU runner,
  [run 44907695785](https://github.com/xeeshan16/upgrade-impact/actions/runs/44907695785))
  returned `{"claims": []}` in 6 output tokens for both PRs, and the report said
  "NO IMPACT FOUND". Under constrained decoding an empty list is the shortest
  valid answer. Two changes: the schema now asks for `breaking_lines` first
  (the model points at lines before writing claims) and a reply that flags
  lines but claims nothing gets one follow-up asking for those claims; and a
  deterministic safety net lists note lines that read like breaking changes
  ("removed", "renamed", "no longer", ...) but that no verified claim cites,
  which turns the verdict into "review". A model that returns nothing can no
  longer produce a confident "no impact".
- qwen2.5:7b on the same run did find the four httpx 0.28 changes, but put
  `app`, `proxies`, `verify` and `cert` in `symbols` rather than `parameters`,
  so nothing matched and fastapi-pagination came out "no impact". A bare
  lower-case name that the cited line calls an argument, parameter or option
  (or writes as `name=`) is now treated as a parameter. On govbr_auth's 28
  lines the 7B model looped until the 2,048-token limit (42 claims dropped,
  535 s); a cut-off, unparseable reply is now retried as two halves of the
  notes instead of being "repaired".
- With `breaking_lines` first, qwen2.5:3b now proposes 12 claims per PR
  ([run 44908293787](https://github.com/xeeshan16/upgrade-impact/actions/runs/44908293787)),
  but every one was dropped by citation checking, and the safety net listed the
  `proxies` and `app` lines for review instead of reporting "no impact". The
  artifacts can't be read from the build sandbox, so dropped claims are now
  kept in the JSON report and printed by the demo script. Two likely causes
  are handled: quotes that start with the `N12:` line prefix or a bullet, and
  citations one or two lines off; a quote found within two lines is re-pointed
  at the line that actually holds it, so the citation stays exact.
- Printing the dropped claims showed why
  ([run 44909317658](https://github.com/xeeshan16/upgrade-impact/actions/runs/44909317658)):
  qwen2.5:7b returned the four correct httpx 0.28 claims, with summaries close
  to verbatim, but every `evidence` list was empty, while `breaking_lines`
  pointed at the right lines. The schema now requires at least one evidence
  item, and a claim that still arrives without evidence is cited to a flagged
  line that names its symbol or parameter (or contains its summary verbatim),
  marked as inferred in the report. A claim that names nothing a flagged line
  mentions is still dropped. A cited line such as "the deprecated `app`
  argument has now been removed" also supplies the parameter when the model
  left both lists empty.
