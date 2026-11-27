# upgrade-impact

**Does this Dependabot or Renovate bump touch your code, where, and why?**
upgrade-impact reads the release notes between the two versions, turns them into
breaking-change claims with a language model, finds the lines in your repository
that use what changed, and cites both sides: a release-note line and a code line,
each verified by code. If nothing is affected, it says so and tells you why.

> **Status: 0.1.0.** Works end to end for PyPI and npm bumps on Python and
> JavaScript/TypeScript code, as a CLI, a
> [GitHub Action](#in-ci-the-github-action) and an
> [MCP server](#for-coding-agents-the-mcp-server) for Claude Code, Cursor and
> VS Code. Scored once on a held-out split of real bump PRs against grep and
> naive-prompt baselines: see [Evaluation](#held-out-results-v010) for what it
> does well (few false alarms, cited answers) and where it still misses.

## What it does

`uriyyo/fastapi-pagination#1373` bumped httpx from 0.27.2 to 0.28.0,
and the maintainer pushed a fix to the same PR (`81a3960`, "Remove usage of
deprecated app param"). Pointed at the Dependabot commit, before that fix, with
qwen2.5:7b on Ollama (CPU, no API key):

```text
$ upgrade-impact check https://github.com/uriyyo/fastapi-pagination/pull/1373 \
    --ref e14c865aebed311824f192cefe16e9e64a9ade19 --model qwen2.5:7b
upgrade-impact: httpx 0.27.2 -> 0.28.0 (pypi)
release notes: 0.28.0 from github-release (17 lines)
code: 2 usage(s) of httpx in 1 file(s); 100 Python file(s) scanned
REVIEW: possible impact; read the matched lines
4 breaking change(s): 0 affected, 1 possible, 0 need review, 3 not used by your code

[possible] C4 removed: The `app` argument has been removed.
  notes N11 (0.28.0): "* The deprecated `app` argument has now been removed."
  symbols: app=
  tests/conftest.py:271  async with LifespanManager(app), AsyncClient(app=app, base_url="http://testserver", timeout=60) as c:  (passes app to httpx.AsyncClient; the notes do not say which API lost it)
      model said affected, but the notes do not tie the change to the API this line calls (it named 'app'), so it counts as unclear: The code passes the `app` argument to `httpx.AsyncClient`, which aligns with the removed `app` argument in the notes.

[not_found] C1 deprecated: The `verify` argument as a string is deprecated and will raise warnings.
  notes N6 (0.28.0): "* The `verify` argument as a string argument is now deprecated and will raise warnings."
  symbols: verify=

[not_found] C2 deprecated: The `cert` argument is deprecated and will raise warnings.
  notes N7 (0.28.0): "* The `cert` argument is now deprecated and will raise warnings."
  symbols: cert=

[not_found] C3 removed: The `proxies` argument has been removed.
  notes N10 (0.28.0): "* The deprecated `proxies` argument has now been removed."
  symbols: proxies=

model: ollama qwen2.5:7b, 1 call(s), 884 in / 319 out tokens, 135.9 s
code review: ollama qwen2.5:7b, 1 call(s), 744 in / 137 out tokens, 81.2 s
warning: C4: 1 'affected' verdict(s) named no API from the notes for a parameter-only change and count as unclear

note: analysed uriyyo/fastapi-pagination at e14c865aebed (downloaded source files)
```

`tests/conftest.py:271` is the line the maintainer changed. Every claim quotes the
release-note line it comes from, and the quotes were checked against the notes.
The `proxies`, `verify` and `cert` changes are "not used": no call passes those
arguments. The `app` line stays "possible": the review step read it and said
"affected", but httpx's notes never say which API lost `app` (httpx also has
`ASGITransport(app=...)`, the replacement), so the verdict is not accepted.

That rule is there because of the second demo PR, `cereja-project/govbr_auth#53`
(httpx 0.27 to 0.28.1, merged with manifest changes only). Its 36 "possible" `app`
matches are all `ASGITransport(app=...)` calls, i.e. false alarms. Before the rule,
qwen2.5:7b called six of the twelve it read "affected"; with it, all twelve stay
"possible" and none is promoted. The review needs more context than these notes
give to clear them (the 0.27.0 deprecation notice that names the replacement), which
is still open (see [Limitations](#limitations)).

Both outputs are from the [`real-model` workflow](.github/workflows/real-model.yml)
([run 45027489304](https://github.com/xeeshan16/upgrade-impact/actions/runs/45027489304),
commit `5501a54`). These are two hand-picked PRs, not an evaluation; see
[Evaluation](#evaluation) for the labelled dataset and baselines.

## Quick start (5 minutes)

```bash
# 1. Install (Python 3.11+). Not on PyPI yet, so install from the repository:
uv tool install git+https://github.com/xeeshan16/upgrade-impact
# or: pipx install git+https://github.com/xeeshan16/upgrade-impact

# 2. A model. Local and free (default):
ollama pull qwen2.5:7b
#    ...or a hosted one: export OPENAI_API_KEY=... / ANTHROPIC_API_KEY=...

# 3. Check a bump PR (its code is downloaded; or run inside your checkout):
export GITHUB_TOKEN=...   # optional for public repos, raises the rate limit
upgrade-impact check https://github.com/you/app/pull/42

# Or check a bump you are about to make, against the current directory:
upgrade-impact check --package httpx --from 0.27.2 --to 0.28.1
git diff main -- requirements.txt | upgrade-impact check -
```

`--format markdown` writes a table for a PR comment or job summary;
`--format json` writes the full report. `--no-llm` skips the model and shows
which release notes were found and where the package is used.

Grouped PRs ("Bump the pip group with 5 updates") are checked package by
package, four at a time (`--jobs`), and listed worst first; `--only httpx`
picks packages. `--max-seconds 600` bounds a check: model stages not started by
then are skipped and those packages say "review". For scripts:
`--json-out`, `--summary-file`, `--annotate` (GitHub annotations) and
`--fail-on affected|review|unknown`, which exits with code 3 when the verdict
is that or worse.

With Docker instead (the image is built and smoke-tested in CI, not published):

```bash
docker build -t upgrade-impact https://github.com/xeeshan16/upgrade-impact.git
docker run --rm -v "$PWD:/work" -e GITHUB_TOKEN \
  -e OLLAMA_HOST=http://host.docker.internal:11434 --add-host=host.docker.internal:host-gateway \
  upgrade-impact check --package httpx --from 0.27.2 --to 0.28.1
```

## In CI: the GitHub Action

Copy [examples/workflows/upgrade-impact.yml](examples/workflows/upgrade-impact.yml)
to `.github/workflows/`. On every Dependabot or Renovate PR it writes the
report to the job summary, puts a warning on each affected line and a notice on
each possible one, and can fail the check:

```yaml
- uses: actions/checkout@v4
- uses: actions/setup-python@v5
  with:
    python-version: "3.12"
- id: impact
  uses: xeeshan16/upgrade-impact@main
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}  # or OPENAI_API_KEY, Azure, base-url
  with:
    fail-on: affected        # never (default), affected, review or unknown
- run: echo "${{ steps.impact.outputs.verdict }}"
```

| Input | Default | |
|---|---|---|
| `pr-url` | the triggering PR | any GitHub PR URL |
| `ref` | checkout / PR head | analyse the code at this commit |
| `only` | all packages | packages of a grouped PR (space or comma separated) |
| `provider`, `model`, `base-url` | from the step's env | as in [Configuration](#configuration) |
| `no-llm` | `false` | never call a model |
| `fail-on` | `never` | `affected`, `review`, `unknown` |
| `annotate` | `true` | line annotations |

Outputs: `verdict` (`affected`, `review`, `unknown`, `no-impact`, or `error`),
`affected-lines`, `packages` and `report-json` (path to the full report).
Workflows that Dependabot triggers do not see repository secrets, so add the
API key as a Dependabot secret too, or point `base-url` at a model server you
run. With no model configured the action says so and runs the no-model check
(breaking-looking note lines and import sites) instead of failing. CI runs the
action on the real fastapi-pagination PR on every push
([`action` job](.github/workflows/ci.yml)).

## For coding agents: the MCP server

`upgrade-impact mcp` serves three tools over stdio:

| Tool | Model? | What it returns |
|---|---|---|
| `assess_bump` | yes | for a PR URL, a manifest diff, or package + versions: each breaking change with its quoted note line, the code lines it reaches, and a verdict per package |
| `find_usages` | no | every import, call (with keyword arguments or options), attribute and method use of a package |
| `get_release_notes` | no | numbered release notes between two versions, breaking-looking and instruction-like lines marked |

Claude Code:

```bash
claude mcp add upgrade-impact -- uvx --from git+https://github.com/xeeshan16/upgrade-impact upgrade-impact mcp
```

Cursor and VS Code: copy [examples/mcp/cursor.mcp.json](examples/mcp/cursor.mcp.json)
to `.cursor/mcp.json` or [examples/mcp/vscode.mcp.json](examples/mcp/vscode.mcp.json)
to `.vscode/mcp.json` ([Claude Code JSON](examples/mcp/claude-code.mcp.json) too).
The server reads code only under its working directory (or the directories in
`UPGRADE_IMPACT_MCP_ROOTS`), runs model calls one at a time, labels release-note
text as untrusted and flags lines that read like instructions to a model. Then
ask: *"Is the axios bump in PR #212 safe to merge? Fix whatever it breaks."*

## How it works

```mermaid
flowchart LR
  PR[PR URL / manifest diff / flags] --> B[Resolve bumps<br/>manifest diff > PR body tables > title]
  B --> R[Registry lookup<br/>PyPI / npm -> GitHub repo]
  R --> N[Release notes in range<br/>GitHub Releases, CHANGELOG, PR body]
  N --> X[LLM extraction<br/>numbered, fenced notes -> JSON claims]
  X --> V[Verify citations<br/>quote on cited line, symbols in notes]
  PR --> C[Code<br/>local checkout or PR tarball]
  C --> I[Usage index<br/>Python ast / JS+TS tree-sitter]
  V --> U[Unclaimed breaking-looking lines<br/>runtime floor check, then a second reading]
  U --> M[Deterministic matching<br/>+ runtime floor for runtime claims]
  V --> M
  I --> M
  M --> A[LLM review of uncertain matches<br/>code window + note lines -> cited verdicts]
  A --> W[Verify both citations<br/>unverified = unclear]
  W --> O[Report: affected / possible / needs review / not used]
```

1. **Bump.** Manifest diffs give exact names and versions; the PR body
   (Renovate's table, Dependabot's group table and "Updates `pkg` from A to B"
   lines, skipping the notes Dependabot quotes) and the title fill the gaps
   (lock-file-only PRs). A grouped PR gives one bump per package, each with its
   own slice of the PR body as last-resort notes, and the PR's verdict is the
   worst of them.
2. **Release notes.** The package's repository comes from PyPI `project_urls` or
   npm `repository`. Each version in `(from, to]` takes its GitHub release body,
   or its CHANGELOG section when the release is missing or just says "see
   CHANGELOG". Pre-releases are skipped unless you are upgrading to one.
3. **Claims.** The notes go to the model as numbered lines between fence
   markers derived from their content, with a JSON schema. Each claim must quote the line it comes
   from. Quotes that aren't on the cited line drop the claim; symbols the notes
   never mention are set aside. See [ADR 0001](docs/adr/0001-verified-claims-then-deterministic-matching.md).
4. **Code.** Python files are parsed with `ast`: `import httpx as h` then
   `h.AsyncClient(timeout=5)` is a call to `httpx.AsyncClient` with keyword
   `timeout`, and `c = httpx.Client()` makes `c.get(url)` a call to
   `httpx.Client.get`. JavaScript and TypeScript are parsed with tree-sitter:
   `axios.get(url, { timeout: 5 })` passes `timeout`, `<QueryClientProvider
   client={qc}>` passes `client`, `require` and barrel re-exports are followed.
   Virtualenvs, `node_modules`, `site-packages` and build output are skipped.
5. **Matching.** A claim about a parameter only matches calls that pass it (or
   forward `**kwargs`); removed and renamed symbols match any use; behaviour
   changes on a used symbol are "possible"; claims that name nothing are "needs
   review" when the package is used at all.
6. **Review.** Every match that is not certain goes back to the model with a
   numbered window of the code and the note lines around the claim. It answers
   affected / not affected / unclear per line, citing a code line and a note
   line; both quotes are checked. A verdict that fails the check counts as
   unclear, so an unverified "not affected" never hides a line, and cleared
   lines stay in the report with the reason. See
   [ADR 0002](docs/adr/0002-model-review-of-candidates-with-two-sided-citations.md).
   Replies are cached on disk, so a re-run costs nothing.
7. **Earning "no impact".** A note line that reads like a breaking change and
   that no claim cites holds the bump at "review", because small models skip
   lines. Such a line can leave that list three ways: it is a section heading;
   it is a runtime drop ("Drop support for Node.js 18") that the project's
   declared floor (`engines.node`, `requires-python`, `python_requires`)
   already meets, which is checked by code and cites the manifest line; or a
   second, focused model reading (20 lines per call) quotes the line and says
   it does not change the package's API, and the line names nothing your code
   uses from the package. A "yes" in that reading becomes a claim like any
   other. Runtime claims are checked against the same floor. Every ruled-out
   line is listed with who ruled it out and why, and the report says what
   keeps a bump at "review". See
   [ADR 0004](docs/adr/0004-earning-no-impact.md).
8. **Untrusted notes.** Release notes and PR bodies are written by whoever
   publishes the package. They are fenced with a content-derived marker and
   called data, every claim must quote them, and lines that read like
   instructions to a model (or try to close the fence, or speak the tool's
   JSON) are flagged: a flagged line can't be ruled out and keeps the bump
   from "no impact". Code shown to a model for review has secrets redacted
   first. The full threat model is [ADR 0005](docs/adr/0005-threat-model.md).

## Evaluation

Does it beat grep? `upgrade-impact eval` answers that on real PRs.

**Dataset** ([data/](data/README.md)): 259 merged Dependabot and Renovate PRs
from 51 public repositories, mined from git history by `upgrade-impact mine`.
61 are *impact* cases: a maintainer pushed code onto the bump PR, and the lines
they changed are the labels. 198 merged with manifest changes only. Only majors
(or 0.x minors) of packages the code imports count, and the dev and held-out
test splits never share a repository
([ADR 0003](docs/adr/0003-labels-from-human-follow-up-commits.md)).

**Systems**, all on the same code snapshot and the same cached release notes:

| System | What it flags |
|---|---|
| `upgrade-impact` | affected lines (strict), plus "possible" ones (lenient) |
| `import-grep` | every line that imports the package |
| `symbol-grep` | identifiers quoted on breaking-looking note lines, grepped in importing files |
| `naive-prompt` | one model call with the notes and the importing files, no verification |

**Metrics**: file precision and recall, hunk recall (a predicted line within
two lines of a fixed hunk), detection (impact cases with at least one right
file), false alarms (clean bumps with anything flagged), lines flagged per
case, model calls and tokens. 95% intervals resample repositories.

```bash
uv run upgrade-impact eval data/cases.jsonl --split dev --model qwen2.5:7b
uv run upgrade-impact eval data/cases.jsonl --split dev --no-llm   # grep baselines only
# the held-out numbers below:
uv run upgrade-impact eval data/cases.jsonl --split test --per-label 10 --model qwen2.5:7b
```

### Held-out results (v0.1.0)

Scored once on the held-out **test split** (139 bumps from 29 repositories, 38
impact and 101 clean; no repository is shared with the dev split used during
development). Nothing below was tuned on these cases.

**With a real model, on a fixed sample of the test split**: 10 impact and 10
clean cases from 15 repositories, picked by a hash of the case id. qwen2.5:7b
on Ollama on a CPU GitHub runner; the tool's default settings except at most 3
uncached review calls per bump (`UPGRADE_IMPACT_MAX_REVIEW_CALLS=3`), a 1,200 s
per-call timeout and 1 retry; no cached replies; a line hit is within 2 lines
of a fixed hunk
([run 46001813142](https://github.com/xeeshan16/upgrade-impact/actions/runs/46001813142),
commit `b8198a6`). 95% intervals resample repositories.

| System | Scoring | File precision | File recall | Hunk recall | Detected | False alarms | Lines/case |
|---|---|---|---|---|---|---|---|
| upgrade-impact | strict | 50.0% (0-100) | 8.7% (0-21) | 5.8% (0-10) | 20.0% (0-50) | **0.0%** (0-0) | 0.3 |
| upgrade-impact | lenient | 33.3% (16-100) | 21.7% (7-44) | 8.7% (0-16) | 40.0% (14-60) | 10.0% (0-33) | 3.1 |
| import-grep | strict | 16.1% (12-37) | 82.6% (62-94) | 7.7% (3-13) | 90.0% (67-100) | 100.0% | 10.4 |
| symbol-grep | strict | 35.3% (23-100) | 26.1% (0-54) | 10.6% (0-23) | 40.0% (0-75) | 40.0% (0-75) | 6.5 |
| naive-prompt | strict | 40.0% (0-100) | 8.7% (0-28) | 1.0% (0-7) | 20.0% (0-50) | 50.0% (17-88) | 0.7 |

*Strict* counts only "affected" lines; *lenient* adds "possible" ones.
upgrade-impact made 71 model calls (111,196 input / 33,180 output tokens),
about 12.5 minutes per bump on the CPU runner; the naive prompt made 20.

**Without a model, on the whole test split** (all 139 cases,
[run 46002112645](https://github.com/xeeshan16/upgrade-impact/actions/runs/46002112645)):

| System | File precision | File recall | Hunk recall | Detected | False alarms | Lines/case |
|---|---|---|---|---|---|---|
| import-grep | 6.4% (4-9) | 43.9% (31-80) | 2.7% (1-13) | 86.8% (75-96) | 100.0% | 21.9 |
| symbol-grep | 8.3% (5-21) | 19.7% (12-46) | 8.9% (6-26) | 44.7% (29-60) | 25.7% (18-34) | 88.7 |

**What this says.**

- *Its warnings are worth reading.* On the 10 clean bumps upgrade-impact
  flagged no line as affected, while grepping imports flagged every one,
  grepping the notes' identifiers flagged 4 and the single naive prompt 5. On
  the whole split, symbol-grep's 89 lines per case is what a reviewer would
  otherwise read.
- *It misses most real breakage.* It detected 2 of 10 impact cases strictly
  and 4 of 10 counting "possible" lines; import-grep found the right file in 9
  of 10, at the price of flagging every clean bump.
- *"No impact" is not yet safe to trust on its own.* Of the 10 bumps it
  called "no impact", 5 were impact cases. Reading those five: a deep-import
  path that stopped working (`react-ace/lib/ace`), removed REST routes the
  code calls as strings (`@octokit/plugin-rest-endpoint-methods`), a breaking
  change in a transitive dependency (`vite-plugin-pwa` via workbox), and two
  packages whose notes (4 and 8 lines) describe no breaking change at all (the
  labels are the lines a maintainer changed on the PR, which can include
  related cleanup; ADR 0003). On the clean bumps it answered "no impact" 5
  times and "review" 5 times; 1 impact case had no release notes ("unknown").
- 20 cases give wide intervals; treat the sample rows as a direction, not a
  ranking. These failure causes were found by reading held-out output, so a
  fix for them has to be measured on new cases (see the
  [roadmap](docs/ROADMAP.md)).

### Development history (dev split)

On a fixed 8-case dev sample, the day 3 code answered "review" for every case
with notes and flagged no line; after the day 4 changes (ADR 0004) it answered
3 of 4 clean bumps "no impact" with no false alarm and found 1 of 4 impact
cases strictly, 3 of 4 counting "possible" lines
([run 45405936719](https://github.com/xeeshan16/upgrade-impact/actions/runs/45405936719)).
Those changes were found by reading that sample, so they measure progress, not
generalisation. CI scores the dev split with the fake provider behind a gate
(`data/gate.json`) on every push.

## Configuration

| Setting | Env var | Default |
|---|---|---|
| provider | `UPGRADE_IMPACT_PROVIDER` | `auto`: Anthropic, Azure, OpenAI if their keys are set, else Ollama |
| model | `UPGRADE_IMPACT_MODEL` | `qwen2.5:7b` (Ollama), `gpt-4.1-mini`, `claude-haiku-4-5` |
| base URL | `UPGRADE_IMPACT_BASE_URL` | any OpenAI-compatible server (vLLM, LM Studio) |
| notes chunk size | `UPGRADE_IMPACT_NOTES_BUDGET_TOKENS` | `3000` tokens per extraction call |
| timeout / retries | `UPGRADE_IMPACT_TIMEOUT_S`, `UPGRADE_IMPACT_MAX_RETRIES` | `300`, `3` |
| review cap | `UPGRADE_IMPACT_MAX_CANDIDATES`, `UPGRADE_IMPACT_MAX_REVIEW_CALLS` | `12` code candidates per claim, `8` uncached review calls per bump (`--no-review` turns review off) |
| second reading | `UPGRADE_IMPACT_RECHECK` | `on`: re-read unclaimed breaking-looking note lines (`off` leaves them for you) |
| reply cache | `UPGRADE_IMPACT_LLM_CACHE` | `on` (`off` or `--no-cache` to bypass) |
| parallel packages | `UPGRADE_IMPACT_JOBS`, `UPGRADE_IMPACT_MODEL_CONCURRENCY` | `4` packages of a grouped PR at once (`--jobs`), `1` model call in flight (Ollama serves one at a time; raise it for hosted APIs) |
| time budget | `UPGRADE_IMPACT_MAX_SECONDS` | none (`--max-seconds`, action input `max-seconds`): model stages not started when it runs out are skipped and the package says "review" |
| prices | `UPGRADE_IMPACT_PRICE_IN`, `UPGRADE_IMPACT_PRICE_OUT` | built-in list prices for common hosted models, `$0` for Ollama; USD per million input / output tokens overrides them |
| cache dir | `UPGRADE_IMPACT_CACHE_DIR` | `~/.cache/upgrade-impact` (downloaded PR code, model replies, eval clones and notes) |
| git host for `mine`/`eval` | `UPGRADE_IMPACT_GIT_URL` | `https://github.com` |

The same keys (lower-case, without the prefix) work in `.upgrade-impact.toml`.
Provider credentials: `OLLAMA_HOST`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_DEPLOYMENT`;
GitHub: `GITHUB_TOKEN` or `GH_TOKEN` (sent only to the GitHub API host).

### Cost, time and traces

Every report ends with a total line (and `accounting` in the JSON):

```text
total: 9 model call(s), 8,743 in / 2,159 out tokens, $0 (local model), 0.6 s
```

(That line is from the fake test provider on the fixture repo; a hosted model
shows `est. $0.0123 (list price)`.) For traces and metrics, install the extra
and turn it on:

```bash
pipx install "upgrade-impact[otel] @ git+https://github.com/xeeshan16/upgrade-impact"
UPGRADE_IMPACT_OTEL=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  upgrade-impact check https://github.com/OWNER/REPO/pull/123
```

A `check` span holds a `bump` span per package (also when packages run in
parallel), each with `notes`, `index`, `extract`, `recheck` and `review` spans
tagged with counts, model and verdict. Metrics: `upgrade_impact.model.calls`,
`upgrade_impact.model.tokens`, `upgrade_impact.model.cost_usd`,
`upgrade_impact.bumps` (by verdict) and `upgrade_impact.stage.duration`. Without
an OTLP endpoint they are printed to stderr. Release-note text and code never go
on a span.

## Design trade-offs

- **The model reads; code checks.** The model turns prose into cited claims and
  reads borderline code lines, but every claim and every verdict must cite
  lines that code then finds. Certain matches never go to the model, and an
  uncheckable verdict changes nothing. That keeps the answer auditable and
  makes small local models usable.
- **Parameter-aware matching** trades recall on dynamic code (`**kwargs`
  forwarding is flagged as "possible", not "affected") for confident "no
  impact" answers on the common case.
- **Release notes over diffs.** Diffing the package's source between versions
  would catch undocumented changes but costs far more and misses behaviour
  changes; notes are what maintainers write for exactly this purpose.

## Limitations

- "No impact" can be wrong: on the held-out sample it was the answer for 5
  impact cases (see [Evaluation](#held-out-results-v010)). Import-path
  changes (`pkg/lib/x` deep imports), string-addressed APIs (REST routes,
  event names), breaking changes in a dependency's own dependencies and
  release notes that do not mention the break are not caught.
- Real-model numbers come from a 20-case held-out sample with a 7B local
  model on a CPU runner; a larger or hosted model has not been measured. The
  dataset's labels are what maintainers changed on the PR, which includes some
  unrelated cleanup and misses fixes that landed in later PRs (ADR 0003).
- The Action writes the job summary and annotations; it does not post a PR
  comment yet.
- The time budget is checked between stages: a stage already running
  finishes (each model call is bounded by `UPGRADE_IMPACT_TIMEOUT_S`).
- Secret redaction is pattern-based (known token formats and secret-named
  literals); an unusual format in a reviewed code window is sent as is. With
  a hosted model, code windows around matched lines leave the machine by
  design; use Ollama or `--no-review` if that is not allowed.
- Cost estimates use a hand-maintained table of list prices; set your own with
  `UPGRADE_IMPACT_PRICE_IN` / `_OUT`.
- Grouped-PR parsing is tested on Dependabot's and Renovate's body formats,
  not yet run on a real grouped PR (the mined dataset has none).

- Instance types are inferred within one file and without flow analysis: an
  instance created in one module and used in another is not resolved, and a
  name rebound to something else keeps the first class.
- Config files (`pytest.ini`, `setup.cfg`, bundler configs) are not indexed.
- Candidate ranking for claims that name no symbol is lexical; embeddings are
  an extension point, not built yet.
- The second reading's guard is lexical: a ruled-out line that describes a
  change to something your code uses without naming it can get through. The
  runtime check reads only the root manifest (not `.nvmrc`, CI matrices or
  workspace packages).
- Deprecation history is not used yet: "the deprecated `app` argument has now
  been removed" does not say which API, and the earlier notice that does is
  outside the bump's range (the govbr_auth false alarms above).
- Version ranges in titles such as `>=0.27,<0.29` resolve to the lower bound.
- On a CPU a bump takes minutes (about 12.5 on a GitHub runner with
  qwen2.5:7b); a GPU or a hosted model is much faster.

## What I'd do next

The [roadmap](docs/ROADMAP.md) has the list. The first three follow from the
held-out misses: read deprecation history and the migration guides the notes
link to, so claims name the API that changed; index import paths and string
routes as usages; and measure a hosted model on the same sample, to separate
what the pipeline contributes from what a 7B model can read.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). `uv sync && uv run pytest`.

## License

MIT, see [LICENSE](LICENSE).
