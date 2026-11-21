"""Data model shared by every stage: bumps, release notes, claims, usages, reports.

Everything that crosses a module boundary (and everything in ``--format json``)
is one of these pydantic models, so the JSON output has a stable, typed shape.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Ecosystem(StrEnum):
    PYPI = "pypi"
    NPM = "npm"


class Bump(BaseModel):
    """One dependency moving from one version to another."""

    ecosystem: Ecosystem
    package: str
    from_version: str | None = None
    to_version: str
    manifest: str | None = Field(
        default=None, description="Manifest file the bump was read from, if any."
    )
    source: str = Field(description="How the bump was found: title, manifest-diff or flags.")

    @property
    def label(self) -> str:
        start = self.from_version or "?"
        return f"{self.package} {start} -> {self.to_version} ({self.ecosystem.value})"


class NoteLine(BaseModel):
    """One numbered line of the release notes shown to the model."""

    n: int
    text: str
    version: str


class NotesSection(BaseModel):
    """Release notes for one version, from one source."""

    version: str
    source: str = Field(description="github-release, changelog or pr-body")
    url: str | None = None
    lines: list[str]


class ReleaseNotes(BaseModel):
    """The notes between two versions, with globally numbered lines."""

    package: str
    repository: str | None = None
    sections: list[NotesSection] = Field(default_factory=list)
    lines: list[NoteLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_sections(
        cls,
        package: str,
        sections: list[NotesSection],
        *,
        repository: str | None = None,
        warnings: list[str] | None = None,
    ) -> ReleaseNotes:
        lines: list[NoteLine] = []
        for section in sections:
            for text in section.lines:
                lines.append(NoteLine(n=len(lines) + 1, text=text, version=section.version))
        return cls(
            package=package,
            repository=repository,
            sections=sections,
            lines=lines,
            warnings=list(warnings or []),
        )

    def line(self, n: int) -> NoteLine | None:
        if 1 <= n <= len(self.lines):
            return self.lines[n - 1]
        return None

    @property
    def versions(self) -> list[str]:
        return [s.version for s in self.sections]


class ClaimKind(StrEnum):
    REMOVED = "removed"
    RENAMED = "renamed"
    SIGNATURE_CHANGED = "signature_changed"
    BEHAVIOR_CHANGED = "behavior_changed"
    DEFAULT_CHANGED = "default_changed"
    TYPE_CHANGED = "type_changed"
    DROPPED_SUPPORT = "dropped_support"
    DEPRECATED = "deprecated"
    OTHER = "other"


class Evidence(BaseModel):
    line: int
    quote: str
    verified: bool = False


class Claim(BaseModel):
    """A breaking (or risky) change stated in the release notes."""

    id: str
    kind: ClaimKind
    summary: str
    symbols: list[str] = Field(default_factory=list)
    parameters: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    version: str | None = None
    verified: bool = False
    unverified_symbols: list[str] = Field(
        default_factory=list,
        description="Symbols the model named that appear nowhere in the release notes.",
    )


class UsageKind(StrEnum):
    IMPORT = "import"
    CALL = "call"
    ATTRIBUTE = "attribute"
    NAME = "name"


class Usage(BaseModel):
    """A place in the repository that uses the upgraded package."""

    path: str
    line: int
    column: int
    symbol: str = Field(description="Fully qualified, e.g. httpx.AsyncClient")
    kind: UsageKind
    keywords: list[str] = Field(default_factory=list)
    snippet: str


class MatchStrength(StrEnum):
    DIRECT = "direct"
    SYMBOL = "symbol"
    IMPORT = "import"


class Match(BaseModel):
    usage: Usage
    strength: MatchStrength
    reason: str


class ClaimStatus(StrEnum):
    AFFECTED = "affected"
    POSSIBLE = "possible"
    NOT_FOUND = "not_found"
    NEEDS_REVIEW = "needs_review"


class ClaimResult(BaseModel):
    claim: Claim
    status: ClaimStatus
    matches: list[Match] = Field(default_factory=list)


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class ModelRun(BaseModel):
    provider: str
    model: str
    calls: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_s: float = 0.0


class IndexStats(BaseModel):
    files_scanned: int = 0
    files_importing: int = 0
    usages: int = 0
    parse_errors: int = 0
    import_names: list[str] = Field(default_factory=list)


class BumpReport(BaseModel):
    """The answer for one bump."""

    bump: Bump
    notes_versions: list[str] = Field(default_factory=list)
    notes_sources: list[str] = Field(default_factory=list)
    notes_lines: int = 0
    claims_proposed: int = 0
    claims_dropped: int = 0
    results: list[ClaimResult] = Field(default_factory=list)
    uncovered: list[NoteLine] = Field(
        default_factory=list,
        description="Note lines that read like a breaking change but no verified claim cites.",
    )
    index: IndexStats = Field(default_factory=IndexStats)
    model: ModelRun | None = None
    warnings: list[str] = Field(default_factory=list)

    def count(self, status: ClaimStatus) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def verdict(self) -> str:
        if self.count(ClaimStatus.AFFECTED):
            return "affected"
        if (
            self.count(ClaimStatus.POSSIBLE)
            or self.count(ClaimStatus.NEEDS_REVIEW)
            or self.uncovered
        ):
            return "review"
        if not self.notes_lines:
            return "unknown"
        return "no-impact"


class Report(BaseModel):
    source: str
    repository_root: str
    bumps: list[BumpReport] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
