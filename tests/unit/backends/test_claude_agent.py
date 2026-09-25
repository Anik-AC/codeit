from __future__ import annotations

import inspect
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import codeit.backends.claude_code_agent as agent_module
from codeit import db
from codeit.backends.base import AgenticRequest
from codeit.backends.claude_code_agent import SKIP_PERMISSIONS, ClaudeCodeAgent
from codeit.backends.state import BackendStateStore
from codeit.sandbox.containers import ExecResult

PATTERNS = ["usage limit", "limit reached"]


class FakeExecutor:
    def __init__(self, lines: list[str], *, timed_out: bool = False, stderr: str = "") -> None:
        self.lines = lines
        self.timed_out = timed_out
        self.stderr = stderr
        self.calls: list[dict[str, Any]] = []

    async def exec_lines(
        self,
        container_id: str,
        argv: list[str],
        *,
        stdin: str,
        workdir: str,
        timeout_s: float,
        result: ExecResult,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {"container": container_id, "argv": argv, "stdin": stdin, "workdir": workdir}
        )
        for line in self.lines:
            yield line
        result.timed_out = self.timed_out
        result.stderr = self.stderr
        result.exit_code = None if self.timed_out else 0


def ev(**fields: Any) -> str:
    return json.dumps(fields)


def result_event(**overrides: Any) -> str:
    fields: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "num_turns": 4,
        "total_cost_usd": 0.3,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 20},
        "modelUsage": {"claude-sonnet-5": {"outputTokens": 20}},
    }
    fields.update(overrides)
    return json.dumps(fields)


def rate_event(status: str = "allowed", resets: int | None = None) -> str:
    return ev(
        type="rate_limit_event",
        rate_limit_info={"status": status, "resetsAt": resets or int(time.time()) + 3600},
    )


def req(**kw: Any) -> AgenticRequest:
    base: dict[str, Any] = {"prompt": "do the thing", "run_id": "R1", "container_id": "c1"}
    base.update(kw)
    return AgenticRequest(**base)


def make(tmp_path: Path, executor: FakeExecutor, **kw: Any) -> ClaudeCodeAgent:
    return ClaudeCodeAgent(PATTERNS, executor, tmp_path / "transcripts", **kw)


async def test_completed_run_streams_and_saves_transcript(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    executor = FakeExecutor(
        [ev(type="system", subtype="init"), rate_event(), "not json", "", "[1]", result_event()]
    )
    result = await make(tmp_path, executor, on_event=events.append).run_agentic(req())
    assert result.status == "completed"
    assert (result.final_message, result.turns, result.cost_usd) == ("done", 4, 0.3)
    assert (result.input_tokens, result.output_tokens, result.model) == (100, 20, "claude-sonnet-5")
    assert result.rate_limit is not None and result.rate_limit["status"] == "allowed"
    lines = [json.loads(x) for x in Path(result.transcript_path).read_text().splitlines()]
    assert [x["type"] for x in lines] == [
        "codeit_request",
        "system",
        "rate_limit_event",
        "raw",
        "raw",
        "result",
    ]
    assert lines[0]["prompt"] == "do the thing"
    assert [e["type"] for e in events] == ["system", "rate_limit_event", "raw", "raw", "result"]
    call = executor.calls[0]
    assert (call["container"], call["stdin"], call["workdir"]) == (
        "c1",
        "do the thing",
        "/workspace",
    )


def test_build_args() -> None:
    args = ClaudeCodeAgent.build_args(
        req(
            max_turns=7,
            tools=["Read", "Bash"],
            allowed_tools=["mcp__codeit-jira__get_ticket"],
            mcp_config_path="/run/codeit/mcp.json",
            system_append="Follow CLAUDE.md",
            model="claude-opus-5-5",
        )
    )
    assert args[:6] == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
    ]
    assert args[args.index("--max-turns") + 1] == "7"
    assert args[args.index("--tools") + 1] == "Read,Bash"
    assert SKIP_PERMISSIONS in args and "--strict-mcp-config" in args
    assert args[args.index("--mcp-config") + 1] == "/run/codeit/mcp.json"
    assert args[args.index("--allowedTools") + 1] == "mcp__codeit-jira__get_ticket"
    assert args[args.index("--append-system-prompt") + 1] == "Follow CLAUDE.md"
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    bare = ClaudeCodeAgent.build_args(req())
    assert "--mcp-config" not in bare and "--allowedTools" not in bare and "--model" not in bare


def test_agent_module_never_starts_host_processes() -> None:
    """PRD 19.6: the permission bypass reaches only a container, through the Executor."""
    source = inspect.getsource(agent_module)
    for forbidden in ("subprocess", "create_subprocess", "os.system", "os.exec"):
        assert forbidden not in source


async def test_max_turns(tmp_path: Path) -> None:
    executor = FakeExecutor([result_event(subtype="error_max_turns", is_error=True)])
    assert (await make(tmp_path, executor).run_agentic(req())).status == "max_turns"


async def test_error(tmp_path: Path) -> None:
    executor = FakeExecutor(
        [result_event(subtype="error_during_execution", is_error=True, result="boom")]
    )
    result = await make(tmp_path, executor).run_agentic(req())
    assert (result.status, result.final_message) == ("error", "boom")


async def test_timeout(tmp_path: Path) -> None:
    result = await make(tmp_path, FakeExecutor([ev(type="system")], timed_out=True)).run_agentic(
        req()
    )
    assert result.status == "timeout"


async def test_no_result_event(tmp_path: Path) -> None:
    result = await make(tmp_path, FakeExecutor([], stderr="OCI runtime error")).run_agentic(req())
    assert (result.status, result.final_message) == ("error", "OCI runtime error")


async def test_rate_limit_event_parks_with_exact_reset(tmp_path: Path) -> None:
    reset = int(time.time()) + 7200
    executor = FakeExecutor(
        [
            rate_event("rejected", reset),
            result_event(subtype="error", is_error=True, result="API Error"),
        ]
    )
    backend = make(tmp_path, executor)
    result = await backend.run_agentic(req())
    assert result.status == "usage_limited"
    assert result.reset_at == datetime.fromtimestamp(reset, UTC)
    assert (await backend.available()).state == "parked"
    parked = await backend.run_agentic(req(run_id="R2"))  # does not run while parked
    assert parked.status == "usage_limited" and len(executor.calls) == 1


async def test_text_pattern_parks_and_persists(tmp_path: Path) -> None:
    db.upgrade(tmp_path / "c.db")
    store = BackendStateStore(db.make_engine(tmp_path / "c.db"))
    executor = FakeExecutor(
        [result_event(subtype="error", is_error=True, result="5-hour limit reached")]
    )
    result = await make(tmp_path, executor, state=store).run_agentic(req())
    assert result.status == "usage_limited" and result.reset_at is not None
    # a fresh instance (e.g. after a restart) still sees the park
    fresh = make(tmp_path, FakeExecutor([]), state=store)
    assert (await fresh.available()).state == "parked"


async def test_usage_limit_in_stderr_without_result(tmp_path: Path) -> None:
    result = await make(
        tmp_path, FakeExecutor([], stderr="Claude AI usage limit reached")
    ).run_agentic(req())
    assert result.status == "usage_limited"


def test_state_store(tmp_path: Path) -> None:
    db.upgrade(tmp_path / "c.db")
    store = BackendStateStore(db.make_engine(tmp_path / "c.db"))
    now = datetime.now(UTC)
    assert store.get("claude_code").state == "ok"
    store.park("claude_code", now + timedelta(hours=1), "limit")
    got = store.get("claude_code")
    assert (got.state, got.reason) == ("parked", "limit")
    assert store.get("claude_code", now + timedelta(hours=2)).state == "ok"  # reset passed
    store.clear("claude_code")
    assert store.get("claude_code").state == "ok"
