"""Optional OpenTelemetry traces and metrics.

Off unless asked for, and a no-op when OpenTelemetry is not installed, so the
core has no hard dependency on it. Turn it on with ``UPGRADE_IMPACT_OTEL=1``
after installing the extra::

    pipx install "upgrade-impact[otel]"
    UPGRADE_IMPACT_OTEL=1 upgrade-impact check https://github.com/o/r/pull/1

With ``OTEL_EXPORTER_OTLP_ENDPOINT`` set, spans and metrics go to that collector
over OTLP/HTTP; otherwise they are printed to stderr. What is recorded:

* one ``check`` span per run, a ``bump`` span per package under it (also when
  packages run on worker threads), and under each bump the stages ``notes``,
  ``index``, ``extract``, ``recheck`` and ``review``, tagged with counts
  (note lines, usages, claims, calls) and the bump's verdict;
* counters ``upgrade_impact.model.calls``, ``upgrade_impact.model.tokens``
  (by direction) and ``upgrade_impact.model.cost_usd``, by provider, model and
  stage; ``upgrade_impact.bumps`` by verdict; and a histogram
  ``upgrade_impact.stage.duration`` in milliseconds.

Release-note text and source code are never put on a span: attributes are
metadata (package, versions, counts, model), never content.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from upgrade_impact.models import BumpReport, Report

log = logging.getLogger(__name__)

SERVICE = "upgrade-impact"
Attr = str | int | float | bool


class Stage:
    """A handle on the current stage; ``set`` adds an attribute (no-op when off)."""

    def set(self, key: str, value: Attr | None) -> None:
        pass


class _SpanStage(Stage):
    def __init__(self, span: Any) -> None:
        self._span = span

    def set(self, key: str, value: Attr | None) -> None:
        if value is not None:
            self._span.set_attribute(key, value)


_NULL_STAGE = Stage()


class Telemetry:
    """The no-op base. Every method is safe to call whether or not OTel is present."""

    enabled = False

    @contextmanager
    def stage(self, name: str, **attributes: Attr | None) -> Iterator[Stage]:
        yield _NULL_STAGE

    def record_bump(self, report: BumpReport) -> None:
        pass

    def record_report(self, report: Report) -> None:
        pass

    def shutdown(self) -> None:
        pass


class OtelTelemetry(Telemetry):
    enabled = True

    def __init__(self, tracer: Any, meter: Any, providers: tuple[Any, ...] = ()) -> None:
        self._tracer = tracer
        self._providers = providers
        self._calls = meter.create_counter(
            "upgrade_impact.model.calls", description="chat model calls (cached ones included)"
        )
        self._tokens = meter.create_counter(
            "upgrade_impact.model.tokens", unit="token", description="chat model tokens"
        )
        self._cost = meter.create_counter(
            "upgrade_impact.model.cost_usd", unit="USD", description="estimated model cost"
        )
        self._bumps = meter.create_counter(
            "upgrade_impact.bumps", description="package bumps checked, by verdict"
        )
        self._stage_ms = meter.create_histogram(
            "upgrade_impact.stage.duration", unit="ms", description="stage latency"
        )

    @contextmanager
    def stage(self, name: str, **attributes: Attr | None) -> Iterator[Stage]:
        started = time.perf_counter()
        with self._tracer.start_as_current_span(name) as span:
            handle = _SpanStage(span)
            for key, value in attributes.items():
                handle.set(key, value)
            try:
                yield handle
            finally:
                elapsed_ms = (time.perf_counter() - started) * 1000
                self._stage_ms.record(elapsed_ms, {"stage": name})

    def record_bump(self, report: BumpReport) -> None:
        self._bumps.add(1, {"verdict": report.verdict, "ecosystem": report.bump.ecosystem.value})
        for stage, run in (
            ("extract", report.model),
            ("recheck", report.recheck),
            ("review", report.adjudication),
        ):
            if run is None:
                continue
            labels = {"provider": run.provider, "model": run.model, "stage": stage}
            self._calls.add(run.calls, labels)
            self._tokens.add(run.usage.input_tokens, {**labels, "direction": "input"})
            self._tokens.add(run.usage.output_tokens, {**labels, "direction": "output"})

    def record_report(self, report: Report) -> None:
        acc = report.accounting
        if acc is not None and acc.cost_usd:
            self._cost.add(acc.cost_usd, {"provider": acc.provider, "model": acc.model})

    def shutdown(self) -> None:
        for provider in self._providers:
            try:
                provider.shutdown()
            except Exception as exc:  # pragma: no cover - best-effort flush
                log.debug("otel shutdown failed", extra={"error": str(exc)})


_NULL = Telemetry()


class _Active:
    telemetry: Telemetry = _NULL


def get_telemetry() -> Telemetry:
    return _Active.telemetry


def set_telemetry(telemetry: Telemetry | None) -> None:
    """Install ``telemetry`` (``None`` restores the no-op). For tests and embedders."""
    _Active.telemetry = telemetry or _NULL


def wants_otel(env: Mapping[str, str]) -> bool:
    return env.get("UPGRADE_IMPACT_OTEL", "").strip().lower() in ("1", "true", "yes", "on")


def configure(env: Mapping[str, str]) -> Telemetry:
    """Set up telemetry from the environment, once; returns the active instance.

    Warns and stays a no-op when the extra is not installed. Safe to call again.
    """
    if _Active.telemetry.enabled or not wants_otel(env):
        return _Active.telemetry
    try:
        _Active.telemetry = _build(env)
    except ImportError:
        log.warning(
            "UPGRADE_IMPACT_OTEL is set but OpenTelemetry is not installed; "
            'install the extra: pipx install "upgrade-impact[otel]"'
        )
    return _Active.telemetry


def _build(env: Mapping[str, str]) -> Telemetry:
    # Imported here: OpenTelemetry is an optional extra.
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    from upgrade_impact import __version__

    resource = Resource.create({"service.name": SERVICE, "service.version": __version__})
    endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter: Any = OTLPSpanExporter()
        metric_exporter: Any = OTLPMetricExporter()
    else:
        # Console output goes to stderr so stdout stays the report (or JSON-RPC for MCP).
        span_exporter = ConsoleSpanExporter(out=sys.stderr)
        metric_exporter = ConsoleMetricExporter(out=sys.stderr)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    log.info("OpenTelemetry enabled", extra={"exporter": "otlp" if endpoint else "console"})
    return OtelTelemetry(
        trace.get_tracer(SERVICE), metrics.get_meter(SERVICE), (tracer_provider, meter_provider)
    )
