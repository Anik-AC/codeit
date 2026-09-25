# jira-mcp

FastMCP server exposing `jira_client` tools to agents. Runs on the host and is the only holder of the Jira token besides the orchestrator. Worker containers connect over streamable HTTP with a short-lived run token bound to their role and ticket. The owner uses stdio for interactive sessions. See PRD 15 and ADR-0004. Built in **M2**.
