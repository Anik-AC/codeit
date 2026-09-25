# jira-mcp

The MCP server that gives agents Jira access. It runs on the host and is the only holder of the Jira token besides the orchestrator (PRD 15, ADR-0004, ADR-0006).

| Module | Contents |
|---|---|
| `server.py` | `JiraMCP`: role filtering, run-token verifier |
| `tools.py` | The six tools and their scoping rules |
| `roles.py` | Which tools each role sees |
| `app.py` | Entry points: `serve_http`, `serve_stdio`, the `codeit-jira-mcp` script |

Usage is in the main [README](../../README.md#jira-mcp).
