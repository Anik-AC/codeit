"""Ask a chat backend for an answer that validates against a pydantic model: the first
available backend in order, one retry with the validation errors, then the next backend."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from codeit.backends.base import (
    BackendOutputError,
    BackendUnavailable,
    ChatBackend,
    ChatMessage,
    ChatRequest,
)
from codeit.log import get_logger
from codeit.prompts import render

log = get_logger(__name__)


@dataclass(frozen=True)
class ModelCall:
    backend: str
    model: str | None
    calls: int
    cost_usd: float | None


async def structured[M: BaseModel](
    backends: Sequence[ChatBackend], messages: list[ChatMessage], model: type[M], name: str
) -> tuple[M, ModelCall]:
    """Ask the first available backend for `model`, retrying once on invalid output."""
    schema = model.model_json_schema()
    reasons: list[str] = []
    for backend in backends:
        convo = list(messages)
        cost: float | None = None
        try:
            for attempt in (1, 2):
                try:
                    result = await backend.complete(
                        ChatRequest(convo, json_schema=schema, schema_name=name)
                    )
                except BackendOutputError as e:
                    answer, error = e.text, f"- not valid JSON: {e}"
                else:
                    cost = (cost or 0) + (result.cost_usd or 0)
                    try:
                        parsed = model.model_validate(result.data)
                        return parsed, ModelCall(backend.name, result.model, attempt, cost)
                    except ValidationError as err:
                        answer = result.text
                        error = "\n".join(
                            f"- {'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                            for e in err.errors()
                        )
                log.warning("model.invalid_output", backend=backend.name, attempt=attempt)
                convo += [
                    ChatMessage("assistant", answer),
                    ChatMessage("user", render("reviewer/retry.md", error=error)),
                ]
            reasons.append(f"{backend.name}: invalid output twice")
        except BackendUnavailable as e:
            reasons.append(str(e))
    raise BackendUnavailable("; ".join(reasons) or "no backend configured")
