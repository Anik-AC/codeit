# 0006. jira-mcp implementation

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

ADR-0004 moved jira-mcp to the host, with per-run bearer tokens. Building it in M2 raised questions the PRD does not settle:
- which MCP SDK API to use
- where tokens live
- how per-role visibility works over one HTTP endpoint
- how the owner starts the stdio server from another directory

It also exposed a migration bug from M0.

## Decision

**MCP SDK 2.x.** The resolved `mcp` package is 2.2, where `FastMCP` is renamed `MCPServer`. We use it rather than pin 1.x.

**Bearer auth uses the SDK's `TokenVerifier`.**
- `RunTokenVerifier` adapts our token store.
- The SDK's middleware returns 401 for missing, unknown, expired or revoked tokens before any MCP handling.
- Tools read the verified grant with `get_access_token()`. A test confirmed that each call sees its own caller's token in both stateful and stateless mode.
- `AuthSettings` is resource-server only: no OAuth routes and no metadata endpoint.

**One server with per-caller filtering, not one server per role.**
- `JiraMCP` overrides `list_tools` and `call_tool`.
- A tool outside the caller's role is missing from the list. Calling it anyway returns the SDK's own `Unknown tool: <name>` text, so it looks the same as a tool that does not exist.
- Over stdio, the role is fixed at startup (`CODEIT_ROLE`, default `human`).

**Tokens live in SQLite (`mcp_tokens`), hashed.**
- Hashes rather than an in-memory registry, so the orchestrator (M7), `codeit mcp token`, and a server started separately with `codeit mcp serve` all share them.
- Tokens survive a server restart.
- Tokens are 32 random bytes. Only the SHA-256 is stored.
- `human` cannot hold a token.

**Scoping inside tools:**
- keys must match `^{PROJECT}-\d+$`
- `search_tickets` becomes `project = P AND (<jql>) <order by>`
- JQL with unbalanced parentheses (outside quotes) is rejected, because it could close the wrapper
- results from other projects are dropped as a second check

**Comment signing and labels:**
- Comments posted with a run token end with `_Posted by <role> (run <id>)_`, so the source shows in Jira.
- Issues created by the planner always get `agent-draft`.

**DNS rebinding protection** stays on for the HTTP listener, with allowed `Host` values from `mcp.allowed_hosts`. M4 adds the name containers use.

**A flag-free stdio entry point, `codeit-jira-mcp`.**
- MCP clients start stdio servers in the user's project directory, so the script switches to the CodeIt checkout (`CODEIT_HOME`, default: this repo) before reading `config/`, `.env` and `jira_ids.yaml`.
- It needs no flags, which the MCP Inspector's CLI otherwise misparses.

**`mcp_servers` is a second top-level package** in the wheel, as in the PRD 21 layout. Ruff treats it as first-party.

**M0 migration fix.**
- `alembic/env.py` ran `PRAGMA journal_mode=WAL` before Alembic's transaction. SQLAlchemy opened a transaction for it, which Alembic then treated as external and never committed. The `alembic_version` row was lost, so a second `codeit db upgrade` failed with "table already exists".
- The fix commits after the PRAGMA. A regression test runs the upgrade twice.

## Consequences

- One more table and migration (`0002`).
- `codeit mcp token` lets the owner test the HTTP path before the orchestrator exists.
- Upgrading the SDK must keep `get_access_token()` behaving per call. `test_http.py` covers it end to end.
