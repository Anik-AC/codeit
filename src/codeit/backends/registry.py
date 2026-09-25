"""Build backends from config routing (PRD 9.3). Routing names map to backends:

- `claude_code_chat`: Claude Code in chat mode on the host
- any key of `models:` that holds a list: OpenRouter with those models, in order

OpenRouter keys are per role (PRD 9.2): the reviewer and coder have their own; every
other role (planner, docs, learning, ops) uses the ops key.
"""

from __future__ import annotations

from codeit.backends.base import ChatBackend
from codeit.backends.claude_code import ClaudeCodeChat
from codeit.backends.openrouter_chat import OpenRouterChat
from codeit.config import Config, Role, Secrets


def openrouter_key(secrets: Secrets, role: Role) -> str | None:
    key = {
        "reviewer": secrets.openrouter_key_reviewer,
        "coder": secrets.openrouter_key_coder,
    }.get(role, secrets.openrouter_key_ops)
    return key.get_secret_value() if key else None


def chat_backend(name: str, cfg: Config, secrets: Secrets, role: Role) -> ChatBackend:
    if name == "claude_code_chat":
        return ClaudeCodeChat(cfg.claude.usage_limit_patterns)
    models = cfg.models.get(name)
    if isinstance(models, list):
        return OpenRouterChat(name, models, openrouter_key(secrets, role))
    raise ValueError(f"routing name {name!r} is not a chat backend")


def chat_route(cfg: Config, secrets: Secrets, role: Role) -> list[ChatBackend]:
    """Primary then fallback backend for a chat role."""
    route = cfg.routing[role]
    names = [route.primary] + ([route.fallback] if route.fallback else [])
    return [chat_backend(n, cfg, secrets, role) for n in names]
