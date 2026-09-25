# 0008. One OpenRouter key; models chosen in .env

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

PRD 9.2 gave each role its own OpenRouter key (`OPENROUTER_KEY_REVIEWER`, `OPENROUTER_KEY_OPS`, `OPENROUTER_KEY_CODER`) and put model IDs in `config.yaml`. The owner wants:
- one OpenRouter key for every use
- to switch models by price, and to compare them, by editing `.env` rather than config

## Decision

- **`OPENROUTER_API_KEY`** is the one key for every role. The old per-role names are still read as fallbacks, so existing `.env` files keep working. Budgets stay per role in CodeIt's own ledger (M7), not per key.
- **`OPENROUTER_MODELS_<NAME>`** overrides `models.openrouter_<name>` in `config.yaml`. Values are comma-separated and tried in order. For example, `OPENROUTER_MODELS_PAID_REVIEW=vendor/model-a,vendor/model-b`. `config.yaml` keeps the defaults.
- **`OPENCODE_MODEL`** overrides `models.opencode_paid`.
- **`CLAUDE_MODEL`** picks the Claude model for every Claude Code run, chat and agentic. Unset, Claude Code uses its own default: Sonnet 5 in containers at the time of writing, and whatever the owner's settings say on the host.
- **Precedence and errors:**
  - the process environment wins over `.env`
  - a variable naming a list that does not exist is an error, so a typo cannot silently fall back to the defaults
- `codeit config validate` prints every model list, its variable, and whether it comes from `.env` or the defaults.

## Consequences

- Per-key credit limits on the OpenRouter dashboard no longer separate roles. The daily per-role caps in PRD 9.4 (M7) become the only per-role spending control.
- Model experiments need no config change or commit. Eval runs (M9) should record the effective model, which they do through `runs.model`.
