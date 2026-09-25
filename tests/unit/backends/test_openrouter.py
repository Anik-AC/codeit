from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from codeit.backends.base import (
    BackendOutputError,
    BackendUnavailable,
    ChatMessage,
    ChatRequest,
    parse_json_text,
)
from codeit.backends.openrouter_chat import OpenRouterChat, reset_dead_models
from codeit.backends.registry import chat_backend, chat_route
from codeit.config import Secrets, load_config
from tests.conftest import REPO_ROOT

URL = "https://openrouter.ai/api/v1"
SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}


@pytest.fixture(autouse=True)
def _fresh_dead_models() -> Iterator[None]:
    reset_dead_models()
    yield
    reset_dead_models()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=URL) as router:
        yield router


def answer(content: str, model: str = "m1", cost: float = 0.001) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4, "cost": cost},
        },
    )


def bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(c.request.content) for c in route.calls]


def req(schema: dict[str, Any] | None = SCHEMA) -> ChatRequest:
    return ChatRequest([ChatMessage("user", "hi")], json_schema=schema, schema_name="thing")


def backend(*models: str, key: str | None = "sk-or") -> OpenRouterChat:
    return OpenRouterChat("openrouter_free", list(models or ["m1", "m2"]), key)


async def test_json_answer_with_schema(mock: respx.MockRouter) -> None:
    route = mock.post("/chat/completions").mock(return_value=answer('{"a": 3}'))
    result = await backend().complete(req())
    assert result.data == {"a": 3}
    assert (result.model, result.input_tokens, result.output_tokens, result.cost_usd) == (
        "m1",
        11,
        4,
        0.001,
    )
    body = bodies(route)[0]
    assert body["model"] == "m1"
    assert body["usage"] == {"include": True}
    assert body["response_format"]["json_schema"] == {
        "name": "thing",
        "strict": False,
        "schema": SCHEMA,
    }
    assert route.calls.last.request.headers["Authorization"] == "Bearer sk-or"


async def test_plain_text(mock: respx.MockRouter) -> None:
    route = mock.post("/chat/completions").mock(return_value=answer("hello"))
    result = await backend().complete(req(schema=None))
    assert (result.text, result.data) == ("hello", None)
    assert "response_format" not in bodies(route)[0]


async def test_404_marks_model_dead_and_uses_next(mock: respx.MockRouter) -> None:
    route = mock.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(404, json={"error": {"message": "No endpoints found", "code": 404}}),
            answer('{"a": 1}', model="m2"),
            answer('{"a": 2}', model="m2"),
        ]
    )
    b = backend()
    assert (await b.complete(req())).model == "m2"
    assert (await b.complete(req())).data == {"a": 2}  # m1 skipped: dead
    assert [x["model"] for x in bodies(route)] == ["m1", "m2", "m2"]


@pytest.mark.parametrize("status", [429, 500, 502])
async def test_transient_errors_try_next_model(mock: respx.MockRouter, status: int) -> None:
    mock.post("/chat/completions").mock(
        side_effect=[httpx.Response(status), answer('{"a": 1}', model="m2")]
    )
    assert (await backend().complete(req())).model == "m2"


async def test_error_inside_200_body(mock: respx.MockRouter) -> None:
    mock.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json={"error": {"message": "provider down", "code": 502}}),
            answer('{"a": 1}', model="m2"),
        ]
    )
    assert (await backend().complete(req())).model == "m2"


async def test_connection_error_tries_next(mock: respx.MockRouter) -> None:
    mock.post("/chat/completions").mock(
        side_effect=[httpx.ConnectError("x"), answer('{"a": 1}', model="m2")]
    )
    assert (await backend().complete(req())).model == "m2"


async def test_schema_rejected_falls_back_to_instruction(mock: respx.MockRouter) -> None:
    route = mock.post("/chat/completions").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "response_format not supported"}}),
            answer('```json\n{"a": 5}\n```'),
        ]
    )
    result = await backend().complete(req())
    assert result.data == {"a": 5}
    second = bodies(route)[1]
    assert "response_format" not in second
    assert second["messages"][0]["role"] == "system"
    assert "JSON Schema" in second["messages"][0]["content"]


@pytest.mark.parametrize("status", [401, 402, 403])
async def test_account_errors_make_backend_unavailable(mock: respx.MockRouter, status: int) -> None:
    route = mock.post("/chat/completions").mock(return_value=httpx.Response(status))
    with pytest.raises(BackendUnavailable):
        await backend().complete(req())
    assert route.call_count == 1


async def test_all_models_fail(mock: respx.MockRouter) -> None:
    mock.post("/chat/completions").mock(return_value=httpx.Response(429))
    with pytest.raises(BackendUnavailable, match="no model answered"):
        await backend().complete(req())


async def test_non_json_answer(mock: respx.MockRouter) -> None:
    mock.post("/chat/completions").mock(return_value=answer("I cannot do that"))
    with pytest.raises(BackendOutputError) as err:
        await backend().complete(req())
    assert err.value.text == "I cannot do that"


async def test_unexpected_shape(mock: respx.MockRouter) -> None:
    mock.post("/chat/completions").mock(return_value=httpx.Response(200, json={"choices": []}))
    with pytest.raises(Exception, match="unexpected response shape"):
        await backend().complete(req())


async def test_no_key_is_unavailable() -> None:
    b = backend(key=None)
    assert (await b.available()).state == "disabled"
    with pytest.raises(BackendUnavailable, match="no OpenRouter key"):
        await b.complete(req())


def test_parse_json_text() -> None:
    assert parse_json_text('{"a": 1}') == {"a": 1}
    assert parse_json_text('```\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_text('Sure! {"a": 1} Hope that helps.') == {"a": 1}
    with pytest.raises(json.JSONDecodeError):
        parse_json_text("nothing here")


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No .env in the cwd and no model or key variables in the environment."""
    monkeypatch.chdir(tmp_path)
    for var in list(os.environ):
        if var.startswith(("OPENROUTER_", "OPENCODE_")):
            monkeypatch.delenv(var)
    return tmp_path


def test_registry_routes(clean_env: Path) -> None:
    cfg = load_config(REPO_ROOT / "config" / "config.yaml")
    secrets = Secrets(_env_file=None, openrouter_api_key="one-key")  # type: ignore[arg-type]
    primary, fallback = chat_route(cfg, secrets, "planner")
    assert primary.name == "claude_code_chat"
    assert isinstance(fallback, OpenRouterChat)
    assert fallback.models == cfg.models["openrouter_free"]
    assert fallback._api_key == "one-key"
    with pytest.raises(ValueError, match="not a chat backend"):
        chat_backend("opencode_paid", cfg, secrets, "coder")


def test_registry_uses_models_from_env(clean_env: Path) -> None:
    (clean_env / ".env").write_text("OPENROUTER_MODELS_FREE=x/one:free, y/two:free\n")
    cfg = load_config(REPO_ROOT / "config" / "config.yaml")
    _, fallback = chat_route(cfg, Secrets(_env_file=None), "planner")
    assert isinstance(fallback, OpenRouterChat)
    assert fallback.models == ["x/one:free", "y/two:free"]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"openrouter_api_key": "a", "openrouter_key_reviewer": "b"}, "a"),
        ({"openrouter_key_reviewer": "b"}, "b"),  # older .env files still work
        ({"openrouter_key_ops": "c"}, "c"),
        ({}, None),
    ],
)
def test_single_openrouter_key(values: dict[str, str], expected: str | None) -> None:
    assert Secrets(_env_file=None, **values).openrouter_key() == expected  # type: ignore[arg-type]
