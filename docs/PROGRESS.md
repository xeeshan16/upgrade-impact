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
- With those fixes, both models produce cited claims
  ([run 44910332828](https://github.com/xeeshan16/upgrade-impact/actions/runs/44910332828),
  commit `ee6fc7a`). qwen2.5:7b: four clean claims for httpx 0.28.0 (94.6 s,
  880 tokens in, 319 out), with `tests/conftest.py:271` as the one "possible"
  hit and `proxies`, `verify`, `cert` "not used". qwen2.5:3b also finds
  conftest.py:271 but adds a catch-all claim citing seven lines. On
  govbr_auth#53 both report the `app` claim as "possible" for its
  `ASGITransport(app=...)` calls: false alarms that day 2's adjudicator must clear.
  The README sample now comes from the 7B run.

Next (day 2): tree-sitter indexing for Python and JS/TS, attribute-name
candidates for method calls, and a model adjudication step that reads each
"possible" or "needs review" match with its code context and cites both sides.

## Day 2 — 2026-11-22: usage analysis core

Built:

- Python index: method calls on instances whose class is visible in the same
  file (`c = httpx.Client()`, `with ... as c`, `self.c = ...`, annotated
  parameters, local functions with a return annotation) resolve to
  `httpx.Client.get`, and names re-exported by local modules (compat shims,
  `__init__` barrels, `src/` layouts) are followed one hop. Every usage found
  this way records how (`via`), and the report shows it.
- JavaScript/TypeScript index with tree-sitter for npm bumps (ESM, type and
  namespace imports, `require` with destructuring, dynamic `import()`,
  re-exports and barrel files, calls and `new` with object-literal keys as
  options (nested objects included), JSX props, type references, methods on
  `new` instances). On `alan2207/bulletproof-react` (447 files) it indexes 243
  `@tanstack/react-query` usages in 58 files in well under a second, no parse
  errors.
- Review step (ADR 0002): uncertain matches go back to the model with a
  numbered code window and the note lines around the claim; each verdict
  cites a code line and a note line, both verified. Unverified verdicts are
  "unclear"; cleared lines stay in the report. Capped at 12 candidates per
  claim and 8 uncached calls per bump.
- On-disk reply cache keyed by the full request; fence markers are now derived
  from the fenced content so identical requests stay identical. A second run
  of the same check makes 0 paid calls (tested end to end with the fake).

Found while building (real runs, `real-model` workflow, Ollama on CPU):

- First run of the review
  ([run 45025347211](https://github.com/xeeshan16/upgrade-impact/actions/runs/45025347211),
  `cc01fcb`), qwen2.5:7b: on fastapi-pagination it called
  `tests/conftest.py:271` (the line the maintainer fixed) affected, as hoped.
  On govbr_auth it called six `httpx.ASGITransport(app=...)` lines affected
  and two unclear: it made the false alarms *worse*, promoting them from
  "possible" to "affected". The notes ("the deprecated `app` argument has now
  been removed") never name the API, and the model filled the gap with a
  guess ("which is the API mentioned in the notes").
- So for a claim that names no symbol, an "affected" verdict must now name,
  from the shown notes, what the line uses (`notes_api`), and that name must
  be what the line calls. With that rule
  ([run 45027489304](https://github.com/xeeshan16/upgrade-impact/actions/runs/45027489304),
  `5501a54`): qwen2.5:7b again said "affected" for six ASGITransport lines,
  naming `httpx.ASGITransport` as "the API mentioned in the notes"; none is
  accepted, and govbr_auth is back to "possible" with no line promoted. The
  cost is that conftest.py:271 is "possible" again too; with these notes,
  that is the honest answer. Clearing the ASGITransport lines needs the 0.27.0
  deprecation notice ("use the explicit style of
  `transport=httpx.ASGITransport()`"), which lies before the bump's range:
  added to day 4 (deprecation history as review context).
- qwen2.5:3b on the same run ran its 8-call budget on govbr_auth (14 claims,
  810 s of review) and promoted three vague claims ("the 0.28 release includes
  a limited set of deprecations") to "affected"; it also produced 12 verdicts
  whose quotes were not on the cited lines, all correctly counted as unclear.
  The named-API rule now applies to every claim without a symbol, and the
  package name does not count. On the next run
  ([run 45029569598](https://github.com/xeeshan16/upgrade-impact/actions/runs/45029569598),
  `321c769`) it rejected 11 unsupported "affected" verdicts from 3b, and the
  vague claims stayed "needs review". One false positive got through: for
  "JSON request bodies use a compact representation" 3b called two
  `response.json()` lines affected, and "JSON" matched the method name
  case-insensitively. The request/response distinction is beyond a name rule;
  it goes on the day 3 eval's list of measured failure modes. qwen2.5:7b on
  the same run: both PRs "possible", no line promoted, no false "affected".
- The first 3b run had no call budget and was still reviewing govbr_auth after
  40 minutes when it was cancelled; hence the per-bump cap.
- Review is slow on a CPU runner: 2 calls for 12 candidates took 619 s with
  the 7B model. Concurrency and timeouts come on day 6; caching already makes
  re-runs free.
- Plan changes: Python stays on the stdlib `ast` (exact, no extra dependency);
  embedding ranking and config-file usages move to day 4, where they can be
  measured on the dev split. No npm demo PR yet: the axios 0.x to 1.x bumps
  found span hundreds of note lines, too slow for the CPU workflow; day 3's
  miner will supply npm cases.

Next (day 3): the dataset miner (Dependabot/Renovate PRs with human follow-up
commits as labels, clean merges as no-impact), splits by repo, the import-grep,
symbol-grep and naive-prompt baselines, and `upgrade-impact eval` with
precision/recall, false-alarm rate and bootstrap CIs, with and without review.

