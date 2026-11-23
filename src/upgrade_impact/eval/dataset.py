"""The labelled dataset: one JSON line per real, merged dependency-bump PR.

A case says which code a human had to change after the bump (``impact``), or
that the bump merged with manifest and lock-file changes only (``no_impact``).
Labels are old-side line ranges in the *snapshot*: the commit whose code the
tool analyses, before any follow-up fix. Splits are by repository, so no
repository contributes to both the dev and the held-out test split.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from upgrade_impact.models import Bump, Ecosystem

DEV_SHARE = 40  # percent of repositories in the dev split; the rest is held out


class Label(StrEnum):
    IMPACT = "impact"
    NO_IMPACT = "no_impact"


class Split(StrEnum):
    DEV = "dev"
    TEST = "test"


class LabelHunk(BaseModel):
    start: int
    end: int
    pure_insert: bool = False


class LabelFile(BaseModel):
    path: str
    lines_known: bool = Field(
        description="False when the file changed between the snapshot and the fix's "
        "parent, so the fix's line numbers do not apply to the snapshot."
    )
    hunks: list[LabelHunk] = Field(default_factory=list)


class Case(BaseModel):
    id: str
    repo: str
    pr: int | None = None
    url: str
    ecosystem: Ecosystem
    package: str
    from_version: str
    to_version: str
    snapshot: str = Field(description="Commit whose code is analysed (before any fix).")
    label: Label
    files: list[LabelFile] = Field(default_factory=list)
    fix_commits: list[str] = Field(default_factory=list)
    merged_at: str
    merge_style: str = Field(description="merge or squash")
    split: Split
    import_files: int = Field(default=0, description="Files importing the package at the snapshot.")

    @property
    def bump(self) -> Bump:
        return Bump(
            ecosystem=self.ecosystem,
            package=self.package,
            from_version=self.from_version,
            to_version=self.to_version,
            source="dataset",
        )

    @property
    def label_paths(self) -> set[str]:
        return {f.path for f in self.files}


def split_for(repo: str) -> Split:
    digest = hashlib.sha256(repo.lower().encode()).digest()
    return Split.DEV if digest[0] * 100 // 256 < DEV_SHARE else Split.TEST


def load_cases(path: Path) -> list[Case]:
    cases: list[Case] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = raw.strip()
        if not text or text.startswith("//"):
            continue
        try:
            cases.append(Case.model_validate_json(text))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: not a valid case: {exc}") from exc
    return cases


def dump_cases(cases: Iterable[Case], path: Path) -> int:
    ordered = sorted(cases, key=lambda c: (c.repo.lower(), c.merged_at, c.id))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for case in ordered:
            handle.write(json.dumps(case.model_dump(mode="json"), sort_keys=True) + "\n")
    return len(ordered)


def select(cases: Iterable[Case], split: str) -> list[Case]:
    if split == "all":
        return list(cases)
    return [c for c in cases if c.split.value == split]


def sample_per_label(cases: Iterable[Case], per_label: int) -> list[Case]:
    """``per_label`` cases of each label, picked by a hash of the id (stable across runs)."""
    picked: list[Case] = []
    for label in Label:
        pool = sorted(
            (c for c in cases if c.label == label),
            key=lambda c: hashlib.sha256(c.id.encode()).hexdigest(),
        )
        picked.extend(pool[:per_label])
    return picked
