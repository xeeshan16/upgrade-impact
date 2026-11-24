# ADR 0004: Earning "no impact": runtime floor, a guarded second reading, injection checks

Status: accepted (day 4, 2026-11-24)

## Context

A "no impact" answer is the most useful thing the tool can say (the bump can be
merged without reading anything) and the most dangerous one (a wrong one ships
a break). Day 1 made it hard to get: any release-note line that *looks*
breaking (`removed`, `no longer`, `breaking`...) and that no verified claim
cites holds the bump at "review". That safety net exists because small models
skip lines; silence must not read as "nothing changed".

The first real-model eval on the dev sample (day 3, qwen2.5:7b, 4 impact and 4
clean bumps) showed the cost: every clean bump was "review". Reading the notes
of those bumps, the lines that held them were:

- section headings (`### ⚠ BREAKING CHANGES`);
- repository housekeeping (`Remove old template`, `Remove outdated info from
  README`);
- runtime drops the project had already made (`Drop support for Node.js v18`
  in `nestjs/nest`, whose `package.json` requires Node `>= 20`);
- real API removals of things the project never calls
  (`Remove sinon.defaultConfig`).

A tool that always says "review" is import-grep with extra steps.

## Decision

Keep the safety net, but let a line leave it only through a check that can
fail safe:

1. **Headings** are not changes; they are skipped by the filter.
2. **Runtime lines** are compared with the floor the project declares
   (`engines.node`, `requires-python`, Poetry's `python`, `python_requires`),
   deterministically and at the precision the note uses ("Node 18" means every
   18.x). A floor that already meets the requirement dismisses the line (or
   clears a runtime claim) and cites the manifest line; a lower or missing
   floor keeps it at review, now with the reason.
3. **A second reading** sends the remaining unclaimed lines to the model in a
   short prompt (20 lines per call, one line of context each), asking per line
   whether it changes the public API, defaults or runtime support. Each answer
   must quote its line, and the quote is checked.
   - "Yes" becomes an ordinary claim: verified, matched, reviewed.
   - "No" dismisses the line, **unless the line names an identifier this
     repository uses from the package** (symbol parts and keyword names from
     the usage index). The model can rule out `Remove sinon.defaultConfig` in
     a project that only calls `sinon.stub`, but not `Removed useFakeTimers
     shorthand` in one that passes `useFakeTimers`.
   - No answer, an unverifiable quote, a failed call: the line stays.
4. **Injection-like lines** (instructions to a model, fence markers, the
   tool's JSON keys) are reported, can never be dismissed, and keep the bump
   from "no impact". They remove nothing: claims cited to them are still
   matched, so an injection cannot hide a change by being noticed.

Every dismissal is listed in the report with who made it (declared runtime or
second reading) and why, and `UPGRADE_IMPACT_RECHECK=off` turns the second
reading off.

## Consequences

- "No impact" now needs either no breaking-looking lines, or every such line
  claimed (and not matched), dismissed by the runtime check, or dismissed by a
  model answer that names nothing the code uses. The guard is one-sided: it
  can only refuse a dismissal.
- The second reading costs one extra call per 20 unclaimed lines, and only
  when there are unclaimed lines. The runtime check costs nothing.
- The reachability guard is lexical. A dismissed line that describes a change
  to something the code uses *without naming it* ("connections are now closed
  on error") gets through if the model says it is internal. The eval's false
  "no impact" rate on impact cases is the number that watches this.
- The injection patterns are a tripwire, not a filter: they catch the obvious
  phrasings, and the fence plus "data, never instructions" remains the main
  defence. A false positive costs a "review" instead of a "no impact", never
  a missed change.

## Alternatives considered

- **Drop the safety net when the model returned claims.** Simple, but it
  trusts silence again, which day 2 showed small models produce.
- **Ask the model to re-extract the whole notes with a stronger prompt.**
  Costs as much as the first pass and fails the same way on long chunks; the
  focused prompt is short enough for a 7B model to answer line by line.
- **Dismiss lines whose identifiers the code never uses, without a model.**
  Deterministic, but most housekeeping lines name nothing at all, so it
  would clear little; it is kept as the guard instead of the decision.
