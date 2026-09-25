"""Claude Code backend (PRD 9.2). M3 implements chat mode; agentic mode lands in M4.

Chat mode runs `claude -p` on the host with every tool disabled, no MCP servers, no saved
session, and an empty working directory (so no project CLAUDE.md is loaded). The prompt
goes in on stdin. With a JSON schema, Claude Code validates the answer itself and returns
it as `structured_output`; that takes two turns, so `max_turns` defaults to 3.

It runs on the owner's Claude login (the subscription). `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN` are removed from the child environment so the CLI can never
switch to pay-per-token API billing.

`--dangerously-skip-permissions` is never used here; it is allowed only in worker
containers (PRD 19.6), and a unit test checks this module never passes it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from codeit.backends.base import (
    Availability,
    BackendError,
    BackendOutputError,
    BackendUnavailable,
    ChatRequest,
    ChatResult,
    parse_json_text,
)
from codeit.log import get_logger

log = get_logger(__name__)

STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
DEFAULT_PARK = timedelta(hours=5)
_EPOCH = re.compile(r"\|(\d{10})\b")


class ClaudeCodeChat:
    name = "claude_code_chat"
    kind: Literal["chat"] = "chat"

    def __init__(
        self,
        usage_limit_patterns: Sequence[str],
        *,
        binary: str = "claude",
        model: str | None = None,
        max_turns: int = 3,
    ) -> None:
        self.binary = binary
        self.model = model
        self.max_turns = max_turns
        self._patterns = [re.compile(re.escape(p), re.IGNORECASE) for p in usage_limit_patterns]
        self._parked_until: datetime | None = None

    async def available(self) -> Availability:
        if self._parked_until and self._parked_until > datetime.now(UTC):
            return Availability("parked", self._parked_until, "usage limit")
        if not _on_path(self.binary):
            return Availability("disabled", reason=f"{self.binary!r} not found on PATH")
        return Availability("ok")

    def build_args(self, system: str, req: ChatRequest) -> list[str]:
        args = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--max-turns",
            str(self.max_turns),
            "--system-prompt",
            system,
        ]
        if req.json_schema is not None:
            args += ["--json-schema", json.dumps(req.json_schema)]
        if self.model:
            args += ["--model", self.model]
        return args

    async def complete(self, req: ChatRequest) -> ChatResult:
        state = await self.available()
        if state.state != "ok":
            raise BackendUnavailable(f"{self.name}: {state.reason}", until=state.until)

        system = "\n\n".join(m.content for m in req.messages if m.role == "system")
        prompt = _transcript([m for m in req.messages if m.role != "system"])
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        with tempfile.TemporaryDirectory(prefix="codeit-claude-") as cwd:
            proc = await asyncio.create_subprocess_exec(
                *self.build_args(system, req),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), timeout=req.timeout_s
                )
            except TimeoutError as e:
                proc.kill()
                await proc.wait()
                raise BackendError(f"{self.name}: timed out after {req.timeout_s}s") from e
        return self._parse(out.decode(errors="replace"), err.decode(errors="replace"), req)

    def _parse(self, out: str, err: str, req: ChatRequest) -> ChatResult:
        try:
            reply: dict[str, Any] = json.loads(out)
        except json.JSONDecodeError:
            reply = {}
        text = str(reply.get("result") or "")
        failed = not reply or reply.get("is_error") or reply.get("subtype") != "success"
        if failed:
            message = text or err.strip() or out.strip() or "no output"
            if any(p.search(message) for p in self._patterns):
                until = _reset_time(message)
                self._parked_until = until
                log.warning("claude.usage_limited", raw=message[:500], until=until.isoformat())
                raise BackendUnavailable(f"{self.name}: usage limit ({message[:200]})", until=until)
            raise BackendError(f"{self.name}: {message[:500]}")

        data: Any = None
        if req.json_schema is not None:
            data = reply.get("structured_output")
            if data is None:
                try:
                    data = parse_json_text(text)
                except json.JSONDecodeError as e:
                    raise BackendOutputError(f"{self.name}: answer is not JSON", text=text) from e
        usage = reply.get("usage") or {}
        return ChatResult(
            text=text if data is None else json.dumps(data),
            data=data,
            backend=self.name,
            model=_model_name(reply) or self.model,
            input_tokens=_sum(
                usage, "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"
            ),
            output_tokens=usage.get("output_tokens"),
            cost_usd=reply.get("total_cost_usd"),
            cost_estimated=True,
            raw=reply,
        )


def _transcript(messages: list[Any]) -> str:
    """One prompt for `claude -p`. A multi-turn exchange (a retry) is laid out as text."""
    if len(messages) == 1:
        return str(messages[0].content)
    parts = []
    for m in messages:
        label = "Your previous answer" if m.role == "assistant" else "Request"
        parts.append(f"## {label}\n\n{m.content}")
    return "\n\n".join(parts)


def _reset_time(message: str) -> datetime:
    m = _EPOCH.search(message)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), UTC)
    return datetime.now(UTC) + DEFAULT_PARK


def _model_name(reply: dict[str, Any]) -> str | None:
    """The model that wrote the answer: Claude Code may also use a small helper model, so
    pick the one with the most output tokens."""
    usage = reply.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None
    return str(max(usage, key=lambda m: (usage[m] or {}).get("outputTokens", 0)))


def _sum(usage: dict[str, Any], *keys: str) -> int | None:
    values = [usage[k] for k in keys if isinstance(usage.get(k), int)]
    return sum(values) if values else None


def _on_path(binary: str) -> bool:
    import shutil

    return shutil.which(binary) is not None
