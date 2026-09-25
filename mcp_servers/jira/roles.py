"""Which jira-mcp tools each role sees (PRD 15). A tool not listed for a role is invisible."""

from __future__ import annotations

READ = frozenset({"get_ticket", "get_comments"})

ROLE_TOOLS: dict[str, frozenset[str]] = {
    "human": READ | {"search_tickets", "add_comment", "create_issue", "link_issues"},
    "planner": READ | {"search_tickets", "create_issue", "link_issues"},
    "learning": READ | {"search_tickets"},
    "coder": READ | {"add_comment"},
    "reviewer": READ | {"add_comment"},
    "rebase": READ | {"add_comment"},
    "docs": READ,
}

ALL_TOOLS = frozenset().union(*ROLE_TOOLS.values())
