# 0017. Rebase agent

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

M10 builds the Rebase agent (PRD 11.5). Agent PRs fall behind or conflict as other PRs merge: sandbox PR #11 conflicted after #7, #9 and #12 merged. The PRD gives the trigger, the clean and conflicted paths, and the escalation rules. It leaves open where the rebase runs, how Claude is budgeted for it, and how the result is trusted.

## Decision

### Finding PRs

- **Polling:** every `rebase.poll_minutes`, the scheduler lists open PRs from the target repo's own branches and maps each to a ticket by its branch name (`{KEY}-...`). It keeps those GitHub marks `dirty` (conflicts) or `behind`. `unknown` means GitHub is still computing, so that PR waits for the next poll.
- **Skipped:** tickets in `In Dev` (the Coder owns the branch), tickets labelled `needs-human` (a human is already involved), and tickets leased or running.
- **Slots:** `slots.rebase` (default 1).

### One rebase (`agents/rebase.py`, `codeit run rebase KEY`)

1. **Lease** the ticket with role `rebase` and heartbeat, like the other agents.
2. **Clone:** check out the PR branch in its own clone, `data/worktrees/<repo>/{KEY}-rebase`, reset to `origin/<branch>`. It never shares the Coder's working tree.
3. **Rebase on the host:** `git rebase origin/<base>`. A clean rebase needs no model and no container.
4. **Conflicts:**
   - Escalate if there are more than `rebase.max_files`, or any path matches `rebase.never_auto` or the target's `never_auto_rebase`.
   - Otherwise, if Claude may run now, Claude Code resolves them in a worker container on the mid-rebase clone, with `GIT_EDITOR=true`. Its prompt holds the conflicted files, the ticket, and the subjects of what landed on main since the branch point.
   - If Claude may not run (run window, parking, concurrency, not counting the job's own lease), the rebase is aborted with nothing changed. The PR waits for a later poll, and Jira gets no comment.
5. **Distrust the agent.** A resolution is escalated when any of these hold:
   - a rebase is still in progress (git's `rebase-merge` or `rebase-apply` directory exists; `REBASE_HEAD` can outlive a finished rebase)
   - the result is not on top of the base
   - conflict markers remain
   - the agent did not report `resolved`
6. **Tests:** CodeIt itself runs install, typecheck, unit and e2e in a container without credentials, after either path. Only green results are pushed, with `--force-with-lease=<branch>:<the head it started from>`, so a concurrent push is never overwritten.
7. **Jira:**
   - Success: a comment ("Rebased ... onto main ..., tests green", plus which files the agent resolved). A ticket in Human Review is told the diff changed.
   - Escalation: a comment with the reason and the conflicted files, and the `needs-human` label.
   - The status never changes.

### Differences from PRD 11.5

- **The clean rebase runs on the host,** not in the container: it is plain git with the bot's identity. Only tests and conflict resolution use containers.
- **No jira-mcp token for the Rebase agent.** The ticket is given in the prompt, and the agent has nothing to write to Jira.

### Fixes found while building M10

- **Ctrl-C did not stop `codeit up`:**
  - uvicorn takes over SIGINT and SIGTERM in `serve()`. Both uvicorn servers (jira-mcp, dashboard API) now run with `capture_signals` disabled; the orchestrator owns the signals and stops them.
  - The API server has a 3-second graceful-shutdown limit, and its live streams end themselves on shutdown, so an open browser tab no longer holds shutdown up.
- **Evals used the production Reviewer's daily budget:** the M9 Reviewer evals recorded their spend as `reviewer`, which hit the $0.50 cap and blocked real reviews for the day. Eval spend is now recorded as `eval`. Today's 94 eval rows ($0.58) in the local database were reclassified.
- **Stale containers:** a killed eval left a worker container running for hours, and eval runs have no per-run lease for the reaper to find. Each reap now removes worker containers older than the longest sandbox timeout plus 30 minutes.
- **"Waiting" messages:** they now repeat only when the reason changes in kind, not when a dollar amount in it changes.

## Consequences

- An open agent PR is kept mergeable without a human, as long as conflicts are small and in ordinary files.
- Lockfiles, migrations, CI configuration and large conflicts always reach the owner.
- A rebase force-pushes the PR branch, so CI runs again and any earlier review refers to the old head.
- A clean rebase holds a `rebase` lease, which counts as a Claude run in the concurrency limit while its tests run (a few minutes).

## Live acceptance (2026-09-30)

- **CODEIT-83, sandbox PR #11:**
  - It conflicted on `src/server/app.ts` after #7, #9 and #12 merged.
  - `codeit run rebase CODEIT-83` rebased it onto `2f42ae4`, and the agent resolved the conflict.
  - CodeIt's tests were green, and it pushed `90d30c7`.
  - GitHub then reported the PR `mergeable: clean`, and CI (checks, e2e) passed.
  - The file keeps every endpoint from both sides: health, ping, version, and the ticket's `GET /api/tasks/:id`.
- **Escalation for `never_auto` paths:** covered by unit tests with real git (a `package-lock.json` conflict, and more than 5 files). Nothing is pushed, and the ticket gets `needs-human`.
