# CodeIt: Project Plan

## Context

`PRD.md` (Draft v1.0) specifies **CodeIt**: a local multi-agent delivery system that uses Jira as the state machine.
- Agents: Planner, Coder, Reviewer, Rebase, Docs and Learning.
- Supporting parts: Docker sandboxes, a hand-built Jira MCP server, an eval harness and a Next.js dashboard.

Nothing exists yet besides the PRD: no repo, folders, git history or dashboard. The project was renamed from "Forge", and traces of the old name remain.

This plan covers:
- making the environment ready for development
- cleaning up the PRD (rename plus fixes for the gaps found)
- building the system milestone by milestone

Following PRD rule 0.1, the owner confirms each milestone before the next one starts.

**Decisions made with the owner:**
- Lowercase `codeit` for all identifiers.
- The repo lives at `~/projects/codeit` on the WSL ext4 filesystem.
- Docker comes from Docker Desktop with WSL integration.
- A minimal sandbox app lands in a new M4.5, before the Coder.
- Per-ticket local clones replace git worktrees.
- Dashboard: Next.js as in PRD D3. It is built from scratch in M8.

## Environment findings (2026-09-24)

| Item | State | Action |
|---|---|---|
| WSL2 | Ubuntu 24.04, kernel 6.6, systemd on, 24 cores, 15 GB RAM, 953 GB free | OK |
| Docker | Desktop 29.4.1 installed on Windows. Engine **not running**. WSL integration **off** | Owner starts Desktop and enables Settings > Resources > WSL Integration > Ubuntu-24.04. Verify with `docker run hello-world` from WSL |
| Python | 3.12.3 | OK. Install `uv` |
| Node | Missing in WSL. Only the Windows `npm`/`pnpm` shims are on PATH | Install Node LTS via `nvm` in WSL so the Linux binaries take precedence |
| gh, jq, claude | Missing in WSL | Install `gh` (apt repo), `jq` (apt) and Claude Code (native installer). Owner runs `gh auth login` and `claude setup-token` |
| Project dir | `/mnt/c/.../Hustle/CodeIt` (Windows FS) | Move to `~/projects/codeit` |

## Owner prerequisites (blocking only the milestone listed)

- **Before M1:** Jira site and project are ready. Still needed:
  - create the project with key `CODEIT`
  - finish the workflow, statuses, fields and board estimation from PRD 7.1
  - create a bot account plus API token (D4). Until it exists, M1 can run on the owner's token (the D4 alternative), recorded in an ADR.
- **Before M0 push / CI:** a GitHub repo `codeit`.
- **Before M4.5:** a GitHub repo `codeit-sandbox-app` and fine-grained PATs (agent: push branches; readonly), scoped to the target repo.
- **Before M3/M6:** OpenRouter per-role keys with credit limits (the account is ready). Fill in model IDs in `config.yaml`.
- **Before M4:** a Claude OAuth token from `claude setup-token`.

## Phase A: Environment and repo bootstrap (first execution step)

1. Owner sets up Docker Desktop's WSL integration. Then I verify `docker version` and `docker compose version` from WSL.
2. Install into WSL:
   - `uv` (astral installer)
   - `nvm` and Node LTS
   - `gh`, `jq`, Claude Code CLI
3. Create `~/projects/codeit`, `git init -b main`, and copy `PRD.md` to `docs/prd.md`.
4. Leave the original in `/mnt/c`, or delete it after the owner confirms.
5. Git conventions:
   - `main` stays protected once it's on GitHub
   - one branch per milestone (`m0-scaffold`, `m1-jira-client`, ...)
   - commit messages `M0: ...`
   - merge by PR

## Phase B: PRD cleanup (edits to `docs/prd.md`)

**Rename map:**

| Old | New |
|---|---|
| `forge <cmd>` (all ~27 CLI references, including `forge jira discover` in section 0) | `codeit <cmd>` |
| `forge-sandbox-app` | `codeit-sandbox-app` |
| `jira_project_key: FORGE` | `CODEIT` |
| `CodeIt_API_TOKEN`, `CodeIt_ROLE`, `CodeIt_TICKET` | `CODEIT_API_TOKEN`, `CODEIT_ROLE`, `CODEIT_TICKET` |
| `CodeIt-worker:{version}` | `codeit-worker` (Docker requires lowercase) |
| `CodeIt-mcp` | `codeit-mcp` |
| `src/CodeIt/` | `src/codeit/` |
| `data/CodeIt.db` | `data/codeit.db` |
| `CodeIt.jsonl` | `codeit.jsonl` |
| `CodeIt.yaml` | `codeit.yaml` |
| `<!-- CodeIt:status:* -->` | `<!-- codeit:status:* -->` |
| `CodeIt-bot` | `codeit-bot` |
| Typer entrypoint "forge" | "codeit" |

Also:
- Remove the "Working name ... placeholder" line.
- Change the SSH repo URL in 20.1 to HTTPS, since token auth is used.
- Keep "CodeIt" in prose.

**Content fixes, each also recorded as an ADR:**
- **ADR-0001:** rename and identifier casing.
- **ADR-0002:** per-ticket local clone instead of `git worktree`. Rewrite 10.2:
  - `git clone` (hardlinks by default on a local path) from the mirror at `data/repos/{repo}` into `data/worktrees/{repo}/{KEY}`, then set `origin` to the GitHub URL.
  - The clone has a self-contained `.git`, so it works when bind-mounted.
- **ADR-0003:** add milestone **M4.5 (Minimal sandbox app)** to section 23 and D8. M9 then extends it with golden tasks.
- **Clarification in 6.2:** `In Dev` with an expired lease and retries ≥ 2 goes to `Human Review` + `needs-human`.
- **Note D4:** until the bot account exists, M1 may use the owner's token.

## Milestone roadmap

Each milestone is merged to `main` and passes CI. Each one has updated README usage, a worklog entry and ADRs as needed (PRD 23 DoD). I stop after each milestone for owner confirmation.

| # | Key deliverables | Verification |
|---|---|---|
| **M0 Scaffold** | See detail below | `uv run codeit --help`. `codeit config validate` rejects a malformed fixture. `pytest`, `ruff` and `mypy --strict` pass. CI green |
| **M1 Jira client** | `src/codeit/jira_client/` with client, adf, search, issues, transitions, comments, discover and models. `codeit jira doctor` and `codeit jira discover` write `config/jira_ids.yaml` | `respx` unit tests: ADF round-trip, pagination with a repeated-token guard, retry/`Retry-After`. `LIVE=1` doctor and transition-to-each-status tests pass on the owner's site |
| **M2 jira-mcp** | `mcp_servers/jira/` FastMCP server, role-filtered tool registration, `CODEIT_TICKET` restriction, stdio + streamable HTTP | Per-tool unit tests, and a test that disallowed tools are absent for each role. Documented Inspector smoke test. `get_ticket` works from interactive Claude Code |
| **M3 Planner** | `backends/openrouter_chat.py`, the `claude_code` chat mode, `agents/planner.py`, `prompts/planner/`, the `codeit plan` command with `--dry-run` | Sample plan with 5+ features creates linked `Agent Draft` tickets. Dry-run output matches created tickets. Invalid JSON is retried once |
| **M4 Sandbox + Claude backend** | `sandbox/Dockerfile` (Playwright base, Python, gh, claude, opencode, codeit-mcp, user `agent` UID 1000), `sandbox/docker.py`, `sandbox/clone.py`, `backends/claude_code.py` (stream-json parsing, transcripts, usage-limit parking), `codeit sandbox build` and `gc` | Container runs `claude -p "echo hello via bash"` and the transcript is saved. Simulated limit output parks the backend. Unit test: `--dangerously-skip-permissions` is never used on the host |
| **M4.5 Sandbox app** | `codeit-sandbox-app`: Vite + React + TS, Express + TS API, SQLite, Vitest, Playwright, `ci.yml`, `codeit.yaml`. The steering kit is added in M5 by `codeit init-target` | CI green on GitHub. `npm ci && npm test && npx playwright test` pass locally and in the worker image |
| **M5 Coder (manual)** | `agents/coder.py`, `templates/target/` (CLAUDE.md, 5 skills, hooks, codeit.yaml), `templates/pr_body.md`, the `codeit init-target` and `codeit run coder KEY` commands, result handling, `--file ticket.md` mode (PRD open question 1) | A 1 to 2 point ticket yields a PR with CI passing, and the ticket reaches `Agent Review` with PR URL set. Rework updates the same PR. The Stop hook blocks on a failing test and respects `stop_hook_active` |
| **M6 Reviewer** | `agents/reviewer/checks.py` (install/lint/typecheck/unit/e2e/`new_tests_fail_on_base`/ci_status), Phase 2 verdict, override rules, PR review + Jira comment, loop routing | A failing PR returns to `Ready for Dev` with findings. The 3rd failure escalates with `needs-human`. A tautological test is flagged critical |
| **M7 Orchestrator** | `orchestrator/` (scheduler, slots, leases + heartbeat + reaper, idempotent transitions, budget guard, run windows, merge watcher), tinyproxy egress allowlist, `codeit up`, `codeit budget`, `codeit agents` | Two tickets reach `Human Review` unattended. Killing the process and restarting recovers. A manual move mid-run causes no conflicting transition. A merge moves the ticket to `Done` |
| **M8 API + Dashboard** | FastAPI endpoints (PRD 14) + SSE with bearer auth on 127.0.0.1. `dashboard/` Next.js App Router + TS strict + Tailwind + shadcn/ui + TanStack Query, with pages Agents, Pipeline, Run detail and Budgets | Agent state updates in under 2 s. Transcripts stream. Slot changes from the UI apply on the next loop. Dashboard lint and build run in CI |
| **M9 Evals** | 12 golden tasks (hidden tests, reference patches), `evals/runner.py`, scorers (pass@1, pass^k, ...), `codeit eval run` and `report`, seeded-bug suite, Evals page | Reference patches pass the hidden tests. Base commits fail. `--repeats 3` reports pass@1 and pass^3 |
| **M10 Rebase** | Poller, clean path, conflict resolution, escalation rules | A forced conflict is resolved with tests green, or escalated when it touches a `never_auto` path |
| **M11 Docs** | Changelog, worklog, ADR detection, README status block, `docs-logged` label | PRD 11.6 acceptance |
| **M12 Learning** | Signal collection, classification, clustering, steering PRs, eval gate | PRD 11.7 acceptance |
| **M13 Fallback** | `backends/opencode.py` (flags verified then), `allow-fallback` routing, comparison eval | PRD M13 acceptance |

M10 to M13 can be cut without breaking the core flow (PRD risk table).

## M0 detail (first code milestone)

**Layout:** as in PRD 21, with the renames applied.
- `pyproject.toml` (uv) sets `[project.scripts] codeit = "codeit.cli:app"`.
- `src/codeit/`:
  - `cli.py` (Typer with stub subcommands: jira, plan, run, sandbox, eval, budget, agents, config)
  - `config.py` (Pydantic models for PRD 20.1 plus a `.env` loader)
  - `logging.py` (structlog JSON to `data/logs/codeit.jsonl`)
  - `db/models.py` (SQLAlchemy 2 models for all tables in PRD 13) plus the Alembic baseline, with SQLite in WAL mode
- Also: `config/config.yaml`, `.env.example`, `.gitignore` (`data/`, `.env`), gitleaks pre-commit, `.github/workflows/ci.yml` (ruff, mypy, pytest without live tests), the `docs/adr/` template, ADRs 0001 to 0003, and `docs/worklog/2026-09-24.md`.
- `tests/unit/` covers config validation and the DB migration.
- **Empty placeholders** with a README each: `dashboard/`, `evals/`, `prompts/`, `templates/`, `sandbox/`, `mcp_servers/jira/`. These make the structure visible from day one.

## Verification (overall)

- **Per milestone:** `uv run pytest` (unit), `uv run ruff check`, `uv run mypy --strict src/`, and CI green on the PR.
- **Live tests:** `LIVE=1 uv run pytest -m live` against the owner's Jira, GitHub and OpenRouter, from M1 on.
- **End-to-end after M8:**
  1. `codeit plan sample.md`
  2. approve tickets in Jira
  3. `codeit up`
  4. watch the dashboard as tickets reach `Human Review`
  5. merge the PR and confirm the ticket moves to `Done`

## Scope of the first execution session

Phase A (environment), Phase B (PRD cleanup), then M0. I stop for owner confirmation before M1.
