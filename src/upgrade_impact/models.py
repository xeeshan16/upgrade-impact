"""Data model shared by every stage: bumps, release notes, claims, usages, reports.

Everything that crosses a module boundary (and everything in ``--format json``)
is one of these pydantic models, so the JSON output has a stable, typed shape.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


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
    inferred: bool = Field(
        default=False,
        description="The model gave no evidence; this is a line it flagged that names "
        "the claim's symbol or parameter.",
    )


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
    via: str | None = Field(
        default=None,
        description="How the symbol was resolved when it is not imported directly: "
        "an instance (c = httpx.Client() at line 4) or a local re-export.",
    )


class MatchStrength(StrEnum):
    DIRECT = "direct"
    SYMBOL = "symbol"
    IMPORT = "import"


class Verdict(StrEnum):
    AFFECTED = "affected"
    NOT_AFFECTED = "not_affected"
    UNCLEAR = "unclear"


class Adjudication(BaseModel):
    """The model's reading of one candidate, with both citations checked."""

    verdict: Verdict
    reason: str
    code_line: int | None = None
    code_quote: str = ""
    code_verified: bool = False
    note_line: int | None = None
    note_quote: str = ""
    note_verified: bool = False
    notes_api: str = Field(
        default="", description="The API the model says the notes tie the change to."
    )
    api_supported: bool = Field(
        default=True,
        description="False when the claim names only a parameter and the model called the "
        "line affected without naming, from the shown notes, the API this line calls.",
    )

    @property
    def verified(self) -> bool:
        return self.code_verified and self.note_verified

    @property
    def effective(self) -> Verdict:
        """A verdict only counts when both citations were found (and, for an
        ``affected`` verdict on a parameter-only claim, when the notes name the
        API the line calls); otherwise it is unclear."""
        if not self.verified:
            return Verdict.UNCLEAR
        if self.verdict == Verdict.AFFECTED and not self.api_supported:
            return Verdict.UNCLEAR
        return self.verdict


class Match(BaseModel):
    usage: Usage
    strength: MatchStrength
    reason: str
    adjudication: Adjudication | None = None


class ClaimStatus(StrEnum):
    AFFECTED = "affected"
    POSSIBLE = "possible"
    NOT_FOUND = "not_found"
    NEEDS_REVIEW = "needs_review"


class RuntimeCheck(BaseModel):
    """A runtime-support change compared with the runtime the project declares."""

    runtime: str = Field(description="node or python")
    required: str = Field(description="What the new version needs, e.g. 'node > 18'.")
    declared: str | None = Field(default=None, description="The project's specifier.")
    path: str | None = Field(default=None, description="Manifest the specifier came from.")
    line: int | None = None
    satisfied: bool | None = Field(
        default=None, description="None when the project declares no runtime version."
    )
    reason: str


class ClaimResult(BaseModel):
    claim: Claim
    status: ClaimStatus
    matches: list[Match] = Field(default_factory=list)
    cleared: list[Match] = Field(
        default_factory=list,
        description="Candidates the model judged not affected, with verified citations.",
    )
    runtime: RuntimeCheck | None = None


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
    cached_calls: int = Field(default=0, description="Calls answered from the on-disk cache.")
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_s: float = 0.0


class Accounting(BaseModel):
    """Totals over every model call of one check (all bumps, all stages)."""

    provider: str
    model: str
    calls: int = 0
    cached_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_latency_s: float = Field(
        default=0.0, description="Summed latency of the uncached calls (they may overlap)."
    )
    cost_usd: float | None = Field(
        default=None, description="Estimated cost; null when the model's price is unknown."
    )
    price_basis: str = Field(
        default="unknown", description="override, local, list price or unknown."
    )


class IndexStats(BaseModel):
    files_scanned: int = 0
    files_importing: int = 0
    usages: int = 0
    parse_errors: int = 0
    import_names: list[str] = Field(default_factory=list)


class DismissedLine(BaseModel):
    """A note line that read like a breaking change and was ruled out, with why."""

    line: NoteLine
    by: str = Field(description="model (second reading) or runtime (declared floor)")
    reason: str


class BumpReport(BaseModel):
    """The answer for one bump."""

    bump: Bump
    notes_versions: list[str] = Field(default_factory=list)
    notes_sources: list[str] = Field(default_factory=list)
    notes_lines: int = 0
    claims_proposed: int = 0
    claims_dropped: int = 0
    dropped_claims: list[Claim] = Field(
        default_factory=list,
        description="Claims whose quotes were not found near the cited note lines.",
    )
    results: list[ClaimResult] = Field(default_factory=list)
    uncovered: list[NoteLine] = Field(
        default_factory=list,
        description="Note lines that read like a breaking change but no verified claim cites.",
    )
    dismissed: list[DismissedLine] = Field(
        default_factory=list,
        description="Breaking-looking note lines ruled out on a second reading: the model "
        "said they do not change the package's API (with a verified quote, and naming "
        "nothing this code uses), or the project's declared runtime already meets them.",
    )
    suspicious: list[NoteLine] = Field(
        default_factory=list,
        description="Note lines that read like instructions to a model (prompt injection).",
    )
    index: IndexStats = Field(default_factory=IndexStats)
    model: ModelRun | None = None
    adjudication: ModelRun | None = Field(
        default=None, description="Model calls that read candidate code lines."
    )
    recheck: ModelRun | None = Field(
        default=None, description="Model calls that re-read unclaimed note lines."
    )
    elapsed_s: float = Field(default=0.0, description="Wall-clock time spent on this bump.")
    warnings: list[str] = Field(default_factory=list)

    def count(self, status: ClaimStatus) -> int:
        return sum(1 for r in self.results if r.status == status)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """``affected``, ``review``, ``unknown`` (no release notes) or ``no-impact``."""
        if self.count(ClaimStatus.AFFECTED):
            return "affected"
        if self.review_reasons:
            return "review"
        if not self.notes_lines:
            return "unknown"
        return "no-impact"

    @property
    def review_causes(self) -> dict[str, int]:
        """What keeps the answer at "review" rather than "no impact", counted by cause:
        ``possible`` (claims with unconfirmed matches), ``no_api`` (claims naming no API,
        of which ``runtime`` are about the runtime), ``unclaimed`` (breaking-looking note
        lines no claim covers) and ``suspicious`` (note lines that read like injection)."""
        needs = [r for r in self.results if r.status == ClaimStatus.NEEDS_REVIEW]
        causes = {
            "possible": self.count(ClaimStatus.POSSIBLE),
            "no_api": len(needs),
            "runtime": sum(1 for r in needs if r.runtime is not None),
            "unclaimed": len(self.uncovered),
            "suspicious": len(self.suspicious) if self.notes_lines else 0,
        }
        return {k: v for k, v in causes.items() if v}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def review_reasons(self) -> list[str]:
        """Why the answer is "review" rather than "no impact" (empty otherwise)."""
        causes = self.review_causes
        reasons: list[str] = []
        if n := causes.get("possible"):
            reasons.append(f"{n} claim(s) with possible matches")
        if n := causes.get("no_api"):
            runtime = causes.get("runtime")
            detail = f", {runtime} about the runtime" if runtime else ""
            reasons.append(f"{n} claim(s) naming no API{detail}")
        if n := causes.get("unclaimed"):
            reasons.append(f"{n} unclaimed breaking-looking note line(s)")
        if n := causes.get("suspicious"):
            reasons.append(f"{n} note line(s) read like instructions to a model")
        return reasons


# Most severe first: the order a report lists its bumps in, and how the verdict of a
# multi-package (grouped) PR is formed.
VERDICT_ORDER = ("affected", "review", "unknown", "no-impact")


def verdict_rank(verdict: str) -> int:
    return VERDICT_ORDER.index(verdict) if verdict in VERDICT_ORDER else len(VERDICT_ORDER)


class Report(BaseModel):
    source: str
    repository_root: str
    bumps: list[BumpReport] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    accounting: Accounting | None = Field(
        default=None, description="Model calls, tokens and estimated cost over all bumps."
    )
    elapsed_s: float = Field(default=0.0, description="Wall-clock time of the whole check.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The most severe bump verdict: one affected package makes the PR affected."""
        if not self.bumps:
            return "unknown"
        return min((b.verdict for b in self.bumps), key=verdict_rank)

    def ordered(self) -> list[BumpReport]:
        """Bumps, most severe verdict first (stable within a verdict)."""
        return sorted(self.bumps, key=lambda b: verdict_rank(b.verdict))

    def affected_lines(self) -> int:
        """Distinct code lines in claims whose status is affected."""
        seen = {
            (b.bump.package, m.usage.path, m.usage.line)
            for b in self.bumps
            for r in b.results
            if r.status == ClaimStatus.AFFECTED
            for m in r.matches
        }
        return len(seen)
