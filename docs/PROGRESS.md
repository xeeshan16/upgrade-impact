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


## Day 3 — 2026-11-23: dataset and eval harness

Built:

- `upgrade-impact mine`: scans the first-parent history of public
  repositories (bare partial clones over HTTPS, no token) for merged
  Dependabot/Renovate PRs. Merge commits give the PR's own commits; bot-authored
  squash merges give human `Co-authored-by` trailers. A human commit that
  changed an existing source file makes an impact case, labelled with the
  old-side hunks in the snapshot (the bot's commit, or the squash's parent);
  a bot-only, manifest-only PR is a no-impact case unless a later human commit
  names the package or reverts it. Majors and 0.x minors of imported packages
  only; at most 6 clean cases per repository; split by repository (ADR 0003).
- `data/cases.jsonl`: 154 repositories scanned, 51 contributed. 259 cases:
  dev 23 impact + 97 no-impact (22 repositories), test 38 + 101
  (29 repositories). 46 of the 61 impact cases are squash merges with a human
  co-author. The `httpx` 0.28 case from day 1 (`fastapi-pagination#1373`) came
  out of the miner with the same label (`tests/conftest.py:271`, plus the
  import line the maintainer also changed).
- `upgrade-impact eval`: the tool, import-grep, symbol-grep and a naive single
  prompt on the same snapshot and the same cached notes; file and line
  precision/recall, detection, false alarms, strict and lenient scoring,
  repository-level bootstrap intervals, call and token totals, per-case
  predictions in `cases.jsonl`, `--gate`, `--per-label` sampling.
- CI: an eval job on the dev split with the fake provider, gated by
  `data/gate.json` (every case scored, no system errors, sanity floors); the
  installed wheel runs a two-case eval. The `real-model` workflow takes
  `eval_per_label` to run the eval with Ollama on a fixed dev sample.

Found while building:

- The first miner pass labelled `github/codeql-action` bumps with ~1,000-hunk
  changes to `lib/*.js`: the repository commits its compiled bundle. Fix files
  with more than 40 hunks or 300 changed lines are now dropped as generated.
- Impact cases are rare: across 154 active repositories only 61 bumps had a
  human push a fix onto the bot's PR. Many large projects (fastapi, pydantic-ai,
  moto, kombu) had none; maintainers there fix in separate PRs, which this
  miner cannot see. The set over-represents projects that fix on the bot's
  branch.
- Release notes are the bottleneck for "unknown" answers: in the build sandbox
  (no GitHub API) most cases had no notes; in CI, with GitHub Releases, 3 of 23
  dev impact cases and 11 of 97 clean ones still had none (packages without a
  GitHub repository in their registry metadata, or without releases or a
  CHANGELOG).
- Measured in CI on the dev split
  ([run 45184488973](https://github.com/xeeshan16/upgrade-impact/actions/runs/45184488973)),
  no model involved: import-grep detects 91.3% (81-100) of impact cases with
  63.5% file recall, but flags 100% of clean bumps and 10.5 lines per case;
  symbol-grep detects 26.1% (12-38) with 37.0% file precision and 23.7%
  (14-34) false alarms. The fake provider's numbers for upgrade-impact and the
  naive prompt are pipeline checks, not results.
- A real-model eval on a dev sample (4 impact + 4 clean cases, qwen2.5:7b,
  at most 3 review calls per bump) was started in the `real-model` workflow;
  its numbers go into tomorrow's entry.

Next (day 4): treat release notes as untrusted input (fencing is in place;
injection checks and output validation), slice long changelogs by version,
follow migration-guide links, the deprecation-history context for the
govbr_auth false alarms, and calibrate "no impact", each measured on the dev
split with a real model.


## Day 4 — 2026-11-24: quality and safety ("no impact" has to be earned)

Day 3's real-model eval on the fixed dev sample (qwen2.5:7b, 4 impact + 4
clean cases,
[run 45186482397](https://github.com/xeeshan16/upgrade-impact/actions/runs/45186482397))
answered "review" for every case that had release notes and flagged no line:
0% false alarms, but 0% detection, and never a "no impact". Its artifacts are
not readable from the build sandbox, so the first change made the eval explain
itself in the CI log; everything after that was found by reading it.

Built:

- **Runtime floor check** (`runtime.py`): "Drop support for Node.js v18",
  "requires Python 3.10 or later" and similar are compared with the floor the
  project declares (`engines.node`, `requires-python`, Poetry's `python`,
  `python_requires`), at the precision the note uses, citing the manifest
  line. Eval snapshots and PR downloads now include the root manifests.
- **Second reading** (`recheck.py`): unclaimed breaking-looking note lines go
  back to the model, 20 per call, one verified answer per line. "Yes" becomes
  a claim; "no" dismisses the line unless it names something the code uses
  from the package (the reachability guard). Failures keep the line.
  `UPGRADE_IMPACT_RECHECK=off` disables it.
- **Injection tripwire** (`guard.py`): note lines that read like instructions
  to a model are reported, can't be dismissed, and keep a bump from "no
  impact" (ADR 0004).
- Section headings no longer count as unclaimed changes.
- **Changed defaults**: matched to the calls that do *not* pass the parameter
  (calls that set it are unaffected); changed-default phrasings mark a line as
  breaking-looking; bare names in a default-change claim are parameters.
- Reports say why a bump is "review" and list ruled-out lines; the eval
  counts review causes and dismissals, lists injection-flagged lines and, for
  small runs, every case and its claims.
- ADR 0004.

Found while building (each from the per-case output of a real-model run):

- The clean bumps were held by `### ⚠ BREAKING CHANGES` headings, "Remove old
  template" / "Remove outdated info from README" (sinon), "Remove
  sinon.defaultConfig" (never called by nestjs/nest), and "Drop support for
  Node.js v18" in a repository whose `package.json` requires `>= 20`.
- With those settled, mdn/yari's dotenv 16 -> 17 bump turned into a false
  "no impact"
  ([run 45399424834](https://github.com/xeeshan16/upgrade-impact/actions/runs/45399424834)):
  17.0.0 flipped the `quiet` default and the maintainer added `quiet: true` to
  four `config()` calls. Three bugs stacked: the parameter rule matched only
  calls that pass `quiet` (right for a removed argument, wrong for a default),
  "Default `quiet` to false" matched no breaking keyword, and the model put
  `quiet` in `symbols`. The safety net had been hiding all three behind
  unrelated unclaimed lines. Fixed in that order, re-measured after each
  ([45401201509](https://github.com/xeeshan16/upgrade-impact/actions/runs/45401201509),
  [45404111514](https://github.com/xeeshan16/upgrade-impact/actions/runs/45404111514)).
- The tripwire's first patterns flagged two real note lines in the dev split
  (CI eval, fake provider): uvicorn's "waiting for the client's close reply
  with a 10 second timeout" and an agent SDK's "...out of the system
  prompt". The eval now lists flagged lines; "reply with" now has to start a
  sentence (or say "only"), and a bare "system prompt" no longer counts. Both
  lines are regression tests.

Measured (qwen2.5:7b, same 8-case dev sample, at most 3 review calls per bump,
[run 45405936719](https://github.com/xeeshan16/upgrade-impact/actions/runs/45405936719)):

| | day 3 code | day 4 code |
|---|---|---|
| clean bumps answered "no impact" | 0 of 4 | 3 of 4 |
| false alarms (strict / lenient) | 0% / 0% | 0% / 0% |
| impact cases detected (strict / lenient) | 0% / 0% | 25% / 75% |
| file recall (strict / lenient) | 0% / 0% | 20% / 80% |
| lines flagged per case (lenient) | 0.0 | 7.4 |

On the same sample symbol-grep detects 75% with 0% false alarms but flags 22.1
lines per case; import-grep detects 100% with 100% false alarms. These cases
drove today's fixes, so this is development progress on a tiny sample, not a
result; the held-out split is scored on day 7. The remaining "review" on a
clean bump (sinon) is a claim with no API ("one breaking change, which only
affects sinon-test"); redis's 49 "possible" lines come from a default-change
claim naming four options; ormar's `engine=` matches stay "possible" because
the notes never name the API in a form the review accepts.

Plan changes: following migration-guide links and the deprecation-history
context move to day 6 if the real-model runs there show misses they would fix;
embedding ranking and config-file usages move to the roadmap (no measured case
needed them). See PLAN.

Next (day 5): the GitHub Action (job summary, annotations, JSON output),
grouped PRs with many packages, and the MCP server (`assess_bump`,
`find_usages`, `get_release_notes`) with configs for Claude Code, Cursor and
VS Code.


## Day 5 — 2026-11-25: where developers work

Built:

- **Grouped PRs.** Dependabot's grouped updates list packages that moved only
  in a lock file in the PR body, as "Updates `pkg` from A to B" lines or, for
  bigger groups, a `| Package | From | To |` table. Both are read; notes that
  Dependabot quotes inside `<details>` are skipped (they can contain their own
  "Updates ..." lines). Each package is checked on its own, bumps are listed
  worst first, and the PR's verdict is the worst one. `--only` picks packages.
- **CI outputs for `check`:** `--json-out`, `--summary-file`, `--annotate`
  (a warning per affected line, a notice per possible one, capped at GitHub's
  10 per level with a count of the rest) and `--fail-on` (exit code 3). The
  JSON now carries the verdict of the report and of each bump.
- **GitHub Action** (`action.yml`, composite): installs from the action's own
  checkout, checks the triggering PR (or `pr-url`), writes the job summary,
  annotates lines, and outputs `verdict`, `affected-lines`, `packages` and
  `report-json`. With no model configured it says so and runs the no-model
  check rather than failing on a missing Ollama. Example workflow in
  `examples/workflows/`.
- **MCP server** (`upgrade-impact mcp`): `assess_bump` (PR URL, manifest diff,
  or package and versions), `find_usages` and `get_release_notes` (no model),
  with structured output, progress per package, local paths limited to the
  working directory or `UPGRADE_IMPACT_MCP_ROOTS`, one model call at a time,
  release-note text labelled as untrusted and instruction-like lines flagged.
  The JSON-RPC layer is the stdio implementation from ci-pinpoint. Configs for
  Claude Code, Cursor and VS Code in `examples/mcp/`.

Found while building:

- The PR-body fallback for release notes handed a grouped PR's whole body to
  every package. In the grouped test PR, `rich` (no notes found anywhere) came
  out "no impact" by reading the httpx notes quoted in the body. Each package
  now gets only its own block of the body, and `rich` is "unknown", as it
  should be.
- Workflows triggered by Dependabot don't get repository secrets, so an Action
  that needs an API key would fail on exactly the PRs it is for. The example
  workflow and README say to add the key as a Dependabot secret, and without
  any key the Action degrades to the no-model check instead of erroring.

Verified:

- Locally: ruff, mypy strict and 332 tests (92% coverage), including the
  official MCP client SDK driving `upgrade-impact mcp` as a subprocess, and
  the action's shell script against a manifest diff with the fake provider
  and with no model.
- In CI
  ([run 45619033448](https://github.com/xeeshan16/upgrade-impact/actions/runs/45619033448),
  `5646d63`): the action itself (`uses: ./`) on the real fastapi-pagination
  httpx bump returns `verdict=review` with `tests/conftest.py:271` among the
  matched lines, `fail-on: review` fails the step, and the no-model path runs;
  the installed wheel's MCP server answers the handshake and lists its three
  tools.
- Not yet: a real grouped PR (the dataset has none; the formats are tested
  from their documented shapes) and the Action with a real model (added to
  the `real-model` workflow, which runs after CI on main).

Next (day 6): OpenTelemetry traces and metrics, token and cost accounting,
concurrent checks of grouped PRs, timeouts, a Docker image smoke-tested in
CI, the threat-model ADR, and a larger real-model eval run on the dev split.
