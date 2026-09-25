from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeit.cli import app
from codeit.config import ConfigError, load_config
from codeit.model_env import effective_models, env_var_for, read_env
from tests.conftest import REPO_ROOT

CFG = load_config(REPO_ROOT / "config" / "config.yaml")


def test_defaults_without_overrides() -> None:
    assert effective_models(CFG, {}) == CFG.models


def test_overrides() -> None:
    models = effective_models(
        CFG,
        {
            "OPENROUTER_MODELS_PAID_REVIEW": " a/one , b/two,",
            "OPENCODE_MODEL": "openrouter/x/coder",
            "UNRELATED": "ignored",
        },
    )
    assert models["openrouter_paid_review"] == ["a/one", "b/two"]
    assert models["opencode_paid"] == "openrouter/x/coder"
    assert models["openrouter_free"] == CFG.models["openrouter_free"]


def test_unknown_list_is_an_error() -> None:
    with pytest.raises(ConfigError, match="OPENROUTER_MODELS_PAID_REVIEW"):
        effective_models(CFG, {"OPENROUTER_MODELS_PAID_REVEIW": "a/b"})


def test_empty_list_is_an_error() -> None:
    with pytest.raises(ConfigError, match="lists no models"):
        effective_models(CFG, {"OPENROUTER_MODELS_FREE": " , "})


def test_env_var_names() -> None:
    assert env_var_for("openrouter_paid_cheap") == "OPENROUTER_MODELS_PAID_CHEAP"
    assert env_var_for("opencode_paid") == "OPENCODE_MODEL"
    assert env_var_for("something_else") is None


def test_process_env_wins_over_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_MODELS_FREE=from/file\nEMPTY=\n")
    monkeypatch.setenv("OPENROUTER_MODELS_FREE", "from/process")
    env = read_env(env_file)
    assert env["OPENROUTER_MODELS_FREE"] == "from/process"
    assert "EMPTY" not in env
    assert read_env(tmp_path / "missing")["OPENROUTER_MODELS_FREE"] == "from/process"


def test_config_validate_shows_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_MODELS_PAID_REVIEW=v/review-model\n")
    for var in ("OPENROUTER_API_KEY", "OPENROUTER_KEY_REVIEWER", "OPENROUTER_KEY_OPS"):
        monkeypatch.delenv(var, raising=False)
    result = CliRunner().invoke(
        app, ["config", "validate", "-c", str(REPO_ROOT / "config" / "config.yaml")]
    )
    assert result.exit_code == 0, result.output
    assert (
        "openrouter_paid_review (OPENROUTER_MODELS_PAID_REVIEW, from .env): v/review-model"
        in result.output
    )
    assert "openrouter_free (OPENROUTER_MODELS_FREE, default)" in result.output
    assert "no OpenRouter key" in result.output

    (tmp_path / ".env").write_text("OPENROUTER_MODELS_TYPO=x\n")
    result = CliRunner().invoke(
        app, ["config", "validate", "-c", str(REPO_ROOT / "config" / "config.yaml")]
    )
    assert result.exit_code == 1
