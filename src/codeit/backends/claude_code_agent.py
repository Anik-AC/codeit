"""Claude Code agentic backend: `claude -p` inside a worker container (PRD 9.2).

This is the container adapter, the only place the permission-bypass flag appears. Its
commands run only through an `Executor`, which execs into a worker container; nothing
here starts a process on the host.

- Output is `--output-format stream-json --verbose`. Each line is appended to
  `data/transcripts/{run_id}.jsonl` as it arrives and passed to `on_event` (the event bus
  from M7 on).
- The final `result` event decides the status: completed, max_turns, usage_limited or
  error. A wall-clock timeout gives `timeout`.
- Usage limits: Claude Code emits `rate_limit_event`s with a status and an exact reset
  time. A run that fails while the status is not `allowed`, or whose error matches
  `claude.usage_limit_patterns`, parks the backend until the reset time, in memory and,
  with a `BackendStateStore`, in the database. The last event is kept on the result
  (utilization feeds the budget guard in M7).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from codeit.backends.base import AgenticRequest, AgenticResult, AgenticStatus, Availability
from codeit.backends.claude_code import UsageLimits, model_name
from codeit.backends.state import BackendStateStore
from codeit.log import get_logger
from codeit.sandbox.containers import ExecResult

log = get_logger(__name__)

SKIP_PERMISSIONS = "--dangerously-skip-permissions"

EventSink = Callable[[dict[str, Any]], None]


class Executor(Protocol):
    """Runs a command inside a worker container, yielding stdout lines."""

    def exec_lines(
        self,
        container_id: str,
        argv: list[str],
        *,
        stdin: str,
        workdir: str,
        timeout_s: float,
        result: ExecResult,
    ) -> AsyncIterator[str]: ...


class ClaudeCodeAgent:
    name = "claude_code"
    kind: Literal["agentic"] = "agentic"

    def __init__(
        self,
        usage_limit_patterns: Sequence[str],
        executor: Executor,
        transcripts_dir: Path,
        *,
        state: BackendStateStore | None = None,
        on_event: EventSink | None = None,
    ) -> None:
        self._limits = UsageLimits(usage_limit_patterns)
        self._executor = executor
        self._transcripts = transcripts_dir
        self._state = state
        self._on_event = on_event
        self._parked_until: datetime | None = None

    async def available(self) -> Availability:
        now = datetime.now(UTC)
        if self._parked_until and self._parked_until > now:
            return Availability("parked", self._parked_until, "usage limit")
        if self._state is not None:
            return self._state.get(self.name, now)
        return Availability("ok")

    @staticmethod
    def build_args(req: AgenticRequest) -> list[str]:
        args = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(req.max_turns),
            "--tools",
            ",".join(req.tools),
            "--no-session-persistence",
            SKIP_PERMISSIONS,
            "--strict-mcp-config",
        ]
        if req.mcp_config_path:
            args += ["--mcp-config", req.mcp_config_path]
        if req.allowed_tools:
            args += ["--allowedTools", ",".join(req.allowed_tools)]
        if req.system_append:
            args += ["--append-system-prompt", req.system_append]
        if req.model:
            args += ["--model", req.model]
        return args

    def _park(self, until: datetime, message: str) -> None:
        self._parked_until = until
        if self._state is not None:
            self._state.park(self.name, until, message)
        log.warning("claude.usage_limited", raw=message[:500], until=until.isoformat())

    async def run_agentic(self, req: AgenticRequest) -> AgenticResult:
        self._transcripts.mkdir(parents=True, exist_ok=True)
        transcript = self._transcripts / f"{req.run_id}.jsonl"
        state = await self.available()
        if state.state != "ok":
            return AgenticResult(
                status="usage_limited" if state.state == "parked" else "error",
                final_message=f"{self.name} is {state.state}: {state.reason}",
                transcript_path=str(transcript),
                reset_at=state.until,
            )

        args = self.build_args(req)
        final: dict[str, Any] | None = None
        rate_limit: dict[str, Any] | None = None
        exec_result = ExecResult()
        with transcript.open("a", encoding="utf-8") as out:
            request_event = {"type": "codeit_request", "argv": args, "prompt": req.prompt}
            out.write(json.dumps(request_event) + "\n")
            async for line in self._executor.exec_lines(
                req.container_id,
                args,
                stdin=req.prompt,
                workdir=req.workdir,
                timeout_s=req.timeout_s,
                result=exec_result,
            ):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = {"type": "raw", "text": line}
                if not isinstance(event, dict):
                    event = {"type": "raw", "text": line}
                out.write(json.dumps(event) + "\n")
                out.flush()
                if self._on_event is not None:
                    self._on_event(event)
                if event.get("type") == "result":
                    final = event
                elif event.get("type") == "rate_limit_event":
                    info = event.get("rate_limit_info")
                    rate_limit = info if isinstance(info, dict) else rate_limit
        result = self._result(final, exec_result, str(transcript), rate_limit)
        return replace(result, rate_limit=rate_limit)

    def _result(
        self,
        final: dict[str, Any] | None,
        exec_result: ExecResult,
        transcript: str,
        rate_limit: dict[str, Any] | None,
    ) -> AgenticResult:
        if exec_result.timed_out:
            return AgenticResult("timeout", "wall-clock timeout", transcript)
        if final is None:
            message = exec_result.stderr.strip() or f"exited {exec_result.exit_code} with no result"
            return self._failure(message, transcript, rate_limit)

        text = str(final.get("result") or "")
        usage = final.get("usage") or {}
        fields: dict[str, Any] = {
            "final_message": text,
            "transcript_path": transcript,
            "turns": final.get("num_turns"),
            "input_tokens": _tokens_in(usage),
            "output_tokens": usage.get("output_tokens"),
            "cost_usd": final.get("total_cost_usd"),
            "model": model_name(final),
        }
        subtype = final.get("subtype")
        if subtype == "success" and not final.get("is_error"):
            return AgenticResult(status="completed", **fields)
        if subtype == "error_max_turns":
            return AgenticResult(status="max_turns", **fields)
        failure = self._failure(text or str(subtype), transcript, rate_limit)
        return AgenticResult(
            status=failure.status,
            reset_at=failure.reset_at,
            **{**fields, "final_message": failure.final_message},
        )

    def _failure(
        self, message: str, transcript: str, rate_limit: dict[str, Any] | None
    ) -> AgenticResult:
        status: AgenticStatus = "error"
        reset_at = None
        limited = rate_limit is not None and rate_limit.get("status") not in (None, "allowed")
        if limited or self._limits.matches(message):
            status = "usage_limited"
            resets = (rate_limit or {}).get("resetsAt")
            reset_at = (
                datetime.fromtimestamp(int(resets), UTC)
                if isinstance(resets, int | float)
                else self._limits.reset_time(message)
            )
            self._park(reset_at, message)
        return AgenticResult(status, message[:2000], transcript, reset_at=reset_at)


def _tokens_in(usage: dict[str, Any]) -> int | None:
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    values = [usage[k] for k in keys if isinstance(usage.get(k), int)]
    return sum(values) if values else None
