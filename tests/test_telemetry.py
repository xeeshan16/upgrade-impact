"""OpenTelemetry spans and metrics, read back from the SDK's in-memory exporters."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from upgrade_impact import telemetry
from upgrade_impact.config import Settings
from upgrade_impact.models import (
    Bump,
    BumpReport,
    Ecosystem,
    ModelRun,
    NotesSection,
    ReleaseNotes,
    TokenUsage,
)
from upgrade_impact.pipeline import Target, check_bump, run_check
from upgrade_impact.providers.fake import ScriptedChat


class Captured:
    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.metrics = InMemoryMetricReader()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        meter_provider = MeterProvider(metric_readers=[self.metrics])
        self.telemetry = telemetry.OtelTelemetry(
            tracer_provider.get_tracer("test"), meter_provider.get_meter("test")
        )

    def finished(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def named(self, name: str) -> list[ReadableSpan]:
        return [s for s in self.finished() if s.name == name]

    def metric_points(self, name: str) -> list[tuple[dict[str, Any], Any]]:
        data = self.metrics.get_metrics_data()
        assert data is not None
        out = []
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name:
                        for point in metric.data.data_points:
                            value = getattr(point, "value", None)
                            if value is None:
                                value = getattr(point, "count", None)
                            out.append((dict(point.attributes or {}), value))
        return out


@pytest.fixture
def captured() -> Iterator[Captured]:
    cap = Captured()
    telemetry.set_telemetry(cap.telemetry)
    yield cap
    telemetry.set_telemetry(None)


BUMP = Bump(
    ecosystem=Ecosystem.PYPI, package="httpx", from_version="0.27", to_version="0.28", source="x"
)
SECRET_NOTE = "* The deprecated `proxies` argument has now been removed."
NOTES = ReleaseNotes.from_sections(
    "httpx", [NotesSection(version="0.28.0", source="changelog", lines=[SECRET_NOTE])]
)
EXTRACTION = json.dumps(
    {
        "breaking_lines": [1],
        "claims": [
            {
                "evidence": [{"line": 1, "quote": "`proxies` argument has now been removed"}],
                "summary": "Client(proxies=) was removed",
                "kind": "removed",
                "symbols": [],
                "parameters": ["proxies"],
            }
        ],
    }
)


def test_check_bump_records_a_span_per_stage(captured: Captured, tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "import httpx\n\nclient = httpx.Client(proxies=None)\n", encoding="utf-8"
    )
    chat = ScriptedChat([EXTRACTION, json.dumps({"decisions": []})])
    report = check_bump(
        BUMP,
        target=Target(source="t", bumps=[BUMP]),
        code_root=tmp_path,
        chat=chat,
        registry=None,  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        settings=Settings(llm_cache="off", recheck="off"),
        import_names=["httpx"],
        notes=NOTES,
    )
    names = [s.name for s in captured.finished()]
    assert names == ["notes", "index", "extract", "review"]
    [notes] = captured.named("notes")
    assert notes.attributes is not None
    assert (notes.attributes["note_lines"], notes.attributes["cached"]) == (1, True)
    [index] = captured.named("index")
    assert index.attributes is not None
    usages = index.attributes["usages"]
    assert isinstance(usages, int)
    assert usages >= 1
    [extract] = captured.named("extract")
    assert extract.attributes is not None
    assert extract.attributes["model"] == "scripted"
    assert (extract.attributes["calls"], extract.attributes["claims"]) == (1, 1)
    assert report.results
    # Metadata only: no release-note text and no code on any span.
    for span in captured.finished():
        for value in (span.attributes or {}).values():
            assert "proxies" not in str(value)
    durations = captured.metric_points("upgrade_impact.stage.duration")
    assert {attrs["stage"] for attrs, _ in durations} >= {"notes", "index", "extract"}


def test_parallel_bumps_nest_under_the_check_span(
    captured: Captured, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bumps = [
        Bump(ecosystem=Ecosystem.PYPI, package=f"pkg{i}", to_version="2", source="flags")
        for i in range(3)
    ]

    def fake_check(bump: Bump, **_: Any) -> BumpReport:
        with telemetry.get_telemetry().stage("notes"):
            pass
        report = BumpReport(bump=bump, notes_lines=1)
        report.model = ModelRun(
            provider="openai",
            model="gpt-4.1-mini",
            calls=2,
            usage=TokenUsage(input_tokens=1000, output_tokens=100),
        )
        return report

    monkeypatch.setattr("upgrade_impact.pipeline.check_bump", fake_check)
    report = run_check(
        Target(source="t", bumps=bumps),
        chat=None,
        registry=None,  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        settings=Settings(jobs=3),
        repo=tmp_path,
        env={},
    )
    [check] = captured.named("check")
    bump_spans = captured.named("bump")
    assert len(bump_spans) == 3
    assert check.context is not None
    for span in bump_spans:
        assert span.parent is not None
        assert span.parent.span_id == check.context.span_id
        assert span.attributes is not None
        assert span.attributes["verdict"] == "no-impact"
    bump_ids = {s.context.span_id for s in bump_spans if s.context is not None}
    for span in captured.named("notes"):
        assert span.parent is not None
        assert span.parent.span_id in bump_ids
    assert check.attributes is not None
    assert check.attributes["model_calls"] == 6
    assert check.attributes["cost_usd"] == pytest.approx(report.accounting.cost_usd)  # type: ignore[union-attr]

    calls = captured.metric_points("upgrade_impact.model.calls")
    assert sum(v for _, v in calls) == 6
    tokens = {a["direction"]: v for a, v in captured.metric_points("upgrade_impact.model.tokens")}
    assert tokens == {"input": 3000, "output": 300}
    assert captured.metric_points("upgrade_impact.bumps") == [
        ({"verdict": "no-impact", "ecosystem": "pypi"}, 3)
    ]
    [(labels, cost)] = captured.metric_points("upgrade_impact.model.cost_usd")
    assert labels == {"provider": "openai", "model": "gpt-4.1-mini"}
    assert cost == pytest.approx(3000 * 0.40 / 1e6 + 300 * 1.60 / 1e6)


def test_off_by_default_and_noop() -> None:
    telemetry.set_telemetry(None)
    assert telemetry.configure({}) is telemetry.get_telemetry()
    assert not telemetry.get_telemetry().enabled
    with telemetry.get_telemetry().stage("x", a=1) as stage:
        stage.set("b", 2)


def test_configure_from_the_environment(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        active = telemetry.configure({"UPGRADE_IMPACT_OTEL": "1"})
        assert active.enabled
        assert telemetry.configure({"UPGRADE_IMPACT_OTEL": "1"}) is active
        with active.stage("check", bumps=1):
            pass
        active.shutdown()
    finally:
        telemetry.set_telemetry(None)
    out, err = capsys.readouterr()
    # Console exporters write to stderr, never into the report on stdout.
    assert out == ""
    assert '"name": "check"' in err
