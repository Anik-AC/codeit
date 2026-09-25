# 0011. Coder and steering kit

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

M5 builds the Coder (PRD 11.2), the target repo steering kit and `codeit init-target` (PRD 16), and the first host-side GitHub client (PRD 8). Several details were open, and PRD 16.3 asks for the hook behaviour to be verified against the installed Claude Code.

## Decision

### Steering kit (`templates/target/`, `codeit init-target`)

- **Contents:** `CLAUDE.md`, five skills in `.claude/skills/<name>/SKILL.md`, `.claude/settings.json`, two hook scripts, and `codeit.yaml` when the repo has none.
- **Rendering:** files ending in `.j2` are rendered with the target's `codeit.yaml` commands, so the hooks and `CLAUDE.md` name the repo's real unit command. Commands are written to YAML as quoted strings: a bare `true` or anything containing `: ` would otherwise break the file. Existing files are never overwritten, and hook scripts are made executable.
- **PR template:** `templates/pr_body.md` is embedded in the `open-pr` skill, together with the RESULT line contract.
- **Skill examples:** the skills name this stack's tools (supertest, Testing Library, Playwright). For another stack, edit the skills after running `init-target`.

### Hooks, verified with Claude Code 2.1.282 in the worker image

- **Stop:** runs the unit command. On failure it exits 2 with the tail of the output on stderr; on `stop_hook_active` it exits 0.
  - Observed: with a failing test, the first stop was blocked and Claude received the failure, including the exact test and line. The second stop, with `stop_hook_active: true`, was allowed.
  - So the hook blocks once per stop, as the PRD intends. Anything still red is caught by the Reviewer's phase 1 checks in M6.
- **PostToolUse** on `Edit|Write|MultiEdit`: runs `eslint --fix` on the changed JS/TS file. Remaining problems go back to Claude with exit 2, which does not undo the edit.
- **Paths:** hooks are addressed through `$CLAUDE_PROJECT_DIR`.
- **Skills:** Claude Code discovers `.claude/skills/` in the container; the Coder's `--tools` list includes `Skill` and `TodoWrite`.

### Coder (`agents/coder.py`, `agents/coder_run.py`)

- **Claim:** as PRD 11.2 describes: lease, check `Ready for Dev`, move to `In Dev`, set Agent and Run ID, and comment.
  - Leases (`orchestrator/leases.py`) arrive now rather than in M7: an atomic upsert that takes free or expired leases only, plus a heartbeat every 60 seconds.
- **Branch:** on rework it comes from the existing PR (`PR URL` field or open PR). Otherwise it is `{KEY}-{slug}`.
- **Rework feedback** covers Jira comments plus PR reviews and comments since the previous Coder run on the ticket started.
  - CodeIt writes to Jira as the owner's account (ADR-0001), so authors cannot tell CodeIt's comments apart. Orchestrator comments end with `_(CodeIt orchestrator)_`, and agent comments carry their jira-mcp signature. Those two are left out; Reviewer comments stay in.
- **jira-mcp:** a manual `codeit run coder` starts jira-mcp over HTTP in-process for the run, unless something already listens on the configured port. The run's token is revoked at the end, whatever happened.
- **The agent's RESULT line is not trusted on its own.** `pr_opened` or `pr_updated` counts only if GitHub shows an open PR for the branch whose head SHA changed during the run. Otherwise the ticket goes to Human Review with `needs-human`.
- **Outcomes** follow the PRD 11.2 table. A rework rebase that conflicts goes to Human Review as `blocked`, until the Rebase agent (M10) exists.
- **Before any transition**, the ticket is re-read. If a human moved it during the run, nothing is applied (PRD 12.5).
- **Crashes:** an unexpected error after the claim moves the ticket to Human Review with `needs-human`, best effort, so it does not sit in `In Dev`.
- **Local mode:** `codeit run coder --file ticket.md` (PRD 25.1) runs the same agent without Jira, GitHub or jira-mcp, and the agent only commits.
- Every run is recorded in `runs` (`codeit.runs`), including prompt hash, model, turns, tokens, cost and transcript path.

### GitHub client (`github_client/`)

- httpx with a few retries and Link-header paging.
- **Tokens:** reads use `GITHUB_TOKEN_READONLY`. `GITHUB_TOKEN_AGENT` is used only for host-side git fetches and inside the container (`GH_TOKEN`).
- Functions for M5 and M6: `find_pr`, `get_pr`, `branch_head`, `pr_feedback`, `check_runs`.

## Consequences

- Coder runs are the first to touch GitHub. A human still merges every PR, and `main` in the target repo is protected.
- The one-bounce Stop hook limits how long an agent can argue with failing tests. M6's phase 1 checks are the hard gate.

## Live acceptance (2026-09-25)

Ticket CODEIT-72, "Show how many tasks there are" (1 point):

1. **First run:** `pr_opened`, 45 turns, about 2 minutes on `claude-sonnet-5`. It opened [codeit-sandbox-app#3](https://github.com/Anik-AC/codeit-sandbox-app/pull/3) with a `TaskCount` component, unit tests and a Playwright test. CI was green, and the ticket moved to Agent Review with `PR URL` set.
2. **Rework:** after a review comment asking for "You have N tasks", the ticket was sent back and a second run returned `pr_updated` (29 turns). It loaded the `address-feedback` skill and pushed one commit, `CODEIT-72: address review: ...`, to the **same** PR. CI was green again.
