"""Release notes are untrusted input. Find the lines that talk to the model.

Anyone who can publish a release, edit a CHANGELOG or (with Renovate and
Dependabot) write the PR body controls text the model reads. The prompts
already fence that text with a content-derived marker (see
:func:`.extract.fence_nonce`) and say it is data. This module adds a
deterministic check on top: note lines that read like instructions to a
model, or that try to close a fence or speak the tool's JSON, are reported.

What a flagged line changes:

* the report lists it and warns;
* the second reading (:mod:`.recheck`) may not dismiss it;
* the bump can no longer be "no impact" (a clean answer is exactly what an
  injected note would ask for); it becomes "review", with the reason.

A flagged line never *removes* anything: claims cited to it are still
matched against the code, so an injection cannot hide a real change by
getting itself noticed.
"""

from __future__ import annotations

import re

from upgrade_impact.models import ReleaseNotes

_PATTERNS = (
    # "ignore all previous instructions", "disregard the above rules"
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|every\s+)?(?:of\s+)?(?:the\s+|your\s+)?"
    r"(?:previous|prior|above|earlier|preceding|system|original)\s+"
    r"(?:instructions?|prompts?|messages?|rules|context|directions)",
    # "you are now a helpful assistant", "as an AI language model"
    r"\byou are (?:now )?(?:an? |the )?(?:\w+\s+){0,2}?"
    r"(?:ai|assistant|language model|llm|chatbot)\b",
    r"\b(?:new|updated|real|hidden|secret)\s+(?:system\s+)?instructions?\b",
    r"\b(?:reveal|print|show|repeat)\s+(?:your|the)\s+system\s+prompt\b",
    r"^\W*(?:assistant|ai)\s*:\s",
    # "respond only with", "answer with no impact", "report that there is no impact"
    # (at the start of a sentence, or with "only": "close reply with a 10 second
    # timeout" in uvicorn's notes is not an instruction)
    r"\b(?:respond|reply|answer|output)\s+only\s+with\b",
    r"(?:^|[.:;!]\s)[\W_]*(?:please\s+)?(?:respond|reply|answer)\s+with\b",
    r"\b(?:report|say|state|conclude|output|return)\s+(?:that\s+)?(?:there\s+is\s+)?"
    r"[\"'`]?(?:no[ -]impact|not[ _]affected|nothing to (?:change|fix))",
    # fence markers and the tool's own JSON keys
    r"<<<\s*(?:end\s+)?(?:notes|code|file)\b",
    r"[\"'](?:claims|breaking_lines|decisions|breaks_users|verdict)[\"']\s*:",
)
INJECTION = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.I)


def suspicious_lines(notes: ReleaseNotes) -> list[int]:
    """Numbers of the note lines that read like instructions to a model."""
    return [line.n for line in notes.lines if INJECTION.search(line.text)]
