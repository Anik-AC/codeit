# 0001. Rename to CodeIt with lowercase identifiers

- **Status:** Accepted
- **Date:** 2026-09-24

## Context

The PRD was drafted under the working name "Forge". Traces remained: the `forge` CLI, `forge-sandbox-app`, the Jira key `FORGE`, and mixed-case identifiers such as `CodeIt-worker` and `CodeIt_ROLE`. Docker image references must be lowercase, and mixed-case Python package and environment variable names go against convention.

## Decision

"CodeIt" is used in prose only. Every identifier is lowercase `codeit`, and environment variables use `CODEIT_`:

| Kind | Value |
|---|---|
| CLI command | `codeit` |
| Python package | `src/codeit/` |
| Env vars | `CODEIT_API_TOKEN`, `CODEIT_ROLE`, `CODEIT_TICKET` |
| Docker image | `codeit-worker:{version}` |
| MCP package | `codeit-mcp` |
| Jira project key | `CODEIT` |
| Sandbox repo | `codeit-sandbox-app` |
| Files | `data/codeit.db`, `data/logs/codeit.jsonl`, `codeit.yaml` |
| Bot git author | `codeit-bot` |

The target repo URL in config uses HTTPS instead of SSH, because pushes authenticate with a fine-grained token.

Until the dedicated Jira bot account (D4) exists, M1 and later may use the owner's own API token. Switching later only changes `.env`.

## Consequences

The PRD was updated to v1.1 with these names. `config.py` enforces a lowercase Docker image and an uppercase Jira key.
