"""Settings: defaults < config file < environment < command-line flags.

Config file: ``.upgrade-impact.toml`` in the working directory, or the path in
``UPGRADE_IMPACT_CONFIG``. Keys mirror the environment variables without the
``UPGRADE_IMPACT_`` prefix, lower-cased (``provider``, ``model``, ``base_url``,
``timeout_s``, ``max_retries``, ``max_output_tokens``, ``repair_attempts``,
``notes_budget_tokens``, ``max_candidates``, ``max_review_calls``, ``recheck``, ``llm_cache``,
``cache_dir``, ``jobs``, ``model_concurrency``, ``max_seconds``).
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

PROVIDERS = ("auto", "ollama", "openai", "anthropic", "azure", "openai-compatible", "fake")

DEFAULT_MODELS = {
    "ollama": "qwen2.5:7b",
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-haiku-4-5",
}

ENV_PREFIX = "UPGRADE_IMPACT_"


class ConfigError(ValueError):
    pass


# Inclusive bounds for the integer settings.
_RANGES = {
    "max_retries": (0, 10),
    "repair_attempts": (0, 3),
    "max_candidates": (0, 200),
    "max_review_calls": (0, 500),
    "jobs": (1, 32),
    "model_concurrency": (1, 32),
}


@dataclass(frozen=True, slots=True)
class Settings:
    provider: str = "auto"
    model: str | None = None
    base_url: str | None = None
    timeout_s: float = 300.0
    max_retries: int = 3
    max_output_tokens: int = 2048
    repair_attempts: int = 1
    # Release notes are split into chunks of about this many tokens; each chunk
    # is one extraction call. Small local models do better with short chunks.
    notes_budget_tokens: int = 3000
    # Most code candidates per claim that the model reads in context.
    max_candidates: int = 12
    # Most uncached review calls per bump (each reads up to six candidates).
    max_review_calls: int = 8
    # "on" re-reads breaking-looking note lines no claim covers, one focused
    # call per 20 lines (see upgrade_impact.recheck); "off" leaves them for a human.
    recheck: str = "on"
    # "on" caches model replies on disk (temperature 0 makes them reproducible).
    llm_cache: str = "on"
    cache_dir: str | None = None
    # Packages of a grouped PR checked at once (fetching notes and indexing code).
    jobs: int = 4
    # Model calls in flight at once across those packages. Ollama serves one
    # request at a time by default (OLLAMA_NUM_PARALLEL), so 1 avoids queueing
    # timeouts there; hosted APIs take more.
    model_concurrency: int = 1
    # Wall-clock budget for one check, in seconds (0 = none). When it runs out,
    # model stages not yet started are skipped and their bumps say "review".
    max_seconds: float = 0.0
    github_api_url: str = "https://api.github.com"
    github_raw_url: str = "https://raw.githubusercontent.com"
    pypi_url: str = "https://pypi.org"
    npm_registry_url: str = "https://registry.npmjs.org"

    def validate(self) -> Settings:
        if self.provider not in PROVIDERS:
            raise ConfigError(
                f"unknown provider {self.provider!r}; use one of {', '.join(PROVIDERS)}"
            )
        if self.timeout_s <= 0:
            raise ConfigError("timeout_s must be positive")
        for name, (low, high) in _RANGES.items():
            value = getattr(self, name)
            if not low <= value <= high:
                raise ConfigError(f"{name} must be between {low} and {high}")
        if self.notes_budget_tokens < 500:
            raise ConfigError("notes_budget_tokens must be at least 500")
        if self.max_output_tokens < 256:
            raise ConfigError("max_output_tokens must be at least 256")
        if self.recheck not in ("on", "off"):
            raise ConfigError("recheck must be 'on' or 'off'")
        if self.max_seconds < 0:
            raise ConfigError("max_seconds must be 0 (no budget) or positive")
        if self.llm_cache not in ("on", "off"):
            raise ConfigError("llm_cache must be 'on' or 'off'")
        return self


_ENV_KEYS = {
    "provider": "UPGRADE_IMPACT_PROVIDER",
    "model": "UPGRADE_IMPACT_MODEL",
    "base_url": "UPGRADE_IMPACT_BASE_URL",
    "timeout_s": "UPGRADE_IMPACT_TIMEOUT_S",
    "max_retries": "UPGRADE_IMPACT_MAX_RETRIES",
    "max_output_tokens": "UPGRADE_IMPACT_MAX_OUTPUT_TOKENS",
    "repair_attempts": "UPGRADE_IMPACT_REPAIR_ATTEMPTS",
    "notes_budget_tokens": "UPGRADE_IMPACT_NOTES_BUDGET_TOKENS",
    "max_candidates": "UPGRADE_IMPACT_MAX_CANDIDATES",
    "max_review_calls": "UPGRADE_IMPACT_MAX_REVIEW_CALLS",
    "recheck": "UPGRADE_IMPACT_RECHECK",
    "llm_cache": "UPGRADE_IMPACT_LLM_CACHE",
    "cache_dir": "UPGRADE_IMPACT_CACHE_DIR",
    "jobs": "UPGRADE_IMPACT_JOBS",
    "model_concurrency": "UPGRADE_IMPACT_MODEL_CONCURRENCY",
    "max_seconds": "UPGRADE_IMPACT_MAX_SECONDS",
    "github_api_url": "GITHUB_API_URL",
}


def _coerce(name: str, value: Any) -> Any:
    types = {f.name: f.type for f in fields(Settings)}
    kind = str(types[name])
    try:
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    return str(value) if value is not None else None


def _from_mapping(base: Settings, values: Mapping[str, Any], origin: str) -> Settings:
    known = {f.name for f in fields(Settings)}
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"{origin}: unknown setting(s) {', '.join(sorted(unknown))}")
    return replace(base, **{k: _coerce(k, v) for k, v in values.items()})


def load_settings(
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    env = os.environ if env is None else env
    settings = Settings()

    config_path = env.get("UPGRADE_IMPACT_CONFIG")
    path = Path(config_path) if config_path else (cwd or Path.cwd()) / ".upgrade-impact.toml"
    if config_path and not path.is_file():
        raise ConfigError(f"UPGRADE_IMPACT_CONFIG points to {path}, which does not exist")
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        settings = _from_mapping(settings, data, str(path))

    from_env = {name: env[key] for name, key in _ENV_KEYS.items() if env.get(key)}
    settings = _from_mapping(settings, from_env, "environment")

    if overrides:
        settings = _from_mapping(
            settings, {k: v for k, v in overrides.items() if v is not None}, "flags"
        )
    return settings.validate()
