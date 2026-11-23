# ADR 0003: Labels come from the commits humans pushed onto bump PRs

- Status: accepted
- Date: 2026-11-23

## Context

The tool's promise is "these lines must change, or nothing does", and the only
way to know whether it keeps it is to score it on real bumps where the answer
is known. No public dataset labels dependency bumps with the code they broke.
Hand-labelling breaks two ways: it doesn't scale past a few dozen PRs, and the
labeller's idea of "affected" quietly becomes the tool's.

Real maintainers already produce the labels. When a Dependabot or Renovate PR
breaks something, a maintainer often pushes the fix onto the same branch
(`uriyyo/fastapi-pagination#1373`: the bot bumps httpx, the maintainer pushes
`81a3960` replacing `AsyncClient(app=...)`). When it doesn't, the PR merges
with manifest and lock-file changes only.

## Decision

1. **Mine git history, not the API.** A bare partial clone (commits and
   trees; blobs on demand) of each repository gives everything: merge commits
   ("Merge pull request #N from owner/dependabot/...") expose the PR's own
   commits, and bot-authored squash merges carry human `Co-authored-by`
   trailers. No token, no rate limit, the same on a laptop and in CI.
2. **Impact** = a human commit on the bump PR that changed an existing source
   file. Labels are the old-side line ranges of that change, in the
   *snapshot*: the last bot commit before the first human commit (merge
   style) or the squash commit's parent. If the file changed between the
   snapshot and the fix's parent, its lines don't map and only the file
   counts.
3. **No impact** = a PR with bot commits only, that changed no source file,
   and whose package no human commit subject names (nor a revert) in the next
   40 first-parent commits. At most six per repository, picked by commit-hash
   order, so busy repositories don't dominate.
4. **Only bumps that can matter:** majors, or minors below 1.0, of a single
   package that the snapshot imports. A package nobody imports is not a code
   question, and every system would trivially answer it.
5. **Fixes that look generated are dropped:** more than 40 hunks or 300
   changed lines in one file is a rebuilt bundle (`lib/*.js` compiled from
   `src/`) or a reformat, not a change to code that uses the package.
6. **Splits by repository** (40% dev, 60% held-out test, by a hash of the
   repository name). Tuning reads the dev split only; the test split is run
   once, for the final numbers.
7. **Intervals resample repositories** (cluster bootstrap). Cases from one
   repository share code, conventions and maintainers; resampling cases would
   pretend they are independent and give intervals that are too narrow.

## Scoring

- File level: a predicted file is right if the fix changed it.
- Line level: a predicted line is right within two lines of a fixed hunk; a
  hunk counts as found if any predicted line hits it.
- Detection: impact cases with at least one right file.
- False alarm: a no-impact case where the system predicted any line.
- *Strict* counts only what a system calls affected; *lenient* adds the
  places it asks a human to look at (upgrade-impact's "possible" lines). The
  baselines make one kind of prediction, so both scores are the same for them.

## Consequences

- Labels are noisy in known directions. A human commit on the bump branch can
  include unrelated cleanup (false labels), and a fix that landed in a later,
  separate PR leaves a "no impact" case that wasn't (filtered when the commit
  subject names the package, missed otherwise). Neither depends on the system
  being scored, so comparisons between systems stay fair even where absolute
  numbers are off.
- Some fixes touch files that don't import the package (a test helper, a
  config module). No system that reads usages can find those, so file recall
  has a ceiling below 100% for every system, the baselines included.
- Repositories that merge bump PRs by rebase, or fix breakages in separate PRs,
  contribute only no-impact cases. The dataset over-represents projects whose
  maintainers fix on the bot's branch.
- "Green CI" is not checked: the merge itself is the signal. A maintainer who
  merged a red bump and fixed it later is caught only by rule 3's later-commit
  check.
