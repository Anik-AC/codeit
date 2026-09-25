# 0010. Sandbox app stack details

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

ADR-0003 added M4.5: a minimal `codeit-sandbox-app` (Vite + React + TS, Express + TS, SQLite, Vitest, Playwright, CI, `codeit.yaml`) as the target for the Coder and Reviewer. Building it needed choices the PRD does not make. The agents inherit these choices, so they should be deliberate.

## Decision

- **Scope:** the app only lists tasks (`GET /api/tasks`). Every feature in `docs/samples/sample-plan.md` is left for the agents, so M5 and later have real work with a known shape.
- **SQLite through Node's built-in `node:sqlite`**, not `better-sqlite3`. No native module to compile inside containers or CI. The cost is requiring Node 24.
  - Migrations are a numbered list applied through `PRAGMA user_version`.
- **The API runs with `tsx`**, with no server build step. `vite build` builds only the client. For e2e, the API serves the built client with an in-memory database seeded with demo tasks (`DB_PATH=:memory: SEED=1 SERVE_CLIENT=1`).
- **Exact version pins** for every dependency:
  - TypeScript is pinned to **6.0.3**, because typescript-eslint 8.70 supports TypeScript below 6.1 and TypeScript 7 is not supported yet.
  - `@playwright/test` is pinned to **1.63.0** to match the worker image (ADR-0009).
- **CI:**
  - lint, typecheck and unit run on Node 24
  - e2e runs inside `mcr.microsoft.com/playwright:v1.63.0-noble`, the worker's base image, so CI and the worker use the same browsers
- **Install scripts:** npm 11 blocks dependency install scripts by default. This is left on. It only skips esbuild's verification step, and it stops a dependency added by an agent from running code at install time.
- **Local e2e on the owner's WSL** needs Chromium's system libraries, which need sudo (`sudo npx playwright install-deps chromium`). Until then, e2e runs in the worker image, which has them.
- **Repo flow:** `main` started with only a README, so the app itself arrived through a reviewed PR, like CodeIt's milestones.
- **`codeit.yaml` loader:** CodeIt reads it with `codeit.target.load_target_config`, a typed model the Reviewer (M6) will use.

## Consequences

- A live test (`tests/live/test_target_repo_live.py`) clones the target repo with the real `CloneManager` and runs all five `codeit.yaml` commands in a worker container. That is the M4.5 acceptance check, and it is the path the Reviewer's phase 1 will take.
- Bumping Playwright means changing both the worker image and the app together.
