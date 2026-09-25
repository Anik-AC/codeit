from __future__ import annotations

import inspect
import json
import stat
import time
from pathlib import Path
from typing import Any

import pytest

import codeit.backends.claude_code as claude_module
from codeit.backends.base import (
    BackendError,
    BackendOutputError,
    BackendUnavailable,
    ChatMessage,
    ChatRequest,
)
from codeit.backends.claude_code import ClaudeCodeChat

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}
PATTERNS = ["usage limit", "limit reached"]


@pytest.fixture
def fake_claude(tmp_path: Path) -> tuple[Path, Path]:
    """An executable that records argv, stdin, cwd and env, then prints `reply.json`."""
    log = tmp_path / "call.json"
    script = tmp_path / "claude"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, os, sys, time
reply = open({str(tmp_path / "reply.txt")!r}).read()
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
            "env": dict(os.environ)}}, open({str(log)!r}, "w"))
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))
print(reply)
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def set_reply(script: Path, reply: dict[str, Any] | str) -> None:
    text = reply if isinstance(reply, str) else json.dumps(reply)
    (script.parent / "reply.txt").write_text(text)


def ok_reply(**overrides: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"a": 1}',
        "structured_output": {"a": 1},
        "total_cost_usd": 0.02,
        "usage": {"input_tokens": 5, "cache_read_input_tokens": 10, "output_tokens": 7},
        "modelUsage": {"claude-haiku": {"outputTokens": 3}, "claude-opus": {"outputTokens": 90}},
    }
    reply.update(overrides)
    return reply


def request(schema: dict[str, Any] | None = SCHEMA, **kw: Any) -> ChatRequest:
    return ChatRequest(
        [ChatMessage("system", "be terse"), ChatMessage("user", "hello")], json_schema=schema, **kw
    )


async def test_structured_output(
    fake_claude: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    script, log = fake_claude
    set_reply(script, ok_reply())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "also-not")
    result = await ClaudeCodeChat(PATTERNS, binary=str(script)).complete(request())
    assert result.data == {"a": 1}
    assert result.model == "claude-opus"
    assert (result.input_tokens, result.output_tokens) == (15, 7)
    assert result.cost_usd == 0.02 and result.cost_estimated

    call = json.loads(log.read_text())
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == "be terse"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    assert call["stdin"] == "hello"
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in call["env"]
    assert Path(call["cwd"]).name.startswith("codeit-claude-")


def test_never_skips_permissions_on_host() -> None:
    """PRD 19.6: the flag is allowed only in worker containers."""
    backend = ClaudeCodeChat(PATTERNS, model="opus")
    args = backend.build_args("sys", request())
    assert "--dangerously-skip-permissions" not in args
    assert "dangerously" not in inspect.getsource(claude_module).split('"""', 2)[2]


async def test_plain_text_mode(fake_claude: tuple[Path, Path]) -> None:
    script, log = fake_claude
    set_reply(script, ok_reply(result="hi there", structured_output=None))
    result = await ClaudeCodeChat(PATTERNS, binary=str(script)).complete(request(schema=None))
    assert (result.text, result.data) == ("hi there", None)
    assert "--json-schema" not in json.loads(log.read_text())["argv"]


async def test_json_in_result_text_is_accepted(fake_claude: tuple[Path, Path]) -> None:
    script, _ = fake_claude
    set_reply(script, ok_reply(structured_output=None, result='```json\n{"a": 2}\n```'))
    result = await ClaudeCodeChat(PATTERNS, binary=str(script)).complete(request())
    assert result.data == {"a": 2}


async def test_non_json_answer(fake_claude: tuple[Path, Path]) -> None:
    script, _ = fake_claude
    set_reply(script, ok_reply(structured_output=None, result="sorry, no"))
    with pytest.raises(BackendOutputError) as err:
        await ClaudeCodeChat(PATTERNS, binary=str(script)).complete(request())
    assert err.value.text == "sorry, no"


async def test_usage_limit_parks_backend(fake_claude: tuple[Path, Path]) -> None:
    script, _ = fake_claude
    reset = int(time.time()) + 3600
    set_reply(
        script,
        ok_reply(is_error=True, subtype="error", result=f"Claude AI usage limit reached|{reset}"),
    )
    backend = ClaudeCodeChat(PATTERNS, binary=str(script))
    with pytest.raises(BackendUnavailable) as err:
        await backend.complete(request())
    assert err.value.until is not None and int(err.value.until.timestamp()) == reset
    state = await backend.available()
    assert state.state == "parked"
    with pytest.raises(BackendUnavailable):  # parked: no second call
        await backend.complete(request())


async def test_usage_limit_without_reset_time(fake_claude: tuple[Path, Path]) -> None:
    script, _ = fake_claude
    set_reply(script, ok_reply(is_error=True, result="5-hour limit reached, try later"))
    with pytest.raises(BackendUnavailable) as err:
        await ClaudeCodeChat(PATTERNS, binary=str(script)).complete(request())
    assert err.value.until is not None


async def test_other_error(fake_claude: tuple[Path, Path]) -> None:
    script, _ = fake_claude
    set_reply(script, ok_reply(is_error=True, subtype="error_max_turns", result=""))
    with pytest.raises(BackendError) as err:
        await ClaudeCodeChat(PATTERNS, binary=str(script)).complete(request())
    assert not isinstance(err.value, BackendUnavailable)


async def test_garbage_output(fake_claude: tuple[Path, Path]) -> None:
    script, _ = fake_claude
    set_reply(script, "not json at all")
    with pytest.raises(BackendError, match="not json at all"):
        await ClaudeCodeChat(PATTERNS, binary=str(script)).complete(request())


async def test_timeout(fake_claude: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    script, _ = fake_claude
    set_reply(script, ok_reply())
    monkeypatch.setenv("FAKE_SLEEP", "5")
    with pytest.raises(BackendError, match="timed out"):
        await ClaudeCodeChat(PATTERNS, binary=str(script)).complete(request(timeout_s=0.5))


async def test_missing_binary_is_unavailable() -> None:
    backend = ClaudeCodeChat(PATTERNS, binary="definitely-not-claude")
    assert (await backend.available()).state == "disabled"
    with pytest.raises(BackendUnavailable):
        await backend.complete(request())


async def test_retry_transcript_layout(fake_claude: tuple[Path, Path]) -> None:
    script, log = fake_claude
    set_reply(script, ok_reply())
    req = ChatRequest(
        [
            ChatMessage("system", "s"),
            ChatMessage("user", "plan this"),
            ChatMessage("assistant", '{"bad": 1}'),
            ChatMessage("user", "fix it"),
        ],
        json_schema=SCHEMA,
    )
    await ClaudeCodeChat(PATTERNS, binary=str(script), model="opus").complete(req)
    call = json.loads(log.read_text())
    assert call["stdin"] == (
        '## Request\n\nplan this\n\n## Your previous answer\n\n{"bad": 1}\n\n## Request\n\nfix it'
    )
    assert call["argv"][call["argv"].index("--model") + 1] == "opus"
