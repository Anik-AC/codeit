# 0014. API and dashboard

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

M8 adds the API from PRD 14 and a web dashboard (PRD D3) with the pages Agents, Pipeline, Run detail and Budgets. The acceptance criteria:

- agent state updates within 2 seconds
- the run detail streams the transcript
- slot changes from the UI take effect on the next loop

The PRD fixes the stack (FastAPI, Next.js App Router, TypeScript, Tailwind, shadcn/ui, TanStack Query, `EventSource`) and the auth model (loopback only, one bearer token). It leaves open how the browser gets the token, where the dashboard is served from, and how live data reaches it.

## Decision

### One origin: the API serves the dashboard

- **Static export:** the dashboard is built with `output: "export"` to `dashboard/out/`, and the FastAPI app inside `codeit up` serves it at `/`, next to `/api/*`. The address is `http://127.0.0.1:8770` (`api:` in config).
- **Why:** there is no second server to run, and no CORS. Pages that need an id use a query string (`/run/?id=...`), because a static export has no dynamic routes.
- **Build:** `codeit dashboard build` runs `npm ci` and `npm run build`.
- **Development:** in `next dev`, `/api/*` is proxied to the API instead (`next.config.ts`).

### Auth: the token, then a session cookie

- **Every `/api/*` route needs the token** except login and the session check. It can come as `Authorization: Bearer $CODEIT_API_TOKEN`, as for scripts.
- **Browser login:**
  - the login page posts the token once to `POST /api/login`
  - the API sets a cookie holding an HMAC of the token, not the token itself; changing the token logs every browser out
  - the cookie is `HttpOnly` and `SameSite=Strict`, and lasts 30 days
- **Why a cookie:** `EventSource` cannot send headers. With the cookie, the live streams need no token in the URL, and scripts on the page cannot read the token.
- **Other sites** cannot ride on the cookie (SameSite), and a DNS-rebinding page is refused by a host check that accepts only `127.0.0.1`, `localhost` and `[::1]`.
- **No token in `.env`:** `codeit up` warns and runs without the API.

### Live data

- **Event bus:** an in-process bus (`orchestrator/bus.py`) with one bounded queue per subscriber. A slow browser loses old events rather than slowing the orchestrator.
- **What the scheduler publishes:**
  - `agent_state`, as soon as a job starts or ends and whenever slots change (not only every 45-second tick)
  - `run_event` on start and end
  - `ticket_update` for tickets whose cached copy changed
  - `budget_update` when the budget state changes
- **`GET /api/stream`** (SSE) first sends a snapshot of agents and budget, then the bus, with a keepalive comment every 15 seconds.
- **Transcripts** stream from `GET /api/runs/{id}/transcript/stream`, an addition to PRD 14. It follows the run's transcript file, which Claude runs append line by line, and ends with an `end` event once the run is no longer `running`.
  - Because it reads the file, it also works for runs started by a manual `codeit run` in another process.
  - The paged `GET /api/runs/{id}/transcript` stays for scripts.
- **In the browser,** one `EventSource` feeds the TanStack Query cache, so every page updates without polling.
- **Ticket cache:** `tickets_cache` is refreshed every tick from one JQL query covering open tickets and anything updated in the last 7 days.

### Slots and manual runs

- **`PATCH /api/agents/slots`** applies at once to the next claim (a running job is never stopped). It is saved in `data/slots.json`, which wins over `config.yaml`, so a UI change survives restarts. Counts are 0 to 10.
- **`POST /api/runs/{role}`** starts the Coder or Reviewer on a ticket now. It uses the same limits as the loop, and first checks that the ticket is in the role's status. A refusal is `409` with the reason.
- **Async routes:** routes that touch the orchestrator are `async`, so they run on the event loop. A plain `def` route runs in FastAPI's thread pool, where it cannot start tasks or safely publish on the bus. Found live: the first manual run request failed with "no running event loop".

### Frontend

- **Versions:** Next.js 16.3, React 19.3, Tailwind 4, TanStack Query 5, TypeScript 5.9 (not 7, which is too new for the Next toolchain), and ESLint 9 with `eslint-config-next`.
- **Components:** the few shadcn-style ones (button, card, badge, input) are written in `components/ui/` with `clsx` and `tailwind-merge`, rather than pulling in the shadcn CLI and Radix for four components.
- **Colours** are tokens with a dark variant (`prefers-color-scheme`).
- **Evals** return an empty list until M9.

## Consequences

- The dashboard is only as up to date as its last `codeit dashboard build`. CI builds it on every PR, but a local checkout must be built once.
- Anything that can reach 127.0.0.1:8770 with the token or cookie can start runs and change slots. That is the owner's machine; the token stays in `.env`.
- No browser test runs locally: Chromium's system libraries need sudo on this WSL setup. The API and streams are tested over real HTTP instead, including a live-stream event arriving under 2 seconds.

## Live check (2026-09-30)

With `codeit up --any-time` and the real `.env` token:

- `401` without the token.
- Agents and 8 cached tickets returned; `/` and `/pipeline/` served.
- `/api/stream` sent its snapshot, and a slot change appeared on it at once.
- A finished Coder run's transcript streamed 58 events, then `end`.
- Cookie login worked.
- Starting a Coder on a Human Review ticket returned `409` with the reason.
- The merge watcher moved CODEIT-84 to Done after the owner merged its PR (#12).
