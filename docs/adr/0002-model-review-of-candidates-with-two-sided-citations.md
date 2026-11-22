# ADR 0002: The model reviews uncertain matches, and only verified verdicts count

- Status: accepted
- Date: 2026-11-22

## Context

ADR 0001 kept the model out of the decision "which lines are affected": it
extracts cited claims, and deterministic matching finds the code. Day 1's
real-model runs showed where that stops working. httpx 0.28 says "the
deprecated `app` argument has now been removed" without naming the API. The
matcher cannot tell `AsyncClient(app=app)` (affected) from
`ASGITransport(app=app)` (the replacement), so on `cereja-project/govbr_auth#53`
it reported 36 "possible" matches, all false alarms. A tool that cries wolf on
every bump gets ignored. Claims that name no symbol at all ("JSON bodies are
now compact") were worse: every import site became "needs review".

Telling those apart takes reading the code with the change in mind, which is
what a model is for. The risk is the usual one: a model that confidently says
"not affected" about a line that is, and the tool says "no impact".

## Decision

Add a review step after matching:

1. Only uncertain candidates go to the model: `possible` matches (parameter
   named without an API, `**kwargs` forwarding, behaviour changes on a used
   symbol) and, for claims that name nothing, the calls into the package
   ranked by word overlap with the change. `direct` matches are never sent;
   the model can only argue the tool out of a true positive there.
2. The prompt carries the claim, its cited note lines plus two neighbours on
   each side (same version), and a numbered ±6-line window of code per
   candidate, all inside fences derived from the fenced content.
3. Each verdict (`affected`, `not_affected`, `unclear`) must cite a code line
   with a quote and a note line with a quote. The code quote must be on the
   cited line (one line of slack, re-pointed to the real line); the note quote
   must be on a note line the model was shown.
4. A verdict whose citations don't check out counts as `unclear`. So an
   unverified "not affected" never removes a line, and an unverified
   "affected" never promotes one.
5. For a claim that names no symbol, an `affected` verdict must also name, in
   `notes_api`, what the notes say changed; that name must appear in the note
   lines shown and be what the line uses: for a parameter claim, the API the
   line calls; otherwise the API or an option it passes. The package name
   does not count. Otherwise the verdict is `unclear`. This rule came from the
   first real runs: qwen2.5:7b called all six `httpx.ASGITransport(app=...)`
   lines it read on govbr_auth "affected" because the parameter name matched,
   and qwen2.5:3b turned "the 0.28 release includes a limited set of
   deprecations" into a claim and then called ordinary httpx calls affected.
6. Cleared lines move to `cleared` in the report with the model's reason, not
   into the void, so a reviewer can disagree.
7. A claim that names nothing is only cleared to "not used" when every call
   into the package was read and cleared; if the candidate cap cut the list,
   it stays "needs review".

## Consequences

- The answer for parameter-only and behaviour claims now depends on the
  model, so its quality must be measured: day 3's eval reports precision and
  recall with and without review, against the deterministic matcher alone.
- Cost grows with the number of uncertain candidates (batched six per call,
  capped at 12 per claim by default). Replies are cached on disk keyed by the
  full request, so re-runs and eval re-scoring are free.
- Small models may answer `unclear` often; that leaves the day 1 behaviour in
  place rather than making things worse.
- The fake provider's rule for review never clears a candidate, so CI cannot
  accidentally prove the clearing path works; that is covered by scripted
  tests and real-model runs.
