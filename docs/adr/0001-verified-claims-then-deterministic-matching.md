# ADR 0001: The model extracts cited claims; code matching is deterministic

- Status: accepted
- Date: 2026-11-21

## Context

A bump PR raises one question: does anything in the release notes between the
two versions break *this* code? Answering it takes two different skills:

1. reading prose release notes ("The deprecated `proxies` argument has now
   been removed", "JSON request bodies use a compact representation") and
   turning them into precise statements about API symbols and parameters;
2. finding every place the code uses those symbols, including through
   aliases (`from httpx import AsyncClient as AC`) and keyword arguments.

The first is what language models are good at and grep is useless for. The
second is what a parser is good at and a model is bad at: it is exhaustive,
cheap and exact, while a model sees only what fits in its context and can
invent file paths. Small local models (the default runs on Ollama) also
hallucinate details, so nothing they say can be trusted unchecked.

## Decision

- The model's only job on day 1 is **extraction**: from numbered note lines,
  list breaking or behaviour-changing claims with `kind`, `symbols`,
  `parameters` and `evidence` (line number plus a verbatim quote). The JSON
  schema is sent for constrained decoding where the provider supports it, with
  `evidence` first so the model commits to a quote before summarising.
- Every claim is **verified** against the notes: at least one quote must be
  found (markup- and case-insensitive) on the cited line or across it and the
  next line. Unverified claims are dropped and counted. Symbols or parameters
  the notes never mention are moved to `unverified_symbols` and never drive
  matching.
- Code usage is found by a **parser** (Python `ast` on day 1; tree-sitter for
  JS/TS on day 2): imports with aliases, calls with their keyword names,
  attribute chains and bare names, each resolved to a qualified symbol.
- **Matching is deterministic**: a parameter claim matches only calls that pass
  that keyword (or forward `**kwargs`), so `AsyncClient(timeout=10)` is
  confidently *not* affected by "`proxies` was removed". Removed or renamed
  symbols match any use; behaviour changes on a used symbol are "possible";
  claims with no symbol are "needs review" if the package is used at all.
- Day 2 adds a second model step that **adjudicates** "possible" and
  "needs review" candidates with their code context, again with verified
  citations on both sides.

## Consequences

- Output is auditable: every line in a report points at a note line and a code
  line that a reviewer can open, and both are checked by code, not trust.
- "No impact" has a reason ("no call passes `proxies`") instead of a vibe.
- A model that misreads the notes produces at worst a wrong-but-cited claim,
  never an invented file. A model that misses a claim is a false negative;
  the eval (day 3) measures that rate against real PRs.
- Method calls on instances (`client.get(...)`) are invisible to a parser
  without type inference; day 2 adds attribute-name candidates for the
  adjudicator. This is the main known gap on day 1.
