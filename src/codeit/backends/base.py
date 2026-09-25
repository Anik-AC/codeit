"""Backend interface shared by all model adapters (PRD 9.1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ChatRequest:
    messages: list[ChatMessage]
    # When set, the backend asks for JSON matching this schema and returns it parsed.
    json_schema: dict[str, Any] | None = None
    schema_name: str = "output"
    timeout_s: float = 600


@dataclass(frozen=True)
class ChatResult:
    text: str
    data: Any
    backend: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    # Claude Code reports an API-equivalent cost; the subscription is not billed per call.
    cost_estimated: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Availability:
    state: Literal["ok", "parked", "disabled"]
    until: datetime | None = None
    reason: str = ""


AgenticStatus = Literal["completed", "max_turns", "timeout", "usage_limited", "error"]


@dataclass(frozen=True)
class AgenticRequest:
    """One agent run inside a worker container (PRD 9.1)."""

    prompt: str
    run_id: str
    container_id: str
    workdir: str = "/workspace"
    system_append: str = ""
    max_turns: int = 80
    tools: list[str] = field(
        default_factory=lambda: ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]
    )
    allowed_tools: list[str] = field(default_factory=list)  # e.g. mcp__codeit-jira__get_ticket
    mcp_config_path: str | None = None  # a path inside the container
    timeout_s: float = 3600
    model: str | None = None


@dataclass(frozen=True)
class AgenticResult:
    status: AgenticStatus
    final_message: str
    transcript_path: str
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    reset_at: datetime | None = None
    model: str | None = None
    # The last `rate_limit_event` Claude Code reported: status, resetsAt, utilization.
    rate_limit: dict[str, Any] | None = None


class BackendError(Exception):
    """A backend call failed for a reason a retry on the same backend will not fix."""


class BackendUnavailable(BackendError):
    """The backend cannot serve now (usage limit, missing key, no model left). Try the
    fallback."""

    def __init__(self, message: str, *, until: datetime | None = None) -> None:
        super().__init__(message)
        self.until = until


class BackendOutputError(BackendError):
    """The model answered, but not with the JSON that was asked for. `text` is its answer."""

    def __init__(self, message: str, *, text: str) -> None:
        super().__init__(message)
        self.text = text


class ChatBackend(Protocol):
    name: str
    kind: Literal["chat"]

    async def available(self) -> Availability: ...

    async def complete(self, req: ChatRequest) -> ChatResult: ...


_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)


def parse_json_text(text: str) -> Any:
    """Parse a model's JSON answer, tolerating a surrounding code fence or chatter."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if 0 <= start < end:
            return json.loads(stripped[start : end + 1])
        raise
