# CodeIt: PRD

**Owner:** Onix (Anik Chakraborti)
**Status:** Draft v1.2 (renamed to CodeIt; host-side jira-mcp; see ADRs 0001 to 0004)
**Date:** 2026-09-25

---

## 0. Instructions for Claude Code

Read this section first. It governs how the system gets built.

1. **Build one milestone at a time** (Section 23). Do not start a milestone until the previous one meets its acceptance criteria and the owner has confirmed.
2. **Every milestone ships with tests.** External HTTP calls (Jira, GitHub, OpenRouter) are mocked in unit tests with `respx`. Live calls go only in tests marked `@pytest.mark.live`, which are skipped unless `LIVE=1` is set.
3. **Never hardcode Jira IDs** for fields, statuses or transitions. Discover them at runtime or through `codeit jira discover` (M1), and read them from config.
4. **Jira search uses `POST /rest/api/3/search/jql` with `nextPageToken` pagination.** The legacy `/rest/api/3/search` endpoint has been removed from Jira Cloud. Do not use libraries that call it.
5. **Jira v3 descriptions and comments use Atlassian Document Format (ADF).** All agent text is written as markdown and converted by `jira_client.adf`. Never send raw strings as description or comment bodies.
6. **Record architectural decisions** as ADRs in `docs/adr/NNNN-title.md`. Append a dated entry to `docs/worklog/` at the end of each working session.
7. **Ask the owner** when something in this PRD is ambiguous or contradicts itself. Do not guess on anything touching credentials, git history or Jira workflow state.
8. **Wrap every external CLI** (claude, opencode, gh, git, docker) in an adapter. If a CLI flag in this PRD turns out to be wrong for the installed version, fix it in the adapter and note it in an ADR.
9. **Writing style for generated docs and comments:** no em dashes.

---

## 1. Overview

CodeIt is a local multi-agent software delivery system for personal projects:

1. A **Planner** agent turns a project plan into Jira tickets.
2. The human approves each ticket and sets its priority and story points.
3. A **Coder** agent implements approved tickets in an isolated Docker container on its own git worktree, writes unit and Playwright tests, and opens a pull request.
4. A **Reviewer** agent runs all checks and can send the ticket back to coding.
5. The human does the final review: merge, or send back to coding or testing.
6. **Support agents** keep the pipeline healthy:
   - **Rebase:** resolves merge conflicts.
   - **Docs:** keeps a changelog and work log.
   - **Learning:** turns review feedback into improved skills and prompts, gated by evals.
7. A **dashboard** shows every agent, what it is working on, costs and eval results.

**The human's job is steering, not typing code:**
- writing skills, agent definitions, MCP integrations and hooks
- deciding what to build
- reviewing every line that reaches a PR

## 2. Goals and non-goals

### 2.1 Goals

- **G1:** End-to-end flow from `plan.md` to merged PR, with human gates at ticket approval and PR approval.
- **G2:** Jira Cloud Free is the source of truth for ticket state.
- **G3:** Near-zero running cost:
  - Claude Code runs on the owner's Claude Pro subscription.
  - OpenRouter is used for cheap and free models (one-time $10 credit purchase already made).
  - Jira Cloud Free.
  - GitHub Free.
- **G4:** Agent quality is measurable and reproducible through an evaluation harness.
- **G5:** A hand-built MCP server for Jira that agents actually use.
- **G6:** Agent capacity scales per role through configuration alone.
- **G7:** The project documents itself (changelog, work log, ADRs, metrics in the README).

### 2.2 Learning objectives (drive design choices)

- **L1:** Building MCP servers and tool integrations for LLM agents.
- **L2:** Agent evaluation: measuring quality and consistency, and iterating on prompts, skills and tooling.
- **L3:** Jira REST API and Jira Automation.

### 2.3 Non-goals

- Multi-user or hosted SaaS. This is a single-owner local tool.
- A generic agent framework. The Jira workflow is the state machine. Do not introduce LangGraph, CrewAI or similar.
- Auto-merging to `main`. A human merges every PR.
- Production deployment of target apps.

## 3. Decisions and assumptions

These are defaults. The owner can override any of them before the milestone that uses it.

| ID | Decision | Default | Change if |
|---|---|---|---|
| D1 | Host OS | Linux, macOS, or Windows via WSL2. All code, worktrees and Docker volumes live on the Linux filesystem in WSL2. | n/a |
| D2 | Languages | Python 3.12 (orchestrator, agents, MCP, evals); TypeScript (dashboard, sandbox target app) | n/a |
| D3 | Dashboard stack | Next.js (App Router) + TypeScript + Tailwind + shadcn/ui | Switch to HTMX served by FastAPI if speed matters more than polish |
| D4 | Jira identity | A dedicated bot Atlassian account (1 of 10 Free seats). The human uses their own account. | Use the owner's own token and tag actions via a field. Until the bot account exists, M1 and later may run on the owner's token (ADR-0001 notes this). |
| D5 | GitHub identity | A fine-grained PAT on the owner's account, with commits authored as `codeit-bot <bot@users.noreply.github.com>`. Machine user is optional. | Create a machine user so PRs can be formally approved |
| D6 | Claude fallback | When Claude Pro is exhausted, park Claude-backed work until the reset. Fall back to OpenCode + OpenRouter only for tickets labeled `allow-fallback`. | Enable fallback globally |
| D7 | Event delivery | Poll Jira and GitHub. No webhooks. | Add a Cloudflare Tunnel + webhooks later |
| D8 | Target repos | (a) `codeit-sandbox-app`, a minimal version built in M4.5 and extended with golden tasks in M9 (ADR-0003); (b) any real repo configured in `config.yaml` | n/a |
| D9 | Persistence | SQLite (WAL mode) through SQLAlchemy 2.0 + Alembic | n/a |

## 4. Users and roles

- **Owner (human):**
  - writes plans
  - approves or rejects tickets and sets priority and story points
  - reviews and merges PRs, or sends them back
  - edits skills and prompts
  - reviews Learning and Docs PRs
- **Agents:** Planner, Coder, Reviewer, Rebase, Docs, Learning.
- **Orchestrator:** a deterministic service. It claims work, launches agents, enforces rules and records everything.

## 5. System architecture

### 5.1 Components

```
HOST (owner's machine)
├── orchestrator      Python/FastAPI. Poll loop, scheduler, leases, budgets, REST + SSE API
├── agents/           Python modules, one per role, invoked by the orchestrator
├── backends/         LLM backend adapters (claude_code, opencode, openrouter_chat)
├── jira_client/      Shared Jira library (used by the orchestrator and jira-mcp)
├── github_client/    Thin wrapper over gh CLI + GitHub REST
├── mcp_servers/jira  FastMCP server exposing jira_client tools to agents. Runs on the host
│                     over streamable HTTP; the only holder of the Jira token (Section 15)
├── dashboard/        Next.js app, reads the orchestrator API
├── evals/            Golden tasks, runner, scorers
└── SQLite DB         data/codeit.db

WORKER CONTAINERS (one per active agent job, created from sandbox/Dockerfile)
├── /workspace        bind mount of the ticket's git worktree
├── Claude Code CLI   auth via CLAUDE_CODE_OAUTH_TOKEN
├── OpenCode CLI      auth via OpenRouter key (fallback only)
├── gh CLI            auth via a branch-only GitHub token
├── Node LTS, Playwright browsers, Python
└── /run/mcp.json     points at the host jira-mcp endpoint with a short-lived run token.
                      No Jira credential is present in the container.

REMOTE: Jira Cloud Free · GitHub · OpenRouter · Anthropic (via Pro login)
```

### 5.2 Trust boundaries

- **The host holds all credentials:** Jira bot token, GitHub PAT, OpenRouter keys, Claude OAuth token.
- **The Jira token never leaves the host.** Agents reach Jira only through the host-side jira-mcp server, authenticated with a per-run token bound to one role and one ticket (Section 15, ADR-0004).
- **Worker containers are untrusted.** They run LLM-driven shell commands on input derived from ticket text.
- **Containers receive only what their role needs** (see Section 19).
- **The orchestrator is the only component that transitions Jira statuses.** Agents return a result, and the orchestrator applies the transition. This keeps the state machine in one place and testable.

### 5.3 Why Jira is the state machine

Jira already provides:
- statuses and transitions
- history
- a UI the human uses to approve work

The orchestrator is a thin, deterministic layer on top. That gives one source of truth, and the human can intervene through the normal Jira UI at any point.

## 6. Ticket lifecycle

### 6.1 Statuses

| Status | Meaning | Who moves it out |
|---|---|---|
| `Agent Draft` | Created by the Planner, awaiting human review | Human |
| `Rejected` | Human rejected the draft (a comment with the reason is required) | Terminal |
| `Ready for Dev` | Approved and ready for the Coder, or returned for rework | Orchestrator (claim) |
| `In Dev` | Coder is working on it | Orchestrator |
| `Agent Review` | PR open, Reviewer runs or will run | Orchestrator |
| `Human Review` | Awaiting the owner | Human, or the orchestrator on merge |
| `Done` | PR merged | Terminal |

### 6.2 Transitions

| From | To | Trigger | Actor |
|---|---|---|---|
| (new) | Agent Draft | Planner creates the ticket | Orchestrator on Planner result |
| Agent Draft | Ready for Dev | Human approves (after setting priority and points) | Human |
| Agent Draft | Rejected | Human rejects with a comment | Human |
| Ready for Dev | In Dev | Coder slot free, dependencies Done, lease acquired | Orchestrator |
| In Dev | Agent Review | Coder result `pr_opened` | Orchestrator |
| In Dev | Human Review | Coder result `failed` or `blocked` (adds label `needs-human`) | Orchestrator |
| In Dev | Ready for Dev | Lease expired (crash recovery), retry count below 2 | Orchestrator |
| In Dev | Human Review | Lease expired and retry count is 2 or more (adds `needs-human`) | Orchestrator |
| Agent Review | Ready for Dev | Reviewer verdict `fail_critical` and `Review Loop` below `max_review_loops` (default 3) | Orchestrator |
| Agent Review | Human Review | Verdict `pass` or `pass_with_notes`, or loop cap reached (adds `needs-human`) | Orchestrator |
| Human Review | Done | Orchestrator detects the PR merged | Orchestrator |
| Human Review | Ready for Dev | Human sends back to coding | Human |
| Human Review | Agent Review | Human sends back to testing | Human |

**Rules:**

1. **Re-entry context.** When a ticket re-enters `Ready for Dev` or `Agent Review`, the orchestrator collects every human and reviewer comment since the last agent run on that ticket (Jira comments + PR review comments). It passes them to the agent as `feedback`.
2. **Coder rework reuses the same branch and PR.** It never opens a second PR for a ticket.
3. **`Review Loop` counts only Reviewer bounces.** Human send-backs increment the separate field `Human Returns`.
4. **Manual Done without a merge.** If a ticket reaches `Done` while its PR is not merged, the orchestrator adds label `state-mismatch` and surfaces it on the dashboard. It takes no other action.
5. **Blocked tickets are skipped.** A ticket with an unresolved `is blocked by` link to a ticket that isn't `Done` is not claimed.

### 6.3 JQL per role

All queries are templated from config. `{P}` is the project key.

| Role | JQL | Order |
|---|---|---|
| Coder | `project = {P} AND status = "Ready for Dev"` | Priority DESC, then created ASC |
| Reviewer | `project = {P} AND status = "Agent Review"` | updated ASC |
| Merge watcher | `project = {P} AND status = "Human Review"` | n/a |
| Docs | `project = {P} AND status = Done AND labels != "docs-logged"` | n/a |
| Learning | `project = {P} AND (status = Rejected OR "Human Returns" > 0) AND updated >= -7d` | n/a |

## 7. Jira integration

### 7.1 One-time manual setup (owner)

1. Invite a bot account (e.g. `onix.agents@...`) to the site and grant it access to the project.
2. As the bot, create a **classic API token** at `https://id.atlassian.com/manage-profile/security/api-tokens`. Note its expiry date.
3. **Workflow (team-managed project):**
   - create the statuses in 6.1
   - set every status to allow transitions from all statuses
4. **Board settings:** enable estimation. This provides the "Story point estimate" field.
5. **Add custom fields to the Story work type:**

| Field | Type |
|---|---|
| `Agent` | Short text |
| `Review Loop` | Number |
| `Human Returns` | Number |
| `PR URL` | URL |
| `Run ID` | Short text |

6. **Create labels on first use:** `agent-draft`, `needs-human`, `allow-fallback`, `docs-logged`, `state-mismatch`, `split-me`.
7. **Install "GitHub for Jira"** (free) and connect the target repo.
8. **Create two Jira Automation rules** (learning objective L3; keep total runs well under 100/month):
   - **R1:** when a ticket transitions to `Human Review`, email the owner.
   - **R2:** when a ticket transitions to `Rejected` and has no comment in the last 5 minutes, add label `needs-reason`.

### 7.2 `jira_client` library

**Package:** `jira_client/`. Async, built on `httpx`.

**Auth and connection:**
- HTTP Basic with `JIRA_EMAIL:JIRA_API_TOKEN`.
- Base URL `JIRA_BASE_URL`.
- Timeout: 30 seconds.

**Retries:**
- On 429 and 503, honor `Retry-After`.
- Otherwise use exponential backoff with jitter.
- Maximum 5 attempts.

**Modules:**

| Module | Responsibility |
|---|---|
| `client.py` | Session, retries, error types (`JiraAuthError`, `JiraNotFound`, `JiraConflict`, `JiraRateLimited`) |
| `adf.py` | `markdown_to_adf(md) -> dict` and `adf_to_markdown(adf) -> str`. Supports headings, paragraphs, bold, italic, inline code, code blocks with language, bullet lists, ordered lists, links, block quotes. Unsupported nodes degrade to plain text. |
| `search.py` | `search(jql, fields, max_results)` over `POST /rest/api/3/search/jql`, as an async generator following `nextPageToken`. Guard against endless pagination: stop if a token repeats or after 50 pages. |
| `issues.py` | `get_issue`, `create_issue`, `bulk_create`, `update_fields`, `add_labels`, `remove_labels`, `link_issues(blocker, blocked)`, `add_remote_link(key, url, title)` |
| `transitions.py` | `transition_to(key, status_name)`. Resolves the transition ID from the cached map. If the map misses, it refetches `GET /issue/{key}/transitions` once. Raises `JiraConflict` if the target isn't reachable. |
| `comments.py` | `add_comment(key, markdown)`, `list_comments(key, since)` |
| `discover.py` | Builds and writes the ID map (7.3) |
| `models.py` | Pydantic models: `Ticket`, `Comment`, `Transition`, `FieldMap` |

**The `Ticket` model**, normalized from Jira fields:
- key, summary, `description_md`, status, priority, `story_points`
- labels, `agent`, `review_loop`, `human_returns`, `pr_url`, `run_id`
- `blocked_by: list[str]`, `epic_key`, created, updated

### 7.3 Discovery CLI

**`codeit jira doctor`** checks, in order:
1. auth (`GET /rest/api/3/myself`)
2. the project exists
3. all required statuses exist
4. all required custom fields exist
5. the bot can create and delete a test issue (only with `--write-test`)

It prints a pass/fail table with remediation hints.

**`codeit jira discover`** reads:
- `GET /rest/api/3/field`
- `GET /rest/api/3/project/{P}/statuses`
- transitions from one sample issue

It writes `config/jira_ids.yaml`:

```yaml
fields:
  story_points: customfield_10016
  agent: customfield_10042
  review_loop: customfield_10043
  human_returns: customfield_10044
  pr_url: customfield_10045
  run_id: customfield_10046
statuses:
  Agent Draft: "10010"
  # ...
issue_types:
  Epic: "10000"
  Story: "10001"
```

## 8. GitHub integration

**Package:** `github_client/`. Uses the `gh` CLI for PR creation inside containers and GitHub REST (via `httpx`) on the host.

**Host-side functions:**

| Function | Purpose |
|---|---|
| `get_pr(number)` | Returns merged, mergeable, `mergeable_state`, head SHA, base SHA |
| `list_open_agent_prs()` | Open PRs whose head branch matches `^[A-Z]+-\d+-` |
| `list_reviews(pr)` and `list_review_comments(pr)` | Human and agent review content |
| `post_review(pr, body_md, event, comments[])` | Posts a review. The Reviewer uses `COMMENT`. `REQUEST_CHANGES` is not allowed on your own PRs when using the owner's PAT, so default to `COMMENT` and record the verdict in the body. |
| `get_check_runs(sha)` | CI status for the PR head |

**Conventions:**
- **Branch:** `{KEY}-{slug}`, where the slug is the summary lowercased with non-alphanumerics replaced by `-` and cut to 40 characters.
- **PR title:** `{KEY}: {summary}`.
- **PR body template:** stored at `templates/pr_body.md`. Sections: Summary, Acceptance criteria checklist, Changes, Tests added, Test results, Notes for reviewer.

**Repo settings (owner, one-time, documented in README):**
- `main` is protected: PR required, CI must pass, no force push.
- **CI workflow** `ci.yml` in the target repo runs install, lint, type check, unit tests and Playwright.

## 9. Model backends and routing

### 9.1 Backend interface

```python
class Backend(Protocol):
    name: str
    kind: Literal["agentic", "chat"]
    async def available(self) -> Availability          # ok | parked(until) | disabled
    async def run_agentic(self, req: AgenticRequest) -> AgenticResult   # agentic only
    async def complete(self, req: ChatRequest) -> ChatResult             # chat only
```

- **`AgenticRequest`:**
  - `prompt`
  - `system_append`
  - `workdir` (container path)
  - `container_id`
  - `max_turns`
  - `allowed_tools`
  - `mcp_config_path`
  - `timeout_s`
  - `model` (optional)
  - `run_id`
- **`AgenticResult`:**
  - status: `completed | max_turns | timeout | usage_limited | error`
  - `final_message`
  - `transcript_path`
  - `turns`
  - `input_tokens`, `output_tokens`
  - `cost_usd` (estimated or reported)
  - `reset_at` (when usage-limited)
- **`ChatRequest` / `ChatResult`:**
  - messages
  - optional JSON schema for structured output
  - model list for fallback
  - usage and cost

### 9.2 Backends

**`claude_code`** (agentic)

Runs inside the worker container:

```
claude -p "<prompt>" --output-format stream-json --verbose \
  --max-turns N --mcp-config /run/mcp.json \
  --allowedTools "<list>" --append-system-prompt "<text>" \
  --dangerously-skip-permissions
```

- **Auth:** `CLAUDE_CODE_OAUTH_TOKEN`, generated on the host with `claude setup-token`.
- **`--dangerously-skip-permissions`** is permitted **only** inside worker containers.
- **Streaming:** stream-json lines are parsed as they arrive. Every event is forwarded to the orchestrator event bus (for the dashboard) and appended to `data/transcripts/{run_id}.jsonl`.
- **Usage-limit detection:**
  - patterns come from config (`claude.usage_limit_patterns`)
  - the first implementation logs the raw message the first time a limit is hit, so the patterns can be confirmed
  - parse the reset time if present; otherwise default to 5 hours from the first failure
  - on detection, return `usage_limited` and set the backend to `parked(until=reset_at)`
- **Also usable in chat mode:** for Planner and Learning, a `claude -p` call with no tools, `--max-turns 1` and a JSON-only instruction. This runs on the host, not in a container, because there are no tools.

**`opencode`** (agentic, fallback)

- Runs `opencode run` inside the container with an OpenRouter model.
- Exact CLI flags are verified during M13 and wrapped in the adapter.
- Tool calls reach jira-mcp through OpenCode's MCP config.

**`openrouter_chat`** (chat)

- OpenAI Python SDK with `base_url=https://openrouter.ai/api/v1`.
- Passes a `models` fallback list.
- Requests usage accounting so each response reports its cost.
- Uses JSON-schema structured output where the model supports it. Otherwise it instructs JSON-only output and validates with Pydantic, retrying once on a validation error.
- Uses **one API key per role** (`OPENROUTER_KEY_REVIEWER`, `OPENROUTER_KEY_OPS`, and so on), each with a credit limit set on the OpenRouter dashboard.

### 9.3 Routing table (config, not code)

| Role | Primary | Fallback | Notes |
|---|---|---|---|
| planner | `claude_code` (chat mode) | `openrouter_chat` (free list) | Low volume, high leverage |
| coder | `claude_code` | `opencode` + cheap paid model, only if ticket has `allow-fallback`. Otherwise park. | Honors the run window |
| reviewer | `openrouter_chat` (cheap paid, non-Anthropic family) | free list | The deterministic phase needs no model |
| rebase | `claude_code` | `opencode` + cheap paid | Conflicts only; a clean rebase needs no model |
| docs | `openrouter_chat` (free list) | cheap paid | n/a |
| learning | `claude_code` (chat mode) | `openrouter_chat` (cheap paid) | Weekly |
| ops | `openrouter_chat` (free list) | n/a | Comment classification, summaries |

Model IDs live in `config.yaml`. Free model IDs rotate, so the config holds an ordered list and the backend tries them in order. On a 404 for a model, it marks that model dead for 24 hours.

### 9.4 Budget guard

**`budget.py` tracks, per backend and role:**
- Claude: parked state and reset time. Run counts and turns per day, for visibility.
- OpenRouter: USD spent per key per day, and free-model requests used today against the daily cap (1,000).

**Configurable limits:**
- `openrouter.daily_usd_cap` per role
- `openrouter.free_requests_daily_cap`
- `claude.run_window` (cron-like windows, e.g. `["23:00-08:00"]` local time)
- `claude.max_concurrent_runs` (default 1)

**Enforcement:** the scheduler checks the budget before claiming a ticket. A ticket is never claimed if its backend would be unavailable.

## 10. Worker sandbox

### 10.1 Image (`sandbox/Dockerfile`)

- **Base:** the official Playwright image (Node LTS + browsers).
- **Adds:** Python 3.12, `git`, `gh`, `jq`, Claude Code CLI, OpenCode CLI. jira-mcp is **not** installed in the image; it runs on the host (Section 15).
- **Non-root user:** `agent`, UID 1000.
- **Tag:** `codeit-worker:{version}`. Built by `codeit sandbox build`.

### 10.2 Worktree lifecycle

**Target repos** are cloned once under `data/repos/{repo}` (bare or normal clone).

**Creation:**
- Each ticket gets its own local clone (called its worktree in this document) under `data/worktrees/{repo}/{KEY}`. Real `git worktree` is not used: its `.git` is a pointer file into the mirror's git dir, which is not mounted into containers, so git would fail inside them (ADR-0002).
- New: `git clone {mirror} {path}` (hardlinked, fast on the same filesystem), set `origin` to the GitHub URL, `git fetch origin`, then `git checkout -B {branch} origin/main`.
- Rework: check out the existing branch, `git fetch`, then rebase onto `origin/main` if behind. A conflicting rebase is aborted, and rebase-agent work is enqueued.

**Cleanup:**
- On `Done` or `Rejected`: delete the ticket's clone directory.
- `codeit sandbox gc` removes worktrees for tickets that are terminal or older than 14 days.

### 10.3 Container launch

Uses the Python Docker SDK.

- **Mounts:**
  - the worktree read-write at `/workspace`
  - a per-run directory at `/run` (MCP config with the run's jira-mcp token, prompt file, outputs)
  - nothing else from the host
- **Environment:** only role-required variables (Section 19).
- **Limits:**
  - `cpus=2`, `mem_limit=4g`, `pids_limit=512` (configurable)
  - wall-clock timeout per role (coder 60 minutes, reviewer 30, rebase 20)
- **Network:**
  - a dedicated Docker network whose egress goes through an allowlisting proxy (`tinyproxy`, run as a sidecar via `sandbox/compose.yaml`)
  - **allowlist:** `api.anthropic.com`, `claude.ai`, `console.anthropic.com`, `openrouter.ai`, `github.com`, `api.github.com`, `codeload.github.com`, `objects.githubusercontent.com`, `registry.npmjs.org`, `pypi.org`, `files.pythonhosted.org`, Playwright CDN hosts
  - the list lives in config
  - **The Jira site host is not on the allowlist.** Containers have no Jira credential and no route to Jira. Their only path to Jira is the host jira-mcp endpoint (Section 15).
  - **Host reachability:** the jira-mcp listener must be reachable from the sandbox network and from nowhere wider (not the LAN). The exact bind address and container-side hostname (for example `host.docker.internal`) under Docker Desktop + WSL2 are verified in M4 and recorded in an ADR.
  - **Fallback if the proxy is deferred:** M4 may start with an unrestricted network, but the proxy must land before M7 is complete.
- **Teardown:** containers are removed after the run. Logs are kept.

## 11. Agent specifications

All agents implement:

```python
class Agent(Protocol):
    role: str
    async def run(self, ctx: RunContext) -> RunResult
```

- **`RunContext`:**
  - `run_id`
  - `ticket` (optional)
  - `feedback` (list of comments)
  - config
  - backends
  - clients (jira, github)
  - `workdir`, `container` (optional)
  - logger
  - event emitter
- **`RunResult`:**
  - status: a role-specific enum
  - `summary_md`
  - `artifacts` (dict)
  - `metrics` (dict)

**Prompts** live in `prompts/{role}/` as versioned markdown with Jinja2 variables. The prompt file hash is recorded on every run.

### 11.1 Planner

- **Trigger:** manual, `codeit plan <path/to/plan.md> [--epic "Name"] [--dry-run]`.
- **Inputs:**
  - the plan markdown
  - the target repo's `CLAUDE.md`
  - a repo file tree (depth 3, ignoring node_modules, .git and build dirs)
  - summaries of open tickets in the project
- **Output schema (validated with Pydantic):**

```json
{
  "epic": {"summary": "...", "description_md": "..."},
  "stories": [{
    "ref": "S1",
    "summary": "imperative, <= 80 chars",
    "user_story": "As a ..., I want ..., so that ...",
    "acceptance_criteria": ["Given ... When ... Then ..."],
    "technical_notes_md": "...",
    "test_plan": {"unit": ["..."], "e2e": ["..."]},
    "suggested_points": 1,
    "depends_on": ["S0"],
    "risk": "low|medium|high"
  }]
}
```

- **Steps:**
  1. Generate the JSON and validate it. On a validation error, retry once with the error attached.
  2. Flag any story with `suggested_points > 5` or fewer than 2 acceptance criteria with label `split-me`.
  3. Unless `--dry-run`:
     - create the Epic and Stories in `Agent Draft` with label `agent-draft`
     - render the description from a template: user story, acceptance criteria as a checklist, technical notes, test plan, suggested points
     - create "is blocked by" links from `depends_on`
  4. With `--dry-run`: print a table and write `data/plans/{run_id}.json`.
- **Does not set:** story points or priority. The human sets those.
- **Acceptance:** a sample plan with 5 or more features produces valid tickets with working links. The dry-run output matches what gets created.

### 11.2 Coder

- **Trigger:** the scheduler claims a `Ready for Dev` ticket (Section 12). Also manual, via `codeit run coder PROJ-12`.
- **Claim sequence (orchestrator):**
  1. acquire the SQLite lease
  2. re-read the ticket and confirm it is still `Ready for Dev`
  3. transition to `In Dev`
  4. set `Agent`, `Run ID`
  5. comment "Picked up by coder-1 (run {run_id})"
- **Prompt includes:**
  - ticket markdown (summary, story, acceptance criteria, notes, test plan)
  - `feedback`, when this is rework
  - branch name and the PR URL if one exists
  - explicit completion rules:
    - implement
    - add or update unit tests for each acceptance criterion
    - add Playwright tests for UI-facing criteria
    - run the full unit suite and the relevant Playwright specs until green
    - commit with messages `{KEY}: ...`
    - push
    - open a PR with the template, or update the existing one
    - print a final line `RESULT: {"status": "...", "pr_url": "...", "notes": "..."}`
- **Target repo steering** (the Coder relies on it; templates in Section 16):
  - `CLAUDE.md`
  - `.claude/skills/*`
  - `.claude/agents/*`
  - hooks in `.claude/settings.json`
- **Allowed tools:** Read, Edit, Write, Bash, Glob, Grep, plus jira-mcp read tools.
- **Result handling (orchestrator):**

| Coder result | Orchestrator action |
|---|---|
| `pr_opened` / `pr_updated` | Verify via GitHub that the PR exists and the head SHA changed. Set `PR URL`, add a remote link, transition to `Agent Review`. |
| `failed` / `blocked` / `max_turns` / `timeout` | Comment with the agent's notes plus a transcript excerpt. Add `needs-human`, transition to `Human Review`. |
| `usage_limited` | Release the lease, transition back to `Ready for Dev`, comment "parked until {reset_at}", park the backend. |

- **Acceptance:** on the sandbox app, a 1 to 2 point ticket yields a PR with passing CI and tests covering each acceptance criterion, with no human intervention.

### 11.3 Reviewer

- **Trigger:** a ticket in `Agent Review`.
- **Phase 1, deterministic** (`reviewer/checks.py`, in a fresh container on the PR head SHA). Each check is a command in the target repo's `codeit.yaml` (Section 16.4):
  1. `install`
  2. `lint`
  3. `typecheck`
  4. `unit`
  5. `e2e` (Playwright)
  6. **`new_tests_fail_on_base`:**
     - identify test files added or modified in the PR diff
     - check out `origin/main` source files while keeping the PR's versions of those test files
     - run only those tests
     - expected result: at least one fails
     - if all pass, record the finding `TESTS_DO_NOT_EXERCISE_CHANGE` (severity critical)
     - skipped with a note if the PR changes no test files. That itself is a critical finding if acceptance criteria exist.
  7. **`ci_status`:** GitHub check runs for the head SHA.
- Each check records status, duration, and a truncated log (last 200 lines).
- **Phase 2, LLM** (`openrouter_chat`, 1 call, or 2 if the diff is over 60 KB: first a summary per file, then a verdict). Inputs:
  - acceptance criteria
  - PR diff
  - Phase 1 results
  - the reviewer checklist at `prompts/reviewer/checklist.md`, which the Learning agent may edit
  - human test suggestions, if the ticket was sent back to testing
- **Output schema:**

```json
{
  "verdict": "pass|pass_with_notes|fail_critical",
  "ac_coverage": [{"criterion": "...", "status": "met|partial|unmet", "evidence": "file:line or test name"}],
  "findings": [{"severity": "critical|major|minor|nit", "file": "...", "line": 0, "issue": "...", "suggestion": "..."}],
  "summary_md": "..."
}
```

- **Verdict override:** any Phase 1 failure among `unit`, `e2e`, `typecheck` and `new_tests_fail_on_base` forces `fail_critical`, whatever the LLM said.
- **Actions:**
  1. post a PR review (summary, acceptance-criteria table, findings, check table)
  2. post a Jira comment with the same content in condensed form
  3. apply the routing from 6.2
- **Acceptance:** on the seeded-bug eval set (Section 17.4), the catch rate for critical bugs is at least 70% and the false-fail rate on correct PRs is at most 20% (initial targets, tuned later).

### 11.4 Merge watcher (non-LLM)

Every poll cycle:
- **For each ticket in `Human Review`:** if its PR is merged, transition to `Done` and comment with the merge SHA.
- **For each ticket in `Done`:** if its PR is not merged, add label `state-mismatch`.

### 11.5 Rebase agent

- **Trigger:** every `rebase.poll_minutes` (default 10). For each open agent PR where `mergeable_state` is `dirty` (conflict) or `behind` and the ticket is not `In Dev`.
- **Steps:**
  1. Take a lease on the ticket, scope `rebase`. A Coder or Reviewer cannot claim it meanwhile.
  2. In the worktree container: `git fetch && git rebase origin/main`.
  3. **Clean rebase:** run `unit` + `e2e`. If green, `git push --force-with-lease` and comment "Rebased onto main {sha}, tests green". If red, treat as a failure.
  4. **Conflicts:** run an agentic backend with:
     - the conflicted files
     - both sides' intent (this ticket's description + summaries of PRs merged into main since the branch point)
     - instructions to resolve, continue the rebase, and run tests
  5. **Escalate** (abort rebase, label `needs-human`, comment with the conflict list) if any of these hold:
     - conflicts in more than `rebase.max_files` (default 5) files
     - any conflicted path matches `rebase.never_auto` globs (default: migrations, lockfiles, `.github/**`)
     - tests are red after resolving
- **Jira status:** never changes. Comments only.
- **After a successful rebase of a PR in `Human Review`:** add a comment so the human knows the diff changed.

### 11.6 Docs agent

- **Trigger:** daily at `docs.run_at` (default 07:00 local), and manually with `codeit run docs`.
- **Inputs:**
  - tickets matching the Docs JQL (Done, not yet logged)
  - their PRs (title, body, merged SHA, review summary)
  - run metrics from SQLite
  - the latest eval summary
- **Outputs**, as one branch `docs/{date}` and one PR, updating:
  - **`CHANGELOG.md`:** Keep a Changelog format, entries grouped by Epic, each ending `({KEY}, #PR)`.
  - **`docs/worklog/{date}.md`:**
    - tickets closed
    - review loops per ticket
    - human returns
    - notable decisions
    - agent cost summary
  - **`docs/adr/NNNN-{slug}.md`:** created only when a PR body or review thread contains a design decision. An ops-model classification call decides this. Uses a Michael Nygard-style template.
  - **`README.md`:** the block between `<!-- codeit:status:start -->` and `<!-- codeit:status:end -->` is replaced with a metrics table (tickets shipped, median review loops, first-pass reviewer rate, latest eval pass@1 and pass^3).
- **After the PR opens:** add label `docs-logged` to each processed ticket.
- **Target repos:** runs against both the CodeIt repo (the system's own worklog) and each target repo (a changelog for that product).
- **Acceptance:** after 3 merged tickets, one docs PR contains correct changelog entries, a work log and an updated status block.

### 11.7 Learning agent

- **Trigger:** weekly (`learning.cron`, default Sunday 22:00), or once `learning.min_new_signals` (default 10) new human signals exist since the last run. Also manual with `codeit run learning`.
- **Signals collected:**
  - Jira rejection comments
  - human PR review comments on agent PRs
  - human send-back comments
  - Reviewer `fail_critical` findings
  - eval failures since the last run
- **Steps:**
  1. Classify each signal (ops model) into themes such as `testing`, `ui-conventions`, `api-design`, `ticket-quality`, `scope-creep`, `naming`, `error-handling`, `other`, with a one-line normalized lesson.
  2. Cluster by theme. A lesson qualifies when it has 2 or more occurrences, or 1 occurrence from the human labeled `learn` in a comment.
  3. Propose edits (Learning backend) to the right steering file:

| Lesson type | File to edit |
|---|---|
| Coder conventions | Target repo `CLAUDE.md` or a skill |
| Review gaps | `prompts/reviewer/checklist.md` |
| Ticket quality | `prompts/planner/*` |

  4. Open a PR per affected repo. The body lists each change, the evidence (links to the source comments) and the expected effect.
  5. **Eval gate:** the orchestrator runs the golden eval set (Section 17) with the PR branch's steering files and appends a before/after table to the PR. On regression beyond `eval.regression_tolerance`, add label `regression`.
- **Never merges its own PRs.**
- **Acceptance:** given a fixture set of 12 synthetic signals with 3 planted recurring themes, the agent proposes edits addressing all 3, and the eval table appears on the PR.

## 12. Orchestrator

### 12.1 Process

**Command:** `codeit up` starts:
- the FastAPI app (API + SSE)
- the scheduler loop
- the merge watcher
- the rebase poller
- cron jobs (docs, learning)

All run in one asyncio process. Graceful shutdown lets running jobs finish, or marks them `interrupted` after 60 seconds.

### 12.2 Scheduler loop (every `poll_seconds`, default 45)

```
for role in [reviewer, coder]:            # reviewer first: finishing work beats starting work
    while free_slots(role) > 0:
        if not budget.can_run(role): break
        ticket = next_candidate(role)      # JQL + dependency filter + not leased
        if not ticket: break
        if not leases.acquire(ticket, role, ttl): continue
        spawn(run_agent(role, ticket))
merge_watcher.tick()
leases.reap_expired()                      # expired lease -> retry or escalate per 6.2
```

### 12.3 Agent instances and slots

- Config `slots: {coder: 1, reviewer: 2, rebase: 1, docs: 1, learning: 1, planner: 1}`.
- Each slot is a named instance (`coder-1`, `coder-2`, ...) shown on the dashboard as idle, busy, parked or disabled.
- Changing slots takes effect on the next loop without a restart.
- `PATCH /api/agents/slots` changes slots at runtime.

### 12.4 Leases

- SQLite table `leases(ticket_key PK, role, instance, run_id, acquired_at, expires_at, heartbeat_at)`.
- Running jobs heartbeat every 60 seconds.
- **An expired lease** marks the run `abandoned`, kills the container if it's still alive, and applies the transition rules in 6.2.

### 12.5 Idempotency

Before any transition, the orchestrator re-reads the ticket and verifies the expected current status. If the status differs, the human has intervened: it logs the event, doesn't transition, and releases the lease.

### 12.6 CLI (Typer)

```
codeit up                      # run everything
codeit jira doctor|discover
codeit plan <file> [--dry-run]
codeit run <role> [KEY]        # one-off run
codeit sandbox build|gc
codeit eval run --suite golden --config <name> [--repeats 3]
codeit eval report [--compare a,b]
codeit budget                  # show budget state
codeit agents                  # show instances
```

## 13. Data model (SQLite)

| Table | Key columns |
|---|---|
| `runs` | `id` (ULID), `role`, `instance`, `ticket_key`, `backend`, `model`, `prompt_hash`, `steering_sha`, `status`, `started_at`, `ended_at`, `turns`, `input_tokens`, `output_tokens`, `cost_usd`, `result_json`, `transcript_path`, `error` |
| `events` | `id`, `run_id`, `ts`, `type` (`tool_call`, `message`, `check`, `transition`, `comment`, `budget`, `error`), `payload_json` |
| `leases` | as in 12.4 |
| `agent_instances` | `name`, `role`, `state`, `current_run_id`, `updated_at` |
| `tickets_cache` | `key`, `status`, `summary`, `priority`, `points`, `review_loop`, `human_returns`, `pr_url`, `updated_at` (dashboard speed only; Jira remains the source of truth) |
| `budget_ledger` | `ts`, `backend`, `role`, `key_name`, `usd`, `requests`, `note` |
| `backend_state` | `backend`, `state`, `parked_until`, `last_error` |
| `eval_runs` | `id`, `suite`, `config_name`, `steering_sha`, `model`, `started_at`, `ended_at`, `summary_json` |
| `eval_results` | `eval_run_id`, `task_id`, `repeat_idx`, `passed`, `hidden_pass_ratio`, `turns`, `cost_usd`, `duration_s`, `diff_lines`, `reviewer_verdict`, `notes` |
| `signals` | `id`, `source`, `ticket_key`, `pr`, `author`, `text`, `theme`, `lesson`, `used_in_learning_run` |

## 14. API (FastAPI)

| Method | Path | Returns |
|---|---|---|
| GET | `/api/agents` | Instances with state and current run |
| PATCH | `/api/agents/slots` | Update slot counts |
| GET | `/api/tickets?status=` | Cached tickets |
| GET | `/api/runs?role=&ticket=&limit=` | Runs |
| GET | `/api/runs/{id}` | Run detail + result |
| GET | `/api/runs/{id}/transcript` | Transcript JSONL (paged) |
| GET | `/api/budget` | Backend states, spend today, free-request usage |
| GET | `/api/evals` | Eval runs + summaries |
| GET | `/api/evals/{id}` | Per-task results |
| POST | `/api/runs/{role}` | Trigger a manual run (body: `ticket_key` optional) |
| GET | `/api/stream` | SSE: `agent_state`, `run_event`, `ticket_update`, `budget_update` |

- **Binding:** `127.0.0.1` only.
- **Auth:** a single bearer token from `.env` (`CODEIT_API_TOKEN`), so other local processes can't trigger runs.

## 15. jira-mcp server

**Package:** `mcp_servers/jira/`. Python, built on the official MCP SDK (FastMCP). Wraps `jira_client`.

**Where it runs:** on the host, never in a worker container. It is the only component besides the orchestrator that holds the Jira token (ADR-0004).

**Transports:**
- **streamable HTTP** on `127.0.0.1:8765` (bind address configurable, see 10.3). This is the path for worker containers. Every request needs a bearer run token.
- **stdio**, for the owner's interactive use from Claude Code or Claude Desktop on the host. Runs as role `human`, set by `CODEIT_ROLE` in the host environment. There is no network listener, so no token is needed.

**Run tokens (HTTP):**
- **Minted by the orchestrator** when it launches an agent run that needs Jira: 32 random bytes, URL-safe encoded.
- **Bound to** `{role, ticket_key, run_id}`. The server reads role and ticket from the token, never from anything the container sends.
- **Short-lived:** expires at the role's wall-clock timeout (10.3) plus 5 minutes, and is revoked when the run ends, whatever the outcome.
- **Stored hashed** (SHA-256) in the server's token registry. The plaintext exists only in the run's `/run/mcp.json`, as an `Authorization: Bearer` header.
- **Rejection:** a missing, unknown, expired or revoked token gets HTTP 401. The event is logged with the `run_id` if known.

**Tools** (typed inputs; outputs are markdown text plus structured content):

| Tool | Input | Output | Roles allowed |
|---|---|---|---|
| `get_ticket` | `key` | Ticket markdown + fields | all |
| `search_tickets` | `jql`, `limit<=50` | Ticket list | planner, learning, human |
| `get_comments` | `key`, `since?` | Comments as markdown | all |
| `add_comment` | `key`, `markdown` | Comment ID | coder, reviewer, rebase |
| `create_issue` | `type`, `summary`, `description_md`, `labels`, `parent?` | Key | planner, human |
| `link_issues` | `blocker`, `blocked` | ok | planner, human |

**Rules:**
- **No transition tool is exposed to agents.** Only the orchestrator transitions.
- **Role enforcement:** a session sees only the tools its role allows, so a disallowed tool is invisible rather than refused. Calls to a hidden tool are also rejected, as a second check.
- **Ticket binding:** with a run token, `add_comment` accepts only the token's `ticket_key`. Read tools accept only keys in the configured project.
- **Tool descriptions are written for models:**
  - say when to use the tool
  - give one example
  - state the constraints

**Tests:**
- unit tests per tool with a mocked `jira_client`
- token tests: missing, expired, revoked and wrong-ticket tokens are rejected; each role sees exactly its tools
- an MCP Inspector smoke test documented in the README
- an integration test that runs `claude -p` with an HTTP MCP config and a run token and asks it to fetch a fixture ticket (`live` marker)

## 16. Target repo steering kit

Templates live in `templates/target/`. `codeit init-target <path>` copies them into a target repo without overwriting existing files.

### 16.1 `CLAUDE.md` template sections

- Project overview
- Stack and commands (install, dev, lint, typecheck, unit, e2e)
- Code conventions
- Testing conventions:
  - every acceptance criterion maps to at least one test
  - Playwright uses role-based locators
  - no sleeps; use web-first assertions
- Git conventions (`{KEY}: message`)
- Definition of done
- Things never to do:
  - editing CI config
  - disabling tests
  - committing secrets
  - modifying lockfiles without need

### 16.2 Skills (`.claude/skills/<name>/SKILL.md`)

| Skill | Purpose |
|---|---|
| `implement-ticket` | The overall procedure from ticket to PR |
| `write-unit-test` | Test structure and naming, mapping to acceptance criteria |
| `write-playwright-test` | Locators, fixtures, web-first assertions, test data setup |
| `open-pr` | Branch naming, PR template, `gh pr create` / `gh pr edit`, final RESULT line |
| `address-feedback` | How to process `feedback` items one by one and reply to each |

### 16.3 Hooks (`.claude/settings.json`)

**`PostToolUse`** on `Edit|Write`:
- runs the formatter and linter on the changed file
- non-blocking; output is shown to the model

**`Stop`:**
- runs the unit suite
- if tests fail, exits with code 2 and writes the failure summary to stderr, so Claude continues working
- **must check `stop_hook_active` in the hook input and exit 0 when it is true**, to avoid endless loops
- behavior verified against current Claude Code hook docs in M5, with any difference recorded in an ADR

### 16.4 `codeit.yaml` (in the target repo)

```yaml
commands:
  install: npm ci
  lint: npm run lint
  typecheck: npm run typecheck
  unit: npm test -- --run
  e2e: npx playwright test
test_globs: ["**/*.test.ts", "**/*.spec.ts", "e2e/**/*.ts"]
never_auto_rebase: ["**/migrations/**", "package-lock.json", ".github/**"]
```

## 17. Evaluation harness

### 17.1 Golden suite layout (`evals/suites/golden/`)

```
tasks/
  T001-add-due-date/
    task.yaml            # id, title, points, base_commit, tags
    ticket.md            # exactly what the Coder receives
    hidden_tests/        # copied in only at scoring time
    reference.patch      # known-good solution (for sanity checks and reviewer evals)
```

- **Target app:** `codeit-sandbox-app`, scaffolded in M4.5 and extended in M9. Vite + React + TypeScript frontend, Express + TypeScript API, SQLite, Vitest, Playwright.
- **Initial suite:** 12 tasks
  - 4 of 1 point, 5 of 2 points, 3 of 3 points
  - mix of UI, API and full-stack
- **Every hidden test is reviewed by the owner.** The harness verifies that `reference.patch` passes all hidden tests and that `base_commit` fails at least one.

### 17.2 Runner

`codeit eval run --suite golden --config <name> --repeats 3`

**Per task and repeat:**
1. create a fresh worktree at `base_commit`
2. run the Coder with the named config (backend, model, prompt versions, steering SHA). Uses the same code path as production minus Jira: the ticket comes from `ticket.md`, and PR creation is replaced by a local commit.
3. copy `hidden_tests/` in and run them
4. optionally run the Reviewer on the diff and record its verdict
5. record the result in `eval_results`

**Concurrency** follows the backend slot and budget limits, and evals respect the Claude run window.

### 17.3 Coder metrics

| Metric | Definition |
|---|---|
| pass@1 | Mean share of repeats where all hidden tests pass |
| pass^k | Share of tasks where all k repeats pass (consistency) |
| hidden_pass_ratio | Mean share of hidden tests passing (partial credit) |
| reviewer_first_pass | Share of runs where the Reviewer verdict is not `fail_critical` |
| cost / turns / duration / diff_lines | Medians |

### 17.4 Reviewer evals

**Seeded-bug suite:** for each golden task, 2 mutated versions of `reference.patch` with a planted defect, drawn from:
- off-by-one
- missing null check
- wrong status code
- test asserting nothing
- UI element not wired

**Metrics:**
- critical catch rate on mutants
- false-fail rate on the clean `reference.patch`

### 17.5 Planner evals

**Suite:** 3 sample plans.

**Metrics:**
- schema validity
- INVEST rubric score from an LLM judge (ops model, rubric in `evals/rubrics/invest.md`)
- the owner's approval rate in real use
- normalized edit distance between the agent's draft and the approved ticket, captured by comparing the first and current description versions

### 17.6 Reporting

- `codeit eval report --compare configA,configB` prints and stores a comparison table.
- The dashboard Evals page charts metrics over time by `steering_sha`.

## 18. Observability

- **Logging:** structured logs (`structlog`, JSON) to `data/logs/codeit.jsonl`, with `run_id` and `ticket_key` on every line.
- **Transcripts:** every agentic run stores its full transcript.
- **Visibility:** every Jira transition and comment made by the orchestrator is also written to `events`.
- **Optional (post-M13):** self-hosted Langfuse via Docker for trace UI. Behind a config flag, not required.

## 19. Security

1. **Credential scoping per container role:**

| Role | Credentials in the container |
|---|---|
| coder | `CLAUDE_CODE_OAUTH_TOKEN` (or the OpenRouter coder key when falling back), branch-push GitHub token, a jira-mcp run token (role `coder`, this ticket only) |
| reviewer | No LLM keys (the LLM call runs on the host), read-only GitHub token. No jira-mcp token: the Reviewer's Jira comment is posted from the host. |
| rebase | Same as coder, with a run token for role `rebase` |

   **No Jira credential ever enters a worker container.** jira-mcp runs on the host and holds the Jira token. Containers get only a short-lived run token bound to their role and ticket (Section 15, ADR-0004). A leaked run token lets an attacker call that role's jira-mcp tools on that ticket until the run ends. It cannot transition statuses, and it does not work from outside the sandbox network.

2. **GitHub tokens:** fine-grained, target repo only. The agent token can push branches, and branch protection prevents pushes to `main`.
3. **Untrusted input:** ticket text, PR comments and repo content are all treated as untrusted. Prompts wrap them in clearly delimited sections and instruct the model to treat them as data. Containers have no host mounts beyond the worktree.
4. **Network:** container egress is allowlisted (10.3).
5. **Secrets:** `.env` is gitignored, and `.env.example` is committed. A pre-commit hook (gitleaks) runs in the CodeIt repo and the target repos.
6. **Permissions flag:** `--dangerously-skip-permissions` appears only in the container adapter, and a unit test asserts it is never used on the host.
7. **Accepted risk: the Claude OAuth token is in coder and rebase containers.** Claude Code must authenticate from inside the container, and there is no proxy for it. A prompt-injected agent can read `CLAUDE_CODE_OAUTH_TOKEN`.
   - **Impact:** someone else uses the owner's Claude Pro quota until the token is revoked. The token does not grant access to Jira, GitHub or the host.
   - **Limited by the egress allowlist (10.3):** the container can reach only allowlisted hosts, so it cannot send the token to an arbitrary server.
   - **Residual:** allowlisted channels can still carry the token out, for example a push to the ticket branch, a PR body or comment, or a request to `openrouter.ai`. The owner reviews every PR, and gitleaks runs in CI on target repos.
   - **Response:** if a leak is suspected, revoke the token and issue a new one with `claude setup-token`.

## 20. Configuration

### 20.1 `config.yaml` (example)

```yaml
project:
  jira_project_key: CODEIT
  target_repo: { name: codeit-sandbox-app, url: https://github.com/Anik-AC/codeit-sandbox-app.git, default_branch: main }
orchestrator:
  poll_seconds: 45
  max_review_loops: 3
  lease_ttl_minutes: { coder: 90, reviewer: 40, rebase: 30 }
slots: { planner: 1, coder: 1, reviewer: 2, rebase: 1, docs: 1, learning: 1 }
routing:
  planner:  { primary: claude_code_chat, fallback: openrouter_free }
  coder:    { primary: claude_code, fallback: opencode_paid, fallback_requires_label: allow-fallback }
  reviewer: { primary: openrouter_paid_review, fallback: openrouter_free }
  rebase:   { primary: claude_code, fallback: opencode_paid }
  docs:     { primary: openrouter_free, fallback: openrouter_paid_cheap }
  learning: { primary: claude_code_chat, fallback: openrouter_paid_cheap }
  ops:      { primary: openrouter_free }
models:
  openrouter_free: ["<free-model-1>:free", "<free-model-2>:free", "<free-model-3>:free"]
  openrouter_paid_review: ["<cheap-non-anthropic-coding-model>"]
  openrouter_paid_cheap: ["<cheap-model>"]
  opencode_paid: "openrouter/<cheap-coding-model>"
claude:
  run_window: ["23:00-08:00"]
  max_concurrent_runs: 1
  max_turns: { coder: 80, rebase: 40 }
  usage_limit_patterns: ["usage limit", "limit reached", "rate limit"]
openrouter:
  daily_usd_cap: { reviewer: 0.50, docs: 0.10, ops: 0.10, coder: 1.00 }
  free_requests_daily_cap: 900
sandbox:
  image: codeit-worker:0.1.0
  cpus: 2
  mem: 4g
  timeouts_minutes: { coder: 60, reviewer: 30, rebase: 20 }
  egress_allowlist: [ ... ]
rebase: { poll_minutes: 10, max_files: 5 }
docs: { run_at: "07:00" }
learning: { cron: "0 22 * * SUN", min_new_signals: 10 }
eval: { regression_tolerance: 0.05 }
```

The owner fills in model IDs at M3 and M6 from OpenRouter's current catalog.

### 20.2 `.env.example`

```
JIRA_BASE_URL=https://<site>.atlassian.net
JIRA_EMAIL=
# Host only (orchestrator and jira-mcp). Never passed to containers.
JIRA_API_TOKEN=
GITHUB_TOKEN_AGENT=
GITHUB_TOKEN_READONLY=
# Passed into coder and rebase containers (accepted risk, Section 19.7).
CLAUDE_CODE_OAUTH_TOKEN=
OPENROUTER_KEY_REVIEWER=
OPENROUTER_KEY_OPS=
OPENROUTER_KEY_CODER=
CODEIT_API_TOKEN=
```

## 21. Repository structure

```
codeit/
├── pyproject.toml            # uv-managed
├── config/                   # config.yaml, jira_ids.yaml (generated)
├── src/codeit/
│   ├── cli.py                # Typer entrypoint "codeit"
│   ├── config.py             # Pydantic settings
│   ├── orchestrator/         # scheduler, leases, budget, watcher, cron, api.py, sse.py
│   ├── agents/               # planner.py coder.py reviewer/ rebase.py docs.py learning.py
│   ├── backends/             # base.py claude_code.py opencode.py openrouter_chat.py
│   ├── jira_client/
│   ├── github_client/
│   ├── sandbox/              # docker.py clone.py
│   ├── db/                   # models.py, alembic/
│   └── evals/                # runner.py scorers.py report.py
├── mcp_servers/jira/
├── prompts/                  # planner/ coder/ reviewer/ rebase/ docs/ learning/ ops/
├── templates/                # pr_body.md, adr.md, target/ (CLAUDE.md, skills, hooks, codeit.yaml)
├── sandbox/                  # Dockerfile, compose.yaml (proxy), tinyproxy.conf
├── evals/suites/golden/  evals/suites/seeded_bugs/  evals/rubrics/
├── dashboard/                # Next.js
├── tests/                    # unit/, integration/, live/
├── docs/                     # prd.md (this file), adr/, worklog/
├── data/                     # gitignored: db, logs, transcripts, repos, worktrees
└── .github/workflows/ci.yml  # ruff, mypy, pytest (no live), dashboard lint + build
```

## 22. Tech stack and engineering standards

- **Python:**
  - `uv`, `ruff` (lint + format), `mypy --strict` on `src/`
  - `pytest`, `pytest-asyncio`, `respx`
  - `httpx`, `pydantic` v2
  - `sqlalchemy` 2 + `alembic`
  - `typer`, `structlog`, `docker` (SDK), `jinja2`
  - `openai` (for OpenRouter), `mcp` (official SDK)
  - `python-ulid`
- **Git operations:** through a `git.py` subprocess wrapper, not GitPython.
- **Dashboard:** Next.js App Router, TypeScript strict, Tailwind, shadcn/ui, `EventSource` for SSE, TanStack Query for fetches.
- **Coverage target:** 80% line coverage for `jira_client`, `orchestrator`, `backends`, `agents/reviewer/checks.py`.
- **Structure:** every module has a docstring stating its responsibility. Files over 400 lines should be split.

## 23. Milestones and acceptance criteria

| # | Milestone | Scope | Acceptance criteria |
|---|---|---|---|
| **M0** | Scaffold | Repo layout, `pyproject`, config loading, logging, DB + Alembic baseline, Typer CLI skeleton, CI | `codeit --help` works. CI green. `codeit config validate` catches a malformed config. |
| **M1** | Jira client + setup | `jira_client` (all modules), ADF converter, `codeit jira doctor` / `discover` | Doctor passes on the owner's site. ADF round-trip tests for all supported nodes. Search pagination tested including the repeated-token guard. Transition to each status works on a live test issue. |
| **M2** | jira-mcp | Host-side FastMCP server, role-scoped tools, streamable HTTP with per-run bearer tokens, stdio for the owner | Unit tests per tool. Inspector smoke test documented. Owner can call `get_ticket` from interactive Claude Code. Disallowed tools are absent for each role. Invalid, expired and wrong-ticket tokens are rejected. |
| **M3** | Planner | Planner agent, prompt, schema, dry-run, creation + links, `split-me` | Sample plan creates valid tickets in `Agent Draft` with links. Dry-run output matches created tickets. Invalid JSON is retried once. |
| **M4** | Sandbox + Claude backend | Dockerfile, per-ticket clone manager, container runner, `claude_code` backend with stream parsing and usage-limit detection | `codeit sandbox build` works. A container can run `claude -p "echo hello via bash"` and the transcript is saved. Simulated usage-limit output parks the backend. |
| **M4.5** | Minimal sandbox app | `codeit-sandbox-app`: Vite + React + TS frontend, Express + TS API, SQLite, Vitest, Playwright, `ci.yml`, `codeit.yaml` (the steering kit is added in M5 by `codeit init-target`) | CI green on GitHub. Install, unit and e2e commands pass locally and inside the worker image. |
| **M5** | Coder (manual) | Coder agent, target steering templates, `codeit init-target`, PR creation, result handling | `codeit run coder KEY` on a 1 to 2 point sandbox ticket produces a PR with passing CI. The ticket ends in `Agent Review` with PR URL set. Rework run updates the same PR. Stop hook blocks on failing tests. |
| **M6** | Reviewer + loop | Phase 1 checks incl. `new_tests_fail_on_base`, Phase 2 verdict, routing, loop cap | A PR with a failing test goes back to `Ready for Dev` with findings. The 3rd failure escalates to `Human Review` + `needs-human`. A tautological test is flagged critical. |
| **M7** | Orchestrator loop | Scheduler, slots, leases, heartbeats, reaper, idempotency, budget guard, run windows, merge watcher, egress proxy | Two tickets flow end to end unattended to `Human Review`. Killing the process mid-run recovers correctly on restart. Moving a ticket manually mid-run causes no conflicting transition. Merging a PR moves the ticket to `Done`. |
| **M8** | API + Dashboard | FastAPI endpoints, SSE, Next.js pages: Agents, Pipeline, Run detail, Budgets | Live agent state updates within 2 seconds. Run detail streams the transcript. Slot changes from the UI take effect on the next loop. |
| **M9** | Sandbox app + eval harness | `codeit-sandbox-app`, 12 golden tasks with hidden tests, runner, metrics, report, Evals page | Reference patches pass hidden tests. Base commits fail. `codeit eval run --repeats 3` completes and reports pass@1 and pass^3. |
| **M10** | Rebase agent | Poller, clean rebase path, conflict resolution, escalation rules | A forced conflict between two sandbox PRs is resolved with green tests, or escalated when it touches a `never_auto` path. |
| **M11** | Docs agent | Changelog, work log, ADR detection, README status block, `docs-logged` | Acceptance in 11.6. |
| **M12** | Learning agent | Signal collection, classification, clustering, steering PRs, eval gate | Acceptance in 11.7. |
| **M13** | Fallback backend | `opencode` backend, `allow-fallback` routing, comparison eval | A labeled ticket completes via OpenCode when Claude is parked. `codeit eval report --compare claude,opencode` produces a table. |

**Definition of done for every milestone:**
- tests pass in CI
- README updated (setup and usage for the new feature)
- work log entry added
- ADRs for any decision not already in this PRD

## 24. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Claude Pro limits stall work | Run window, 1 concurrent run, parking with reset time, opt-in fallback |
| Anthropic changes subscription usage rules for headless use | Backend abstraction. Fallback backend (M13). Nothing Claude-specific outside `backends/claude_code.py` and target steering files. |
| Free OpenRouter models disappear or 429 | Ordered model lists, dead-model marking, paid cheap fallback with daily caps |
| Tautological tests | `new_tests_fail_on_base` check |
| Coder/reviewer ping-pong | `max_review_loops`, escalation |
| Prompt injection from tickets or PRs | Container isolation, scoped credentials, no Jira token in containers (host-side jira-mcp with per-run tokens), egress allowlist, no transition tool for agents |
| Orchestrator crash mid-run | Leases, heartbeats, reaper, idempotent transitions |
| Jira API changes or pagination bugs | All calls in `jira_client`, repeated-token guard, live tests runnable on demand |
| Jira Free site deactivated for inactivity | Regular use keeps it active. Document in README. |
| Human review becomes the bottleneck | Batched docs PRs, reviewer summaries in the dashboard, Learning agent reduces repeat feedback |
| Scope creep in this build | Milestones are gated. M10 to M13 can be cut without breaking the core flow. |

## 25. Open questions (non-blocking)

1. Should the Coder also run in the evaluation "no Jira" mode for manual local tickets (a `codeit run coder --file ticket.md`)? Cheap to add in M5.
2. Should Human Review include a dashboard "review cockpit" (ticket, diff, checks, findings on one page with approve/send-back buttons that call Jira and GitHub)? Proposed as M8.5 if review friction is high.
3. Webhooks via Cloudflare Tunnel for faster reaction? Post-M13.
4. A Jira Automation rule that calls the orchestrator through a "Send web request" action once a tunnel exists? Useful for learning objective L3.
