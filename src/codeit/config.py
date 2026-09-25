"""Configuration loading and validation.

Two sources:
- `config/config.yaml`: non-secret settings, validated by `Config` (PRD 20.1).
- `.env` / environment: credentials, loaded by `Secrets` (PRD 20.2).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config/config.yaml")

Role = Literal["planner", "coder", "reviewer", "rebase", "docs", "learning", "ops"]

# Backends that are not model lists in `models:` but can still be routed to.
BUILTIN_BACKENDS = frozenset({"claude_code", "claude_code_chat"})

_HHMM = r"([01]\d|2[0-3]):[0-5]\d"
_TIME_RE = re.compile(rf"^{_HHMM}$")
_WINDOW_RE = re.compile(rf"^{_HHMM}-{_HHMM}$")


def _role_ints(**values: int) -> dict[Role, int]:
    """Typed default factory helper for per-role integer maps."""
    return values  # type: ignore[return-value]


class ConfigError(Exception):
    """Raised when the config file is missing, unparsable or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TargetRepo(_Strict):
    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]+$")]
    url: str
    default_branch: str = "main"


class ProjectConfig(_Strict):
    jira_project_key: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{1,9}$")]
    target_repo: TargetRepo


class OrchestratorConfig(_Strict):
    poll_seconds: PositiveInt = 45
    max_review_loops: PositiveInt = 3
    lease_ttl_minutes: dict[Role, PositiveInt] = Field(
        default_factory=lambda: _role_ints(coder=90, reviewer=40, rebase=30)
    )


class Route(_Strict):
    primary: str
    fallback: str | None = None
    fallback_requires_label: str | None = None


class ClaudeConfig(_Strict):
    run_window: list[str] = Field(default_factory=list)
    max_concurrent_runs: PositiveInt = 1
    max_turns: dict[Role, PositiveInt] = Field(default_factory=dict)
    usage_limit_patterns: list[str] = Field(default_factory=list)

    @field_validator("run_window")
    @classmethod
    def _check_windows(cls, v: list[str]) -> list[str]:
        for w in v:
            if not _WINDOW_RE.match(w):
                raise ValueError(f"run_window entry {w!r} must look like 'HH:MM-HH:MM'")
        return v


class OpenRouterConfig(_Strict):
    daily_usd_cap: dict[Role, NonNegativeFloat] = Field(default_factory=dict)
    free_requests_daily_cap: Annotated[int, Field(ge=0, le=1000)] = 900


class SandboxConfig(_Strict):
    # Docker image references must be lowercase.
    image: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?$")]
    cpus: Annotated[float, Field(gt=0)] = 2
    mem: Annotated[str, Field(pattern=r"^\d+[kmg]$")] = "4g"
    pids_limit: PositiveInt = 512
    timeouts_minutes: dict[Role, PositiveInt] = Field(
        default_factory=lambda: _role_ints(coder=60, reviewer=30, rebase=20)
    )
    egress_allowlist: list[str] = Field(default_factory=list)


class RebaseConfig(_Strict):
    poll_minutes: PositiveInt = 10
    max_files: PositiveInt = 5
    never_auto: list[str] = Field(
        default_factory=lambda: ["**/migrations/**", "package-lock.json", ".github/**"]
    )


class DocsConfig(_Strict):
    run_at: str = "07:00"

    @field_validator("run_at")
    @classmethod
    def _check_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f"run_at {v!r} must look like 'HH:MM'")
        return v


class LearningConfig(_Strict):
    cron: str = "0 22 * * SUN"
    min_new_signals: PositiveInt = 10

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        if len(v.split()) != 5:
            raise ValueError(f"cron {v!r} must have 5 fields")
        return v


class EvalConfig(_Strict):
    regression_tolerance: Annotated[float, Field(ge=0, le=1)] = 0.05


class Config(_Strict):
    data_dir: Path = Path("data")
    project: ProjectConfig
    orchestrator: OrchestratorConfig = OrchestratorConfig()
    slots: dict[Role, NonNegativeInt] = Field(
        default_factory=lambda: _role_ints(
            planner=1, coder=1, reviewer=2, rebase=1, docs=1, learning=1
        )
    )
    routing: dict[Role, Route]
    models: dict[str, list[str] | str]
    claude: ClaudeConfig = ClaudeConfig()
    openrouter: OpenRouterConfig = OpenRouterConfig()
    sandbox: SandboxConfig
    rebase: RebaseConfig = RebaseConfig()
    docs: DocsConfig = DocsConfig()
    learning: LearningConfig = LearningConfig()
    eval: EvalConfig = EvalConfig()

    @model_validator(mode="after")
    def _check_routes(self) -> Config:
        known = BUILTIN_BACKENDS | set(self.models)
        for role, route in self.routing.items():
            for target in (route.primary, route.fallback):
                if target is not None and target not in known:
                    raise ValueError(
                        f"routing.{role} refers to unknown backend {target!r}; "
                        f"expected one of {sorted(known)}"
                    )
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "codeit.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


class Secrets(BaseSettings):
    """Credentials from `.env` or the process environment. All optional at load time;
    each feature checks for the secrets it needs."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: SecretStr | None = None
    github_token_agent: SecretStr | None = None
    github_token_readonly: SecretStr | None = None
    claude_code_oauth_token: SecretStr | None = None
    openrouter_key_reviewer: SecretStr | None = None
    openrouter_key_ops: SecretStr | None = None
    openrouter_key_coder: SecretStr | None = None
    codeit_api_token: SecretStr | None = None


def _format_validation_error(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        lines.append(f"  {loc}: {e['msg']}")
    return "\n".join(lines)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Read and validate a config file. Raises `ConfigError` with a readable message."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{path} is invalid:\n{_format_validation_error(e)}") from e
