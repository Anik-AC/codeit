# 0004. Run jira-mcp on the host with per-run tokens

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

PRD v1.1 ran jira-mcp as a stdio server inside each worker container. That put the Jira bot token in every coder and rebase container. It also meant the only controls on it were `CODEIT_ROLE` and `CODEIT_TICKET`, two environment variables in the container.

Worker containers are untrusted (PRD 5.2). They run LLM-driven shell commands on ticket text. A prompt-injected agent could read the Jira token, and then:
- call the Jira REST API directly, with every permission the token has, including transitions
- act on any ticket, not just its own
- rewrite `CODEIT_ROLE` or `CODEIT_TICKET` to unlock hidden tools

The Jira site host also had to be on the egress allowlist, which opened a direct path from the container to Jira.

## Decision

- **jira-mcp runs on the host**, as the only holder of the Jira token besides the orchestrator. It is not installed in the worker image.
- **Worker containers connect over streamable HTTP.** The owner's interactive use stays on stdio (role `human`, host only).
- **Each run gets a short-lived bearer token** from the orchestrator:
  - bound to `{role, ticket_key, run_id}`
  - expires at the role's wall-clock timeout plus 5 minutes
  - revoked when the run ends
  - stored hashed on the server
  - delivered only in the run's `/run/mcp.json`
- **Role and ticket come from the token**, never from anything the container sends. `CODEIT_TICKET` is dropped. `CODEIT_ROLE` is used only by the stdio transport on the host.
- **The Jira site host is removed** from the container egress allowlist.
- **The jira-mcp listener** must be reachable from the sandbox network and from nowhere wider. The bind address and container-side hostname under Docker Desktop + WSL2 are verified in M4 and recorded in a follow-up ADR.

## Accepted risk: Claude OAuth token in containers

Claude Code must authenticate inside coder and rebase containers, so `CLAUDE_CODE_OAUTH_TOKEN` stays there.

- **Impact:** misuse of the owner's Claude Pro quota. The token grants no access to Jira, GitHub or the host.
- **Limited by the egress allowlist:** the token cannot be sent to arbitrary hosts.
- **Residual:** allowlisted channels, such as a branch push, a PR body or an OpenRouter request, could still carry it out. The owner reviews every PR, and the token can be rotated with `claude setup-token`.
- **Window of higher exposure:** until the egress proxy lands (at the latest M7, PRD 10.3), containers from M4 on may run with unrestricted egress. During that window, run only owner-written tickets.

## Consequences

- A compromised container can do at most what its role's jira-mcp tools allow, on its own ticket, while its run lasts. It cannot transition statuses.
- jira-mcp must be running whenever an agent run needs Jira. `codeit up` starts it with the orchestrator. It also needs a token registry that the orchestrator can write to.
- M2 grows: bearer-token auth, per-session tool filtering, and token rejection tests.
- M4 must solve host reachability from containers, which a stdio server inside the container did not need.
- PRD sections 5.1, 5.2, 10.1, 10.3, 15, 19, 20.2, 23 (M2) and 24 were updated. `config/config.yaml` and `.env.example` were aligned with them.
