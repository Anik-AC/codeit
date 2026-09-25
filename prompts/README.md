# prompts

Versioned Jinja2 markdown prompts, one folder per role (planner, coder, reviewer, rebase, docs, learning, ops). Every run records the SHA-256 of its role's prompt files (`codeit.prompts.prompt_hash`), so eval results can be traced to a prompt version.

| Role | Files |
|---|---|
| planner | `system.md` (role and rules), `user.md` (plan, repo context, open tickets), `retry.md` (sent once after a validation error) |

Untrusted input (plans, repo files, ticket text) is always placed inside tagged sections and described as data (PRD 19.3).
