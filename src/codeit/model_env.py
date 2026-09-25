"""Model choices from the environment (ADR-0008).

`config.yaml` holds default model lists under `models:`. The owner switches models for
price or testing in `.env`, without editing config:

- `OPENROUTER_MODELS_<NAME>` overrides `models.openrouter_<name>`, comma-separated and
  tried in order. For example, `OPENROUTER_MODELS_PAID_REVIEW=a/model,b/model`.
- `OPENCODE_MODEL` overrides `models.opencode_paid`.
- `CLAUDE_MODEL` picks the Claude model for every Claude Code run (chat and agentic).
  Unset, Claude Code uses its own default.

The process environment wins over `.env`. A variable naming a list that does not exist
is an error, so a typo cannot silently fall back to the defaults.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from codeit.config import Config, ConfigError

PREFIX = "OPENROUTER_MODELS_"
OPENCODE_VAR = "OPENCODE_MODEL"


def read_env(env_file: Path = Path(".env")) -> dict[str, str]:
    file_values = dotenv_values(env_file) if env_file.is_file() else {}
    merged = {k: v for k, v in file_values.items() if v}
    merged.update({k: v for k, v in os.environ.items() if v})
    return merged


def env_var_for(list_name: str) -> str | None:
    if list_name == "opencode_paid":
        return OPENCODE_VAR
    if list_name.startswith("openrouter_"):
        return PREFIX + list_name.removeprefix("openrouter_").upper()
    return None


def effective_models(
    cfg: Config, env: Mapping[str, str] | None = None
) -> dict[str, list[str] | str]:
    """`cfg.models` with environment overrides applied."""
    env = read_env() if env is None else env
    models: dict[str, list[str] | str] = dict(cfg.models)
    for var, value in env.items():
        if var == OPENCODE_VAR:
            models["opencode_paid"] = value.strip()
        elif var.startswith(PREFIX):
            name = "openrouter_" + var.removeprefix(PREFIX).lower()
            if name not in cfg.models:
                known = sorted(v for n in cfg.models if (v := env_var_for(n)))
                raise ConfigError(f"{var} matches no model list; expected one of {known}")
            chosen = [m.strip() for m in value.split(",") if m.strip()]
            if not chosen:
                raise ConfigError(f"{var} is set but lists no models")
            models[name] = chosen
    return models


def claude_model(env: Mapping[str, str] | None = None) -> str | None:
    env = read_env() if env is None else env
    return env.get("CLAUDE_MODEL") or None
