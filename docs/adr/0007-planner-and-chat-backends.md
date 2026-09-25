# 0007. Planner and chat backends

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

M3 builds the first agent (the Planner) and the chat side of two backends: Claude Code in chat mode, and OpenRouter. The installed tools and live runs settled several details the PRD leaves open or gets slightly wrong.

## Decision

### Claude Code chat mode (`backends/claude_code.py`)

- Flags: `-p --output-format json --tools "" --strict-mcp-config --no-session-persistence --system-prompt <text> --json-schema <schema>`.
  - The prompt goes in on stdin.
  - The CLI runs in an empty temporary directory, so no project `CLAUDE.md` or settings are loaded.
- **`--max-turns 3`, not 1** (PRD 9.2). With `--json-schema`, Claude Code returns a validated `structured_output` object, but that takes two turns.
- **Subscription only:** `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed from the child's environment, so the CLI cannot switch to API billing.
- **Reporting:**
  - `total_cost_usd` is recorded as an estimate, since the subscription is not billed per call.
  - The model recorded is the one with the most output tokens in `modelUsage`. The first entry can be a small helper model.
- **Usage limits** match `claude.usage_limit_patterns`. They park the backend until the `|<epoch>` reset time if one is given, otherwise for 5 hours.

### OpenRouter (`backends/openrouter_chat.py`) uses httpx, not the OpenAI SDK

The OpenAI SDK (3.x) now uses its own HTTP library, `httpx2`, which respx cannot mock, and CodeIt needs only one endpoint. httpx matches `jira_client` and keeps the tests simple.

- Models are tried in order:
  - a 404 marks a model dead for 24 hours, across the process
  - 429, 5xx and connection errors move on to the next model
  - 401, 402 and 403 make the backend unavailable
- `response_format: json_schema` is sent with `strict: false`. A model that rejects it with a 400 gets one retry, with the schema in the system prompt instead.
- **Keys per role:** reviewer and coder use their own keys. Planner, docs, learning and ops share `OPENROUTER_KEY_OPS`.
- **Initial free list:** `nvidia/nemotron-3-super-120b-a12b:free`, `qwen/qwen3.8-27b:free`, `nex-agi/nex-n2.5-pro:free`. These are free models that advertise structured outputs in the catalog on 2026-09-25.

### Planner

- **Three separate steps:**
  1. Draft: model call, validation, one retry.
  2. Plan: pure mapping to Jira payloads.
  3. Apply: Jira writes.

  Dry runs and real runs share step 2, so a dry run always matches what gets created.
- **Saved plans:** every draft is saved to `data/plans/{run_id}.json`. `codeit plan <that file>` applies it later without calling the model again, and refuses to apply it twice.
- **Retry and fallback are separate.** Invalid output is retried once on the same backend, with the errors attached. A second invalid answer fails the run. The fallback backend is used only when the primary is unavailable, because fallback is for availability, not quality.
- **All or nothing:** if any Jira write fails, the run deletes every issue it created, newest first.
- **Options:** `--epic` takes a name for the new Epic or an existing Epic key. `--repo` points at the target checkout, defaulting to `data/repos/<target repo>`. Without one, the Planner plans from text alone.
- **Schema:**
  - at least 1 acceptance criterion; fewer than 2 gets `split-me`, as does more than 5 points
  - points 1 to 13
  - refs must be unique, dependencies must point to existing refs, and cycles are rejected
- **Run records:** each run is recorded in `runs` with status `dry_run`, `completed` or `failed`, plus the prompt hash, backend, model, attempts and estimated cost.
- **Dependency guidance:** the first live run produced no dependencies, so the prompt now states when `depends_on` is required. A UI story that calls a new endpoint depends on the story that adds it.

### Database repair

Databases created before the ADR-0006 fix have tables but no `alembic_version` row. `db.upgrade` now stamps such a database at the revision whose tables it has, then upgrades normally. This repaired the owner's `data/codeit.db` without data loss.

## Consequences

- A sample plan costs about $0.18 in API-equivalent usage against the subscription.
- Live OpenRouter calls are untested until `OPENROUTER_KEY_OPS` is set. The unit tests cover the protocol.
- M4 adds agentic mode to `claude_code.py`, reusing its usage-limit parsing.
