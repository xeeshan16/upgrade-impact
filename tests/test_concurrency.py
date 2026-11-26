"""Parallel package checks, the model-call cap and the time budget."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from upgrade_impact.config import ConfigError, Settings, load_settings
from upgrade_impact.models import (
    Bump,
    BumpReport,
    Ecosystem,
    NotesSection,
    ReleaseNotes,
    TokenUsage,
)
from upgrade_impact.pipeline import Deadline, Target, check_bump, run_check
from upgrade_impact.providers.base import Completion, Message
from upgrade_impact.providers.bounded import BoundedChat
from upgrade_impact.providers.fake import ScriptedChat


class SlowChat:
    """Records the most calls ever in flight at once."""

    provider = "fake"
    model = "slow"

    def __init__(self, delay: float = 0.02) -> None:
        self._delay = delay
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        json_mode: bool = False,
        json_schema: Mapping[str, Any] | None = None,
    ) -> Completion:
        with self._lock:
            self.in_flight += 1
            self.calls += 1
            self.peak = max(self.peak, self.in_flight)
        time.sleep(self._delay)
        with self._lock:
            self.in_flight -= 1
        return Completion('{"claims": []}', self.model, TokenUsage(), 0.0, "stop")


def _hammer(chat: Any, threads: int) -> None:
    workers = [
        threading.Thread(target=lambda: chat.complete([Message("user", "hi")]))
        for _ in range(threads)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def test_bounded_chat_caps_calls_in_flight() -> None:
    inner = SlowChat()
    _hammer(BoundedChat(inner, 2), 8)
    assert inner.calls == 8
    assert inner.peak == 2


def test_bounded_chat_passes_identity_through() -> None:
    bounded = BoundedChat(SlowChat(), 1)
    assert (bounded.provider, bounded.model, bounded.limit) == ("fake", "slow", 1)
    with pytest.raises(ValueError, match="at least 1"):
        BoundedChat(SlowChat(), 0)


def test_settings_for_jobs_and_budget() -> None:
    env = {"UPGRADE_IMPACT_JOBS": "8", "UPGRADE_IMPACT_MAX_SECONDS": "90"}
    settings = load_settings(env=env, cwd=Path("/nonexistent"))
    assert (settings.jobs, settings.model_concurrency, settings.max_seconds) == (8, 1, 90.0)
    with pytest.raises(ConfigError, match="jobs must be between 1 and 32"):
        load_settings(env={"UPGRADE_IMPACT_JOBS": "0"}, cwd=Path("/nonexistent"))
    with pytest.raises(ConfigError, match="max_seconds"):
        load_settings(env={"UPGRADE_IMPACT_MAX_SECONDS": "-1"}, cwd=Path("/nonexistent"))


def _ticks(*values: float) -> Iterator[float]:
    yield from values
    while True:
        yield values[-1]


def test_deadline() -> None:
    assert not Deadline().expired()
    assert not Deadline.after(0).expired()  # 0 = no budget
    clock = _ticks(100.0, 105.0, 111.0)
    deadline = Deadline.after(10, clock=lambda: next(clock))
    assert deadline.at == 110.0
    assert not deadline.expired()
    assert deadline.expired()


BUMP = Bump(
    ecosystem=Ecosystem.PYPI, package="httpx", from_version="0.27", to_version="0.28", source="x"
)
NOTES = ReleaseNotes.from_sections(
    "httpx",
    [
        NotesSection(
            version="0.28.0",
            source="changelog",
            lines=["* The deprecated `proxies` argument has now been removed."],
        )
    ],
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


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(
        "import httpx\n\nclient = httpx.Client(timeout=5)\n", encoding="utf-8"
    )
    return tmp_path


def _check(chat: ScriptedChat, root: Path, deadline: Deadline) -> BumpReport:
    return check_bump(
        BUMP,
        target=Target(source="test", bumps=[BUMP]),
        code_root=root,
        chat=chat,
        registry=None,  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        settings=Settings(llm_cache="off"),
        import_names=["httpx"],
        notes=NOTES,
        deadline=deadline,
    )


def test_spent_budget_skips_the_model_and_holds_review(tmp_path: Path) -> None:
    clock = _ticks(0.0, 5.0)
    chat = ScriptedChat([])
    report = _check(chat, _repo(tmp_path), Deadline.after(1, clock=lambda: next(clock)))
    assert chat.requests == []
    assert report.model is None
    assert report.skipped == ["extract"]
    assert report.verdict == "review"
    assert report.review_causes["budget"] == 1
    assert "time budget ran out before: extract" in report.review_reasons
    assert any("time budget of 1 s ran out before the extract stage" in w for w in report.warnings)


def test_budget_spent_after_extraction_skips_later_stages(tmp_path: Path) -> None:
    # Fresh at the extract check, spent by the second-reading check.
    clock = _ticks(0.0, 0.0, 50.0)
    chat = ScriptedChat([EXTRACTION])
    report = _check(chat, _repo(tmp_path), Deadline.after(10, clock=lambda: next(clock)))
    assert len(chat.requests) == 1
    assert report.model is not None
    assert report.skipped == ["recheck", "review"]
    # The claim's parameter is not used here, but the skipped stages keep it from
    # being "no impact".
    assert report.verdict == "review"


class _Registry:
    pass


def test_parallel_checks_keep_order_and_cap_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bumps = [
        Bump(ecosystem=Ecosystem.PYPI, package=f"pkg{i}", to_version="2", source="flags")
        for i in range(6)
    ]
    seen_threads: set[str] = set()
    progress: list[int] = []

    def fake_check(bump: Bump, *, chat: Any, **_: Any) -> BumpReport:
        seen_threads.add(threading.current_thread().name)
        chat.complete([Message("user", bump.package)])
        return BumpReport(bump=bump)

    monkeypatch.setattr("upgrade_impact.pipeline.check_bump", fake_check)
    inner = SlowChat()
    report = run_check(
        Target(source="t", bumps=bumps),
        chat=inner,
        registry=_Registry(),  # type: ignore[arg-type]
        github=None,  # type: ignore[arg-type]
        settings=Settings(jobs=4, model_concurrency=2),
        repo=tmp_path,
        env={},
        on_bump=lambda i, total, bump: progress.append(i),
    )
    assert [b.bump.package for b in report.bumps] == [b.package for b in bumps]
    assert inner.calls == 6
    assert inner.peak == 2
    assert len(seen_threads) > 1
    assert progress == list(range(6))
    assert all(b.elapsed_s > 0 for b in report.bumps)


def test_a_failing_package_fails_the_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bumps = [
        Bump(ecosystem=Ecosystem.PYPI, package=f"pkg{i}", to_version="2", source="flags")
        for i in range(3)
    ]

    def fake_check(bump: Bump, **_: Any) -> BumpReport:
        if bump.package == "pkg1":
            raise RuntimeError("boom")
        return BumpReport(bump=bump)

    monkeypatch.setattr("upgrade_impact.pipeline.check_bump", fake_check)
    with pytest.raises(RuntimeError, match="boom"):
        run_check(
            Target(source="t", bumps=bumps),
            chat=None,
            registry=_Registry(),  # type: ignore[arg-type]
            github=None,  # type: ignore[arg-type]
            settings=Settings(jobs=3),
            repo=tmp_path,
            env={},
        )
