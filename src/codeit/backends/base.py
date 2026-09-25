"""Backend interface shared by all model adapters (PRD 9.1).

M3 needs chat backends only; the agentic side (`run_agentic`) lands with the sandbox in M4.
"""

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
