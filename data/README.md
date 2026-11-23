# Dataset: real dependency bumps and the code they broke

`cases.jsonl` is the labelled set the eval runs on. Each line is one merged
Dependabot or Renovate PR from a public GitHub repository:

- **impact**: a maintainer pushed code changes onto the bump PR. The changed
  lines are the labels, as old-side line ranges in the *snapshot* (the bot's
  commit, before the fix).
- **no_impact**: the PR merged with manifest and lock-file changes only, and no
  later commit fixed or reverted it.

How cases are mined, filtered and scored is in
[ADR 0003](../docs/adr/0003-labels-from-human-follow-up-commits.md).

## Contents (mined 2026-11-23)

| Split | Repositories | Impact (PyPI / npm) | No impact (PyPI / npm) |
|---|---|---|---|
| dev | 22 | 23 (12 / 11) | 97 (49 / 48) |
| test (held out) | 29 | 38 (23 / 15) | 101 (68 / 33) |
| total | 51 | 61 | 198 |

154 repositories were scanned ([repos.txt](repos.txt)); 51 had at least one
qualifying case. Merges span 2023-11 to 2026-11. Impact cases come from 15
merge-commit PRs and 46 squash merges with a human co-author. All 280 labelled
files have line labels that map onto the snapshot.

## Rebuild it

```bash
uv run upgrade-impact mine --repos-file data/repos.txt -o data/cases.jsonl
```

The miner needs only git over HTTPS (no token): bare partial clones go to
`~/.cache/upgrade-impact/git`. Defaults: the last three years of first-parent
history (at most 5,000 commits), major bumps and 0.x minor bumps only, at most
25 impact and 6 no-impact cases per repository. History moves on, so a rebuild
later will differ; the committed file is the frozen set the README numbers
refer to.

## Fields

| Field | Meaning |
|---|---|
| `id` | `owner/repo#PR:package` |
| `url` | the PR (or the commit, for squash merges without a PR number) |
| `ecosystem`, `package`, `from_version`, `to_version` | the bump |
| `snapshot` | commit whose code is analysed |
| `label` | `impact` or `no_impact` |
| `files[]` | `path`, `lines_known`, `hunks[]` (`start`, `end`, `pure_insert`) |
| `fix_commits` | the human commits (or the squash commit) the labels come from |
| `merge_style` | `merge` or `squash` |
| `split` | `dev` or `test`, by a hash of the repository name |
| `import_files` | files importing the package at the snapshot |

## Provenance and licence

The labels are facts derived from public commit history: commit ids, file
paths, line numbers and version strings. No source code from these
repositories is stored here; the eval fetches each snapshot from GitHub when it
runs. Every case links to its PR. The dataset file itself is MIT-licensed with
the rest of this repository.
