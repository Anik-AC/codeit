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
| M1 Jira client | In review (live checks pending) |
| M2 to M13 | Planned (see PRD section 23) |

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

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                     # unit tests; live tests are skipped
LIVE=1 uv run pytest -m live      # hits real Jira / GitHub / OpenRouter

Live Jira tests create issues labeled `codeit-live-test` and delete them afterwards. They need
the Jira setup above.
```

**Schema changes:** edit `src/codeit/db/models.py`, then run `uv run alembic revision --autogenerate -m "..."`. `tests/unit/test_db.py` fails if the models and migrations drift apart.

## Layout

| Path | Contents |
|---|---|
| `src/codeit/` | CLI, config, logging, db; orchestrator, agents, backends and clients land here per milestone |
| `src/codeit/jira_client/` | Async Jira client: retries, ADF, search, issues, transitions, comments, doctor, discover |
| `config/` | `config.yaml`, plus `jira_ids.yaml` written by `codeit jira discover` |
| `mcp_servers/jira/` | jira-mcp server (M2) |
| `prompts/`, `templates/` | Agent prompts and the target repo steering kit |
| `sandbox/` | Worker image and egress proxy |
| `evals/` | Eval suites and rubrics |
| `dashboard/` | Next.js dashboard (M8) |
| `docs/` | PRD, ADRs, work log |
| `data/` | Runtime state (gitignored) |
