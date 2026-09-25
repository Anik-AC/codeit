# 0003. Build a minimal sandbox app in M4.5

- **Status:** Accepted
- **Date:** 2026-09-24

## Context

M5 (Coder) and M6 (Reviewer) are accepted against "a 1 to 2 point sandbox ticket", but the PRD built `codeit-sandbox-app` only in M9, together with the eval harness.

## Decision

Add milestone **M4.5**, a minimal `codeit-sandbox-app` with:
- Vite + React + TypeScript
- Express + TypeScript API
- SQLite
- Vitest, Playwright
- a GitHub Actions `ci.yml`
- `codeit.yaml`

M5 adds the steering kit to it with `codeit init-target`. M9 extends the app and adds the 12 golden tasks with hidden tests.

## Consequences

- M5 and M6 have a real target with CI from the start.
- M9 gets smaller, because the app skeleton already exists.
- The owner needs the GitHub repo `codeit-sandbox-app` before M4.5.
