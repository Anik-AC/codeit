# 0013. Orchestrator and egress proxy

- **Status:** Accepted
- **Date:** 2026-09-29

## Context

M7 builds the unattended loop (PRD 12): `codeit up`, the scheduler, leases with heartbeats and crash recovery, the budget guard (PRD 9.4), the merge watcher (PRD 11.4) and the container egress allowlist (PRD 10.3). The PRD leaves several details open, and live testing turned up problems that needed decisions.

## Decision

### `codeit up`

- **One asyncio process:** jira-mcp over HTTP, the scheduler loop and the merge watcher. The API and dashboard (M8), the rebase poller (M10) and the cron jobs (M11, M12) join it later.
- **Preflight** fails fast when `GITHUB_TOKEN_AGENT` or `CLAUDE_CODE_OAUTH_TOKEN` is missing or the worker image is absent, and it starts the egress proxy. A missing OpenRouter key is only a warning: the Reviewer then waits.
- **Logs** go to `data/logs/codeit.jsonl`. The console shows one line per event, prefixed with the agent instance.
- **`--any-time`** ignores `claude.run_window` for that session. It exists for daytime testing; the configured window stays 23:00 to 08:00.

### Scheduler

- **Order:** reviewer first, then coder (PRD 12.2).
- **Per role, while a slot is free and the budget allows,** it takes the next ticket from the role's JQL that is:
  - not already running here
  - not leased by another process, such as a manual `codeit run`
  - not cooling down
  - for the coder, not blocked by an unfinished ticket
- **Agent runs keep their own claim logic.** The scheduler does not take the lease; `run_coder` and `run_reviewer` take it themselves, as they already did for manual runs, and accept a `run_id` from the scheduler. A refused claim, such as a race with a human or a manual run, is just a failed job.
- **Errors:** a job that raises puts its ticket on a 10-minute cooldown, so a broken ticket or Docker outage does not spin.
- **Claude concurrency** (`claude.max_concurrent_runs`) counts this process's Coder jobs plus every live coder or rebase lease in the database. So a crashed run whose container is still working, or a manual run, holds the one Claude slot until it is reaped or finishes.
- **Slots** are re-read from `config.yaml` every loop. `agent_instances` holds each instance as busy, idle, parked (the budget refused the role) or disabled (above the slot count), for `codeit agents` and the M8 dashboard.
- **Isolation:** each step of a tick (reaper, each role, merge watcher) runs in its own `try`, so one failure does not stop the rest.
- **Shutdown:** Ctrl-C or SIGTERM stops new claims and waits 60 seconds for running jobs. The rest are cancelled and their runs marked `interrupted`.

### Leases, heartbeats and recovery

- **Heartbeats:** leases heartbeat every 60 seconds for the whole run. M5 only did this while the agent ran; the Reviewer did not do it at all.
- **Dead runs:** a lease is dead when it expired, **or when its heartbeat stopped 3 minutes ago**. The TTLs (90 minutes for the coder) are kept as the backstop, but the PRD's "Killing the process mid-run recovers correctly on restart" should not take 90 minutes.
- **For each dead lease,** the reaper:
  - marks the run `abandoned`
  - removes its container (by the `codeit.run_id` label)
  - revokes its jira-mcp token
  - releases the lease
- **Then it applies PRD 6.2:**
  - **Coder, ticket still `In Dev`:** back to Ready for Dev, or to Human Review with `needs-human` at the 2nd abandoned run.
  - **Reviewer, ticket still `Agent Review`:** left for the next reviewer, with the same cap of 2.
  - **Ticket in any other status:** left alone, because a human moved it or the run finished its transition before dying.
- **Orphans at startup:** a ticket `In Dev` with no lease whose last Coder run is still `running` or `interrupted` is recovered the same way. `interrupted` (a clean shutdown) is not counted against the ticket.
- **Resuming after a crash:** the dead run's container may have finished and pushed the work before it was reaped. If the previous Coder run on a ticket was `abandoned` or `interrupted`, an open PR for the branch counts as the result even without new commits, and the ticket goes to Agent Review. Otherwise the M5 rule stands: a claimed PR must have new commits.
- **Clones:** rework on an existing ticket clone first discards uncommitted edits, which are left over from a run that died. If git fails on the clone, for example with a ref left pointing at a missing object after a crash mid-fetch, the clone is deleted and made again from GitHub. The same applies to the shared mirror and to review clones, which a hard restart once left with an empty object file.

### Budget guard (`orchestrator/budget.py`)

- **Claude roles** need all of these: Claude not parked, the local time inside `claude.run_window` (windows may cross midnight; an empty list means any time), and the concurrency limit not reached.
- **OpenRouter roles** need all of these: a key, the role's spend today under `openrouter.daily_usd_cap`, and, for roles routed to the free list first, free-model requests under `free_requests_daily_cap`.
- **Spend:** the Reviewer and Planner write their OpenRouter cost and request count to `budget_ledger` when a run ends. "Today" is the local day.

### Merge watcher

- **Human Review with a merged PR:** a comment with the merge commit, then Done. The ticket's clone and review clone are removed.
- **Done with an unmerged PR:** label `state-mismatch` and a comment, nothing else. Only tickets updated in the last 2 days and not already labelled are checked, so the number of GitHub calls stays small.
- **Reviewer and merged PRs:** a PR merged while its ticket waits in Agent Review is not reviewed. The ticket goes to Human Review with a comment, and the watcher then moves it to Done. Before, it escalated as "closed".

### Egress proxy (PRD 10.3)

- **Built with the Docker SDK instead of a `compose.yaml` sidecar,** so `codeit up`, manual runs and tests all set it up the same way:
  - `codeit-workers` is an internal network, with no route to the internet or the host
  - `codeit-proxy` runs tinyproxy (image `codeit-proxy:0.1.0`, `sandbox/proxy/`, Alpine) on that network and on `codeit-egress`
  - the proxy container runs read-only, with no capabilities, as `nobody`
- **Filtering:** tinyproxy filters on the whole URL, deny by default:
  - HTTPS (CONNECT on 443) and HTTP to each host in `sandbox.egress_allowlist`; subdomains need their own entry
  - plain HTTP to jira-mcp on the host, **on its port only**. Other ports on the host are refused.
- **Proxy config:** written to `data/proxy/`. The proxy is recreated when the config's hash changes.
- **Workers get:**
  - `HTTP(S)_PROXY` in both spellings
  - `NO_PROXY` for localhost, so e2e tests reach their own dev server
  - `NODE_USE_ENV_PROXY=1` for Node's built-in fetch
- **Claude containers also get:**
  - `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`: telemetry would only be refused
  - `MCP_CONNECTION_NONBLOCKING=0`, because of the MCP race below
- **The MCP race** (verified with Claude Code 2.1.282):
  - In `-p` mode, Claude Code connects MCP servers in the background by default. Through the proxy this is slightly slower, and a live test saw the first turn start without the jira-mcp tools ("I don't have a tool called get_ticket").
  - The CLI reads `MCP_CONNECTION_NONBLOCKING`; set to `0`, it waits for the servers, up to the MCP connect timeout, before the first turn.
- `sandbox.egress_proxy: false` turns all of this off for debugging, with a warning at `codeit up`.

## Consequences

- Containers can no longer reach arbitrary hosts, which limits where a prompt-injected agent can send the Claude token (PRD 19.7). Allowlisted channels remain, as the PRD notes.
- A new host a target repo needs, such as a package mirror, must be added to `sandbox.egress_allowlist`.
- After a crash, recovery takes up to about 4 minutes: 3 for the heartbeat to count as stopped, plus one poll.
- The Claude concurrency limit now also covers manual runs.

## Live acceptance (2026-09-29)

- **Egress** (`tests/live/test_egress_live.py`), in the real worker image:
  - allowed: npm and GitHub, jira-mcp's port on the host
  - refused: other sites, another port on the host, and a request that bypasses the proxy
  - Claude Code reaches its API and jira-mcp through the proxy (`test_sandbox_live.py`)
- **Orchestrator:** see the work log for 2026-09-29, session 10.
