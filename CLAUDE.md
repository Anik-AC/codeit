# CLAUDE.md

## Start of every session

1. Read [docs/prd.md](docs/prd.md) (the spec) and [docs/plan.md](docs/plan.md) (milestone roadmap and decisions).
2. Read the latest entry in [docs/worklog/](docs/worklog/) for current status, what's done and what's blocked on the owner.

## How to work

- **Build one milestone at a time.** Stop when a milestone meets its acceptance criteria and wait for the owner's review. Start the next milestone only after they confirm.
- Work on a branch per milestone (`m1-jira-client`, ...). Commit messages look like `M1: ...`.
- A milestone is done only when all of these hold:
  - tests pass
  - the README is updated
  - a worklog entry is added for the session
  - an ADR exists in `docs/adr/` for any decision not already in the PRD
- Follow PRD section 0. Ask the owner rather than guess on anything touching credentials, git history or Jira workflow state.

## Commands

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                  # live tests skipped unless LIVE=1
uv run codeit --help
```
