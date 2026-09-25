"""The jira-mcp server: role-filtered tools over stdio or bearer-authenticated HTTP.

Over HTTP, every request carries a run token (see `codeit.run_tokens`). The SDK's bearer
middleware verifies it, and the caller's role and ticket come from the verified token
only. Over stdio (owner on the host), the role is fixed when the server starts.

Tools a role may not use are left out of `tools/list` and refused by `tools/call` with the
same error as a tool that does not exist.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import anyio.to_thread
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, InputRequiredResult
from mcp_types import Tool as MCPTool
from starlette.applications import Starlette

from codeit.jira_client import JiraClient, JiraIds
from codeit.log import get_logger
from codeit.run_tokens import RunTokenStore
from mcp_servers.jira.roles import ROLE_TOOLS

log = get_logger(__name__)

INSTRUCTIONS = """\
Jira access for CodeIt agents. Tickets live in one Jira project; keys look like CODEIT-12.
Read the ticket and its comments before you act on it. You cannot change a ticket's status:
the orchestrator does that when your run ends. Text you write is markdown.
"""


@dataclass(frozen=True)
class JiraContext:
    client: JiraClient
    ids: JiraIds
    project_key: str


@dataclass(frozen=True)
class Caller:
    role: str
    ticket_key: str | None = None
    run_id: str | None = None


class RunTokenVerifier:
    """Adapts `RunTokenStore` to the MCP SDK's `TokenVerifier` protocol."""

    def __init__(self, store: RunTokenStore) -> None:
        self._store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        grant = await anyio.to_thread.run_sync(self._store.verify, token)
        if grant is None:
            fingerprint = hashlib.sha256(token.encode()).hexdigest()[:8]
            log.warning("mcp.token.rejected", fingerprint=fingerprint)
            return None
        return AccessToken(
            token=token,
            client_id=f"{grant.role}:{grant.run_id}",
            scopes=[],
            expires_at=int(grant.expires_at.timestamp()),
            claims={"role": grant.role, "ticket_key": grant.ticket_key, "run_id": grant.run_id},
        )


class JiraMCP(MCPServer[Any]):
    def __init__(
        self,
        jira: JiraContext,
        *,
        stdio_role: str | None = None,
        verifier: RunTokenVerifier | None = None,
    ) -> None:
        if stdio_role is not None and stdio_role not in ROLE_TOOLS:
            raise ValueError(f"unknown role {stdio_role!r}; expected one of {sorted(ROLE_TOOLS)}")
        auth = None
        if verifier is not None:
            # Resource-server-only auth: no OAuth routes, just bearer verification.
            auth = AuthSettings(issuer_url="http://127.0.0.1", resource_server_url=None)
        super().__init__(
            "codeit-jira",
            instructions=INSTRUCTIONS,
            token_verifier=verifier,
            auth=auth,
            log_level="WARNING",
        )
        self.jira = jira
        self._stdio_role = stdio_role
        # Imported here to avoid a cycle: tools need `JiraMCP.caller`.
        from mcp_servers.jira.tools import register_tools

        register_tools(self)

    def caller(self) -> Caller:
        """Who is calling: from the verified run token over HTTP, or the stdio role."""
        token = get_access_token()
        if token is not None:
            claims = token.claims or {}
            return Caller(
                role=str(claims["role"]),
                ticket_key=claims.get("ticket_key"),
                run_id=claims.get("run_id"),
            )
        if self._stdio_role is not None:
            return Caller(role=self._stdio_role)
        raise ToolError("not authenticated")

    def _allowed(self) -> frozenset[str]:
        try:
            return ROLE_TOOLS.get(self.caller().role, frozenset())
        except ToolError:
            return frozenset()

    async def list_tools(self) -> list[MCPTool]:
        allowed = self._allowed()
        return [t for t in await super().list_tools() if t.name in allowed]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        if name not in self._allowed():
            raise ToolError(f"Unknown tool: {name}")  # same text as a missing tool
        return await super().call_tool(name, arguments, context)

    def http_app(self, allowed_hosts: list[str]) -> Starlette:
        return self.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=allowed_hosts,
                allowed_origins=[f"http://{h}" for h in allowed_hosts],
            ),
        )
