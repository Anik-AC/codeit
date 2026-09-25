# 0009. Worker sandbox implementation

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

M4 builds the worker image, the per-ticket clone manager, the container runner and the agentic Claude Code backend (PRD 10, 9.2). ADR-0004 left one open question: how containers reach the host-side jira-mcp. Building M4 settled that and several smaller details.

## Decision

### Image (`sandbox/Dockerfile`)

- Base: `mcr.microsoft.com/playwright:v1.63.0-noble` (Node 24, browsers, Python 3.12, git). Target repos must pin `@playwright/test` 1.63.0 to match.
- Adds `gh` (GitHub apt repo), `jq`, `uv`, Claude Code 2.1.282 and OpenCode 1.18.32, all pinned with build args.
- **User:** Ubuntu's `ubuntu` user (UID 1000) is renamed to `agent`, with its supplementary groups removed (it came with `sudo` and `adm`). The owner's WSL user is also UID 1000, so files written to the mounted clone belong to the owner.
- **Git identity:** set system-wide to `codeit-bot`. Pushes authenticate through `gh auth git-credential` with `GH_TOKEN`.
- `DISABLE_AUTOUPDATER` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` keep Claude Code from reaching hosts beyond the API.

### Containers (`sandbox/containers.py`, not `docker.py`)

- The module is named `containers.py` so it does not shadow the `docker` package.
- **Lifecycle** uses the Docker SDK:
  - one container per run, running `sleep infinity`, as `1000:1000`
  - every capability dropped, `no-new-privileges`, `init`
  - CPU, memory and PID limits from config
  - labelled with the run ID
- **Agent runs** use `docker exec -i` through asyncio. That gives line-by-line streaming and a clean kill on timeout. On a timeout the caller removes the container, which stops the process inside.
- **Secrets** go into the container's environment at creation, never on a command line.
- **Mounts:** the clone at `/workspace` and the run directory at **`/run/codeit`**, not `/run` (PRD 10.3), so the image's own `/run` is left alone. The container's MCP config is `/run/codeit/mcp.json`.
- **Network:** a dedicated `codeit-sandbox` network. It has unrestricted egress for now; the allowlisting proxy lands in M7, as PRD 10.3 allows.

### Reaching jira-mcp (closes ADR-0004's open question)

- Tested on Docker Desktop 4.71 with WSL2 in NAT mode: a container reaches a service bound to **127.0.0.1 inside WSL** through `host.docker.internal`.
- So jira-mcp keeps binding to loopback, not reachable from the LAN, and containers use `http://host.docker.internal:8765/mcp`.
- The sandbox also maps `host.docker.internal` to `host-gateway`, so the same name works on Linux Docker.
- `host.docker.internal:*` is added to the allowed `Host` headers.
- A live test runs a real agent in a container, with only a coder run token, that reads a ticket through this path.

### Clones (`sandbox/clone.py`, `git.py`)

- **The mirror** `data/repos/{repo}` is a normal clone kept at `origin/{default}`, not a bare repo, because the Planner reads its `CLAUDE.md` and file tree.
- **Bot identity:** every ticket clone gets `user.name`/`user.email` set to the bot. Host-side rebases then create bot commits and do not depend on the owner's global git identity. The owner's machine has none, which is how this was found.
- **Conflicts:** a rework rebase counts as a conflict only when git reports conflicting files. Other rebase failures are raised, not reported as conflicts.
- **GitHub token:** passed to host-side git through `GIT_CONFIG_*` environment variables, never in argv or `.git/config`, which is mounted into containers.

### Agentic Claude Code (`backends/claude_code_agent.py`)

- **Separate module:** this module is the container adapter and the only place `--dangerously-skip-permissions` appears. Its commands run only through an `Executor`, which execs into a container. A unit test checks that the module never starts host processes, and another checks that the host chat module never mentions the flag.
- **Transcripts:** every stream-json line is appended to `data/transcripts/{run_id}.jsonl` as it arrives, after a first `codeit_request` line with the argv and prompt.
- **Usage limits:**
  - Claude Code emits `rate_limit_event`s with a status, an exact `resetsAt` and window utilization. A failed run whose last event is not `allowed` parks the backend until `resetsAt`.
  - The text patterns remain a fallback.
  - Parking is saved in `backend_state`, so it survives restarts.
  - The last event is kept on the result for the M7 budget guard.

### CLI

- `codeit sandbox build` wraps `docker build`, for BuildKit and live progress.
- `codeit sandbox smoke` runs the M4 acceptance check.
- `codeit sandbox gc [--days 14] [--containers]` removes clones of Done, Rejected or deleted tickets, and old clones. With `--containers`, it also removes leftover worker containers.

## Consequences

- The Coder (M5) gets its workspace, container, run token, MCP config and agent run from pieces that are each tested, including live.
- Until M7, a prompt-injected agent has unrestricted egress. Only run tickets the owner wrote.
- Upgrading Claude Code or Playwright means changing the build args and rebuilding. Target repos must follow the Playwright pin.
