"""Build backends from config routing (PRD 9.3). Routing names map to backends:

- `claude_code_chat`: Claude Code in chat mode on the host
- any key of `models:` that holds a list: OpenRouter with those models, in order

Every role uses the one OpenRouter key, and model lists can be overridden from `.env`
(ADR-0008, `codeit.model_env`).
"""

from __future__ import annotations

from codeit.backends.base import ChatBackend
from codeit.backends.claude_code import ClaudeCodeChat
from codeit.backends.openrouter_chat import OpenRouterChat
from codeit.config import Config, Role, Secrets
from codeit.model_env import effective_models


def chat_backend(name: str, cfg: Config, secrets: Secrets, role: Role) -> ChatBackend:
    if name == "claude_code_chat":
        return ClaudeCodeChat(cfg.claude.usage_limit_patterns)
    models = effective_models(cfg).get(name)
    if isinstance(models, list):
        return OpenRouterChat(name, models, secrets.openrouter_key())
    raise ValueError(f"routing name {name!r} is not a chat backend")


def chat_route(cfg: Config, secrets: Secrets, role: Role) -> list[ChatBackend]:
    """Primary then fallback backend for a chat role."""
    route = cfg.routing[role]
    names = [route.primary] + ([route.fallback] if route.fallback else [])
    return [chat_backend(n, cfg, secrets, role) for n in names]
