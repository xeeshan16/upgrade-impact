# Roadmap

Where upgrade-impact goes after 0.1.0. Items are ordered by how much they
would change the answers developers get, and each is sized to be one focused
change with tests. Items marked **good first issue** need no knowledge of the
model side.

The held-out results in the README set the order. upgrade-impact rarely
raises a false alarm, but it called 5 of 10 sampled impact cases "no impact":
the changes were to import paths, to APIs the code addresses by string, to a
transitive dependency, or were not in the notes at all. Making "no impact"
trustworthy comes first.

## Answers (recall on real breakage)

- [ ] **Import paths as usages.** Index the module path of every import
  (`react-ace/lib/ace`, `lodash/fp`) and match claims about removed entry
  points, deep imports and `exports` map changes against them.
- [ ] **String-addressed APIs.** Index string literals passed to a package's
  calls (`octokit.request("GET /projects/{id}")`, event names, config keys)
  so claims that name routes or keys can reach them.
- [ ] **Hold "no impact" when the notes are thin.** A major bump whose notes
  have no breaking-looking line, or that only bump a dependency ("updated
  workbox to 7"), should say "review: notes do not list breaking changes"
  rather than "no impact". Measure on new cases, not the test split.
- [ ] **Deprecation history.** When a release note says "the deprecated `app`
  argument has now been removed", read the earlier notes (outside the bump's
  range) that introduced the deprecation and name the API and its
  replacement. This is the cause of the `httpx` `app=` false alarms in the
  README and of several "claims naming no API" cases.
- [ ] **Follow migration-guide links.** Notes that say "see the migration
  guide" link to a page that has the actual list. Fetch linked pages in the
  package's own repository or docs domain, number them like notes, and allow
  claims to cite them.
- [ ] **Embeddings for claims that name no symbol.** Candidate ranking for
  behaviour changes ("connections are now closed on exit") is lexical today.
  Rank the package's call sites by embedding similarity to the claim, behind
  the existing provider interface (Ollama's embedding endpoint works locally).
- [ ] **Config files.** Index `pytest.ini`, `setup.cfg`, `pyproject.toml`
  tool sections, `.babelrc`, `webpack.config.*`, `jest.config.*` and
  `tsconfig.json`, so option renames in tools like pytest-asyncio and
  webpack-dev-server reach the line that sets them.
- [ ] **Cross-module instance types.** Resolve an instance created in one
  module and used in another (`client = httpx.Client()` in `deps.py`, used in
  `routes.py`).
- [ ] **Release notes from more places**: GitHub's compare view for packages
  with no releases and no CHANGELOG (fall back to commit subjects with
  `BREAKING` / `!:` markers, clearly labelled as weaker evidence), and
  `docs/changelog/*.rst` fragments.

## Where developers work

- [ ] **PR comment from the Action**, updated in place on re-runs, as an
  opt-in input (`comment: true`), next to the job summary and annotations.
- [ ] **`upgrade-impact fix`**: for affected lines, ask the model for a patch
  restricted to those lines, apply it to a worktree and run the project's tests;
  keep only patches that pass (the MCP server already gives a coding agent
  enough to do this by hand).
- [ ] **Renovate and Dependabot grouped PRs, run for real.** The parsing is
  tested on both formats but the mined dataset has no grouped PR; mine some
  and add them to the dataset.
- [ ] **Go modules and Maven** ecosystems (a new manifest parser, a registry
  lookup and a tree-sitter index each).

## Evaluation

- [ ] **A model-level comparison**: the same held-out sample with a hosted
  model (the provider interface supports OpenAI, Anthropic and Azure) and with
  a larger local one, to separate what the pipeline contributes from what the
  model contributes.
- [ ] **More labelled impact cases.** 61 impact cases in 259 bumps give wide
  intervals. Scan more repositories with `upgrade-impact mine` and
  hand-check a sample of labels for unrelated cleanup (ADR 0003).
- [ ] **Score the time to an answer** per case alongside quality, so a change
  that adds model calls has to pay for itself.

## Good first issues

- [ ] **good first issue**: resolve version ranges in titles such as
  `>=0.27,<0.29` to the upper bound when the manifest diff is missing (today
  they resolve to the lower bound).
- [ ] **good first issue**: read `.nvmrc`, `.python-version` and the CI
  matrix as extra evidence for the runtime floor (only the root manifest is
  read today).
- [ ] **good first issue**: add more token formats to the secret redaction
  (`src/upgrade_impact/redact.py`) with a test per format.
- [ ] **good first issue**: `--format sarif`, so affected lines show up in
  GitHub code scanning.
- [ ] **good first issue**: add list prices for more hosted models to the
  cost table, each with a source link in a comment.
