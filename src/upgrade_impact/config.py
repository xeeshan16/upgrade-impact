"""Settings: defaults < config file < environment < command-line flags.

Config file: ``.upgrade-impact.toml`` in the working directory, or the path in
``UPGRADE_IMPACT_CONFIG``. Keys mirror the environment variables without the
``UPGRADE_IMPACT_`` prefix, lower-cased (``provider``, ``model``, ``base_url``,
``timeout_s``, ``max_retries``, ``max_output_tokens``, ``repair_attempts``,
``notes_budget_tokens``, ``max_candidates``, ``llm_cache``, ``cache_dir``).
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
    # "on" caches model replies on disk (temperature 0 makes them reproducible).
    llm_cache: str = "on"
    cache_dir: str | None = None
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
        if not 0 <= self.max_retries <= 10:
            raise ConfigError("max_retries must be between 0 and 10")
        if not 0 <= self.repair_attempts <= 3:
            raise ConfigError("repair_attempts must be between 0 and 3")
        if self.notes_budget_tokens < 500:
            raise ConfigError("notes_budget_tokens must be at least 500")
        if self.max_output_tokens < 256:
            raise ConfigError("max_output_tokens must be at least 256")
        if not 0 <= self.max_candidates <= 200:
            raise ConfigError("max_candidates must be between 0 and 200")
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
    "llm_cache": "UPGRADE_IMPACT_LLM_CACHE",
    "cache_dir": "UPGRADE_IMPACT_CACHE_DIR",
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
