# CodeIt

A local multi-agent software delivery system for personal projects:
- A Planner turns a plan into Jira tickets.
- A Coder implements approved tickets in isolated Docker containers and opens PRs.
- A Reviewer runs checks and can send work back.
- A human reviews and merges.

Jira is the state machine. See [docs/prd.md](docs/prd.md) for the full spec and [docs/adr/](docs/adr/) for decisions.

<!-- codeit:status:start -->
_Metrics appear here once the Docs agent runs (M11)._
<!-- codeit:status:end -->

## Status

| Milestone | State |
|---|---|
| M0 Scaffold | Done |
| M1 Jira client | Done |
| M2 jira-mcp | Done |
| M3 Planner | Done |
| M4 Sandbox + Claude backend | Done |
| M4.5 Sandbox app ([codeit-sandbox-app](https://github.com/Anik-AC/codeit-sandbox-app)) | Done |
| M5 Coder | In review |
| M6 to M13 | Planned (see PRD section 23) |

## Requirements

- Linux, macOS or Windows with WSL2. On Windows, keep the repo on the WSL filesystem (for example `~/projects/codeit`), not under `/mnt/c`.
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node LTS (for the dashboard and target apps). In WSL, install through `nvm` so the Linux `node` is used instead of the Windows one.
- Docker. On Windows, use Docker Desktop with **Settings > Resources > WSL Integration** enabled for your distro.
- `git`, `gh`, `jq`, and the Claude Code CLI

## Setup

```bash
uv sync
cp .env.example .env              # fill in credentials as milestones need them
uv run pre-commit install
uv run codeit config validate     # checks config/config.yaml
uv run codeit db upgrade          # creates data/codeit.db
```

## Usage

```bash
uv run codeit --help
uv run codeit config validate [-c path/to/config.yaml]
uv run codeit db upgrade
```

Commands for later milestones (`plan`, `run`, `sandbox`, `up`, `eval`, ...) are registered already. Until then they exit with a message naming their milestone.

## Jira setup

1. Set up the project as described in [PRD 7.1](docs/prd.md#71-one-time-manual-setup-owner): a team-managed project with key `CODEIT` (or whatever `project.jira_project_key` says), the statuses, the custom fields on the Story work type, and board estimation.
2. Fill in `.env`:

   ```bash
   JIRA_BASE_URL=https://<your-site>.atlassian.net
   JIRA_EMAIL=<the account's email>
   JIRA_API_TOKEN=<classic API token for that account>
   ```

   Create the token at https://id.atlassian.com/manage-profile/security/api-tokens. Until a bot account exists, your own account is fine (ADR-0001).
3. Check the setup, then record the site's IDs:

   ```bash
   uv run codeit jira doctor               # read-only checks, with a hint for each failure
   uv run codeit jira doctor --write-test  # also creates and deletes a test issue
   uv run codeit jira discover             # writes config/jira_ids.yaml (gitignored)
   ```

   Rerun `discover` after changing fields, statuses or work types in Jira.

## jira-mcp

The MCP server agents use to read tickets and comment. It runs on the host and holds the Jira token. What a caller sees depends on its role:

| Role | Tools |
|---|---|
| human (you, over stdio) | all six: `get_ticket`, `get_comments`, `search_tickets`, `add_comment`, `create_issue`, `link_issues` |
| planner | all but `add_comment` |
| learning | `get_ticket`, `get_comments`, `search_tickets` |
| coder, reviewer, rebase | `get_ticket`, `get_comments`, `add_comment` (own ticket only) |
| docs | `get_ticket`, `get_comments` |

No role can change a ticket's status. Keys and searches are limited to the configured project.

**Use it from interactive Claude Code** (stdio, role `human`). Run this once, from any directory:

```bash
claude mcp add codeit-jira --scope user -- ~/projects/codeit/.venv/bin/codeit-jira-mcp
```

Then ask Claude something like "get ticket CODEIT-2". Set `CODEIT_ROLE` in the server's environment to try another role, for example `claude mcp add ... -e CODEIT_ROLE=coder -- ...`.

**Smoke test with the MCP Inspector** (`-e` must come after the command):

```bash
npx @modelcontextprotocol/inspector --cli ~/projects/codeit/.venv/bin/codeit-jira-mcp --method tools/list
npx @modelcontextprotocol/inspector --cli ~/projects/codeit/.venv/bin/codeit-jira-mcp \
  --method tools/call --tool-name get_ticket --tool-arg key=CODEIT-2
npx @modelcontextprotocol/inspector --cli ~/projects/codeit/.venv/bin/codeit-jira-mcp \
  -e CODEIT_ROLE=coder --method tools/list        # shows only the coder's three tools
npx @modelcontextprotocol/inspector ~/projects/codeit/.venv/bin/codeit-jira-mcp   # browser UI
```

**HTTP with run tokens** (the path worker containers use from M4). Every request needs a bearer token bound to one role, one run and usually one ticket. Until the orchestrator mints them (M7), you can do it by hand:

```bash
uv run codeit mcp serve                     # http://127.0.0.1:8765/mcp (config: mcp:)
TOKEN=$(uv run codeit mcp token --role coder --ticket CODEIT-2)
cat > /tmp/mcp.json <<JSON
{"mcpServers": {"codeit-jira": {"type": "http", "url": "http://127.0.0.1:8765/mcp",
  "headers": {"Authorization": "Bearer $TOKEN"}}}}
JSON
claude -p "Use get_ticket for CODEIT-2 and give me its summary" \
  --mcp-config /tmp/mcp.json --strict-mcp-config --allowedTools mcp__codeit-jira__get_ticket
uv run codeit mcp revoke <run-id>           # the run ID is printed by `mcp token`
```

Tokens expire with the role's sandbox timeout plus `mcp.token_grace_minutes`. Only their SHA-256 is stored, in `data/codeit.db`.

## Planner

Turns a plan written in markdown into one Epic and a set of small Stories in Jira, all in `Agent Draft` with the label `agent-draft`. Stories that look too big (more than 5 points, or fewer than 2 acceptance criteria) also get `split-me`. Dependencies become "blocks" links. You then set priority and points and approve each Story (PRD 6).

```bash
uv run codeit plan docs/samples/sample-plan.md --dry-run   # draft and show; creates nothing
uv run codeit plan data/plans/<run>.json                   # create exactly that draft
uv run codeit plan my-plan.md                              # draft and create in one go
uv run codeit plan my-plan.md --epic CODEIT-40             # add Stories under an existing Epic
uv run codeit plan my-plan.md --epic "Q4 work"             # name the new Epic yourself
```

- **Backends:** the Planner uses Claude Code on your subscription (`claude -p` with every tool disabled). If Claude is unavailable, for example because of a usage limit, it falls back to OpenRouter's free models. That needs `OPENROUTER_KEY_OPS` in `.env`.
- **Repo context:** it reads the target repo's `CLAUDE.md` and file tree from `--repo` (default `data/repos/<target repo>`), and the open tickets in Jira.
- **Saved plans:** every plan is saved in `data/plans/` and recorded in the `runs` table with its prompt hash and estimated cost.
- **Invalid answers** are retried once with the validation errors.
- **Failures:** if Jira rejects any write, the run deletes what it created.

## Coder

Takes a ticket in `Ready for Dev` to a pull request (PRD 11.2):

```bash
uv run codeit run coder CODEIT-12            # claim, implement in a container, open or update the PR
uv run codeit run coder --file ticket.md     # local run: no Jira, the agent only commits in the clone
```

What a run does:

1. **Claims the ticket:** takes a lease, moves the ticket to `In Dev`, and sets `Agent` and `Run ID`.
2. **Prepares a workspace:** the ticket's clone on `{KEY}-{slug}`, or on the existing PR's branch for rework.
3. **Runs Claude Code in a worker container.** The container gets a coder run token for jira-mcp, and the prompt includes feedback since the last run (Jira comments and PR reviews).
4. **Applies the result.** If GitHub confirms the PR has new commits, the ticket gets `PR URL` and a remote link and moves to `Agent Review`. Otherwise it gets a comment and `needs-human`, and moves to `Human Review`. After a usage limit, it goes back to `Ready for Dev`.

It needs `CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN_AGENT` and `GITHUB_TOKEN_READONLY` in `.env`, the worker image, and the target repo's steering kit.

**Steering kit.** The Coder follows the target repo's own instructions. Add them once:

```bash
uv run codeit init-target ~/projects/my-app   # CLAUDE.md, .claude/skills/*, .claude/settings.json + hooks
```

Then fill in the placeholder sections of `CLAUDE.md` and commit. The Stop hook runs the unit tests and refuses to let the agent finish while they fail. The edit hook lints each changed file.

## Models and keys

- **OpenRouter:** one key for everything: `OPENROUTER_API_KEY` in `.env`. The older per-role names still work.
- **Model lists:** the defaults are in `config/config.yaml` under `models:`. To change them, for price or to compare models, set these in `.env`:

  ```bash
  OPENROUTER_MODELS_PAID_REVIEW=vendor/model-a,vendor/model-b   # tried in order
  OPENROUTER_MODELS_FREE=...          # also _PAID_CHEAP
  OPENCODE_MODEL=openrouter/vendor/coder-model
  CLAUDE_MODEL=claude-opus-5-5        # unset: Claude Code's default
  ```

- `uv run codeit config validate` shows which model each list uses, and whether it comes from `.env`.

## Target repo

CodeIt's agents work on [`codeit-sandbox-app`](https://github.com/Anik-AC/codeit-sandbox-app), a small task tracker that can only list tasks so far (ADR-0010). Its `codeit.yaml` lists the commands agents and the Reviewer run: install, lint, typecheck, unit, e2e. `codeit.target.load_target_config` reads it.

To check that the target repo passes all its commands inside the worker image:

```bash
LIVE=1 uv run pytest tests/live/test_target_repo_live.py        # set CODEIT_TARGET_BRANCH for a branch
```

## Worker sandbox

Agents that change code run in Docker containers built from `sandbox/Dockerfile`. The image is Playwright 1.63 plus `gh`, `jq`, `uv`, Claude Code and OpenCode, running as user `agent` (UID 1000).
- **Mounts:** each container sees only its ticket's clone at `/workspace` and its run directory at `/run/codeit`.
- **Hardening:** every capability is dropped, and there are no new privileges.
- **Credentials:** only those of its role. Never a Jira token; containers reach Jira through jira-mcp at `host.docker.internal` with a run token.

```bash
uv run codeit sandbox build     # build codeit-worker:<version> (tag from sandbox.image)
uv run codeit sandbox smoke     # run claude -p in a container; checks the token and Docker
uv run codeit sandbox gc        # remove clones of Done/Rejected tickets and clones over 14 days old
```

`sandbox smoke` needs `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (from `claude setup-token`). Transcripts go to `data/transcripts/<run>.jsonl` and container logs to `data/logs/containers/`. Per-ticket clones live in `data/worktrees/<repo>/<KEY>`, made from the mirror in `data/repos/<repo>`.

Until M7 adds the egress proxy, containers have unrestricted network access, so only run tickets you wrote.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                     # unit tests; live tests are skipped
LIVE=1 uv run pytest -m live      # hits real Jira / GitHub / OpenRouter

Live Jira tests create issues labeled `codeit-live-test` and delete them afterwards. They need
the Jira setup above. The jira-mcp and Planner live tests also call `claude -p` on your
subscription (skipped if `claude` is not installed); the Planner test creates a full plan in
Jira and deletes it.
```

**Schema changes:** edit `src/codeit/db/models.py`, then run `uv run alembic revision --autogenerate -m "..."`. `tests/unit/test_db.py` fails if the models and migrations drift apart.

## Layout

| Path | Contents |
|---|---|
| `src/codeit/` | CLI, config, logging, db; orchestrator, agents, backends and clients land here per milestone |
| `src/codeit/agents/` | Agents: `planner.py` and `planner_run.py` (`codeit plan`); `coder.py` and `coder_run.py` (`codeit run coder`) |
| `src/codeit/backends/` | Model adapters: Claude Code (chat on the host, agentic in containers), OpenRouter, routing, parking |
| `src/codeit/sandbox/` | Worker containers, per-ticket clones, run glue, garbage collection |
| `prompts/`, `templates/` | Versioned prompts per role; Jira description templates |
| `src/codeit/github_client/` | GitHub REST: PRs, feedback, check runs |
| `src/codeit/orchestrator/` | Leases (the scheduler loop lands in M7) |
| `src/codeit/target.py` | The target repo's `codeit.yaml` |
| `src/codeit/run_tokens.py` | Per-run jira-mcp tokens (mint, verify, revoke) |
| `src/codeit/jira_client/` | Async Jira client: retries, ADF, search, issues, transitions, comments, doctor, discover |
| `config/` | `config.yaml`, plus `jira_ids.yaml` written by `codeit jira discover` |
| `mcp_servers/jira/` | jira-mcp server: role-filtered tools, run-token auth |
| `sandbox/` | Worker image (`Dockerfile`); egress proxy in M7 |
| `evals/` | Eval suites and rubrics |
| `dashboard/` | Next.js dashboard (M8) |
| `docs/` | PRD, ADRs, work log |
| `data/` | Runtime state (gitignored) |
