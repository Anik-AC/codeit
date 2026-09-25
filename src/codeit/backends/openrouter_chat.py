"""OpenRouter chat backend (PRD 9.2), called with httpx directly (ADR-0007).

- Tries the configured models in order. A 404 (model gone, common for rotating free
  models) marks the model dead for 24 hours; a 429 or 5xx moves on to the next model.
- Asks for usage accounting, so each answer reports its cost.
- With a JSON schema, sends `response_format: json_schema`. A model that rejects that
  gets one retry with the schema written into the system prompt instead.
- 401 and 402 (bad key, no credit) make the whole backend unavailable.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from codeit.backends.base import (
    Availability,
    BackendError,
    BackendOutputError,
    BackendUnavailable,
    ChatMessage,
    ChatRequest,
    ChatResult,
    parse_json_text,
)
from codeit.log import get_logger

log = get_logger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
DEAD_FOR = timedelta(hours=24)

# Process-wide: a model that 404s is dead for every role, not just the one that found out.
_dead_until: dict[str, datetime] = {}


def reset_dead_models() -> None:
    _dead_until.clear()


class OpenRouterChat:
    kind: Literal["chat"] = "chat"

    def __init__(
        self,
        name: str,
        models: Sequence[str],
        api_key: str | None,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.models = list(models)
        self._api_key = api_key
        self._base_url = base_url
        self._transport = transport

    def _live_models(self) -> list[str]:
        now = datetime.now(UTC)
        return [m for m in self.models if _dead_until.get(m, now) <= now]

    async def available(self) -> Availability:
        if not self._api_key:
            return Availability("disabled", reason="no OpenRouter key in .env")
        if not self._live_models():
            return Availability("disabled", reason="every configured model is dead")
        return Availability("ok")

    async def complete(self, req: ChatRequest) -> ChatResult:
        state = await self.available()
        if state.state != "ok":
            raise BackendUnavailable(f"{self.name}: {state.reason}")
        failures: list[str] = []
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}", "X-Title": "CodeIt"},
            timeout=req.timeout_s,
            transport=self._transport,
        ) as http:
            for model in self._live_models():
                try:
                    return await self._complete_with(http, model, req)
                except _TryNext as e:
                    failures.append(f"{model}: {e}")
        raise BackendUnavailable(f"{self.name}: no model answered ({'; '.join(failures)})")

    async def _complete_with(
        self, http: httpx.AsyncClient, model: str, req: ChatRequest
    ) -> ChatResult:
        use_format = req.json_schema is not None
        for _ in range(2):
            body = self._body(model, req, use_format)
            try:
                resp = await http.post("/chat/completions", json=body)
            except httpx.TransportError as e:
                raise _TryNext(f"{type(e).__name__}") from e
            error = _error_of(resp)
            if error is None:
                return self._result(model, resp.json(), req)
            status, message = error
            if status == 400 and use_format:
                log.info("openrouter.no_json_schema", model=model, error=message[:200])
                use_format = False
                continue
            if status in (401, 402, 403):
                raise BackendUnavailable(f"{self.name}: {status} {message}")
            if status == 404:
                _dead_until[model] = datetime.now(UTC) + DEAD_FOR
                log.warning("openrouter.model_dead", model=model, error=message[:200])
            raise _TryNext(f"{status} {message[:200]}")
        raise _TryNext("rejected the request")

    def _body(self, model: str, req: ChatRequest, use_format: bool) -> dict[str, Any]:
        messages = list(req.messages)
        if req.json_schema is not None and not use_format:
            instruction = (
                "Answer with one JSON object only, no prose and no code fence. It must match "
                f"this JSON Schema:\n{json.dumps(req.json_schema)}"
            )
            messages = [ChatMessage("system", instruction), *messages]
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "usage": {"include": True},
        }
        if req.json_schema is not None and use_format:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": req.schema_name,
                    "strict": False,
                    "schema": req.json_schema,
                },
            }
        return body

    def _result(self, model: str, reply: dict[str, Any], req: ChatRequest) -> ChatResult:
        try:
            text = str(reply["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as e:
            raise BackendError(f"{self.name}: unexpected response shape from {model}") from e
        data: Any = None
        if req.json_schema is not None:
            try:
                data = parse_json_text(text)
            except json.JSONDecodeError as e:
                raise BackendOutputError(
                    f"{self.name}/{model}: answer is not JSON", text=text
                ) from e
        usage = reply.get("usage") or {}
        return ChatResult(
            text=text,
            data=data,
            backend=self.name,
            model=str(reply.get("model") or model),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cost_usd=usage.get("cost"),
            raw=reply,
        )


class _TryNext(Exception):
    """This model failed in a way the next model might not."""


def _error_of(resp: httpx.Response) -> tuple[int, str] | None:
    """(status, message) if the response is an error, including 200s carrying `error`."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    err = body.get("error") if isinstance(body, dict) else None
    if resp.status_code < 400 and not err:
        return None
    status = resp.status_code
    message = resp.reason_phrase
    if isinstance(err, dict):
        message = str(err.get("message") or message)
        if status < 400 and isinstance(err.get("code"), int):
            status = int(err["code"])
    return (status if status >= 400 else 502), message
