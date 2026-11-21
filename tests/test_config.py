from __future__ import annotations

from pathlib import Path

import pytest

from upgrade_impact.config import ConfigError, Settings, load_settings


def test_precedence_file_env_flags(tmp_path: Path) -> None:
    (tmp_path / ".upgrade-impact.toml").write_text(
        'provider = "ollama"\nmodel = "from-file"\ntimeout_s = 30\nnotes_budget_tokens = 1500\n'
    )
    settings = load_settings(
        env={"UPGRADE_IMPACT_MODEL": "from-env", "UPGRADE_IMPACT_MAX_RETRIES": "1"},
        cwd=tmp_path,
        overrides={"model": "from-flag", "provider": None},
    )
    assert settings.provider == "ollama"
    assert settings.model == "from-flag"
    assert settings.timeout_s == 30.0
    assert settings.max_retries == 1
    assert settings.notes_budget_tokens == 1500


def test_explicit_config_path(tmp_path: Path) -> None:
    path = tmp_path / "custom.toml"
    path.write_text('provider = "fake"\n')
    assert load_settings(env={"UPGRADE_IMPACT_CONFIG": str(path)}).provider == "fake"
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings(env={"UPGRADE_IMPACT_CONFIG": str(tmp_path / "missing.toml")})


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("provider = [", "custom.toml"),
        ('colour = "blue"', "unknown setting"),
        ('provider = "gpt"', "unknown provider"),
        ('timeout_s = "soon"', "must be a number"),
        ("timeout_s = 0", "positive"),
        ("max_retries = 11", "max_retries"),
        ("repair_attempts = 4", "repair_attempts"),
        ("notes_budget_tokens = 100", "notes_budget_tokens"),
        ("max_output_tokens = 10", "max_output_tokens"),
    ],
)
def test_invalid_settings(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "custom.toml"
    path.write_text(content + "\n")
    with pytest.raises(ConfigError, match=message):
        load_settings(env={"UPGRADE_IMPACT_CONFIG": str(path)})


def test_defaults_are_valid() -> None:
    assert Settings().validate().provider == "auto"
    assert load_settings(env={}, cwd=Path("/nonexistent")).notes_budget_tokens == 3000
