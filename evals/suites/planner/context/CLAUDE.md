# codeit-sandbox-app

Instructions for AI agents (and humans) working in this repository. CodeIt's Coder reads
this file on every run, so keep it short and true.

## Project overview

A small single-user task tracker. Today it only lists tasks; features are added one
ticket at a time.

- `src/server/`: Express 5 API, run with `tsx`. `app.ts` builds the app (routes under
  `/api`), `db.ts` opens SQLite (`node:sqlite`) and holds the migrations, and each feature
  has a module (e.g. `tasks.ts`) with its queries.
- `src/client/`: React 19 + Vite. `App.tsx` loads data through `api.ts`; components are
  one per file.
- `src/shared/types.ts`: types shared by the API and the client.
- `tests/unit/`: Vitest (`server/` with supertest, `client/` with Testing Library).
  `e2e/`: Playwright.

## Stack and commands

| Task | Command |
|---|---|
| Install | `npm ci` |
| Lint | `npm run lint` |
| Type check | `npm run typecheck` |
| Unit tests | `npm test -- --run` |
| End-to-end tests | `npx playwright test` |

Run lint, type check and unit tests before every commit. Run the end-to-end tests that
cover what you changed before opening or updating a pull request.

## Code conventions

- TypeScript strict mode; no `any`. Import local files with their `.ts`/`.tsx` extension.
- **API:** validate every request body and parameter with `zod`; on bad input return
  `400` with `{ "error": "<message>" }`. Unknown IDs return `404` with the same shape.
  Keep routes thin: SQL lives in the feature module, not in `app.ts`.
- **Database:** schema changes are new entries appended to `MIGRATIONS` in `db.ts`. Never
  edit an existing migration. Store times as ISO 8601 UTC strings.
- **Client:** function components and hooks. Fetch through `api.ts` only. Every form
  control has a visible label; buttons have accessible names.
- Keep `src/shared/types.ts` the single source of truth for API shapes.

## Testing conventions

- Every acceptance criterion maps to at least one test. Name tests after the behaviour
  they check.
- Unit tests live next to the code they test, or under `tests/unit/`.
- Playwright tests use role-based locators (`getByRole`, `getByLabel`), never CSS or XPath
  selectors that depend on layout.
- No sleeps or fixed timeouts. Use web-first assertions (`await expect(locator)...`).
- A new test must fail without your change. If it passes on the old code, it tests
  nothing.

## Git conventions

- Branch: `{KEY}-{short-slug}`, for example `CODEIT-12-add-due-dates`.
- Commit messages: `{KEY}: what changed`, for example `CODEIT-12: validate due dates`.
- One pull request per ticket. On rework, push to the same branch; never open a second PR.

## Definition of done

- Every acceptance criterion is implemented and has a test.
- Lint, type check, unit and end-to-end tests pass locally.
- The pull request uses the template and fills in every section.

## Never

- Edit CI configuration (`.github/`), or this file's hooks in `.claude/`, unless the ticket
  asks for it.
- Disable, skip or delete tests to make a build pass.
- Commit secrets, `.env` files or credentials.
- Change lockfiles unless you added or removed a dependency.
- Approve npm install scripts for new dependencies without a stated reason.
