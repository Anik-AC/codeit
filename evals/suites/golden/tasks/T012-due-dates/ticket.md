# T012: Due dates and overdue tasks

**User story:** As a user, I want to give tasks a due date and list the overdue ones, so that nothing slips.

## Acceptance criteria

- [ ] Every task in the API has a `dueDate` field: a date string like `"2026-05-31"`, or `null`. The demo tasks have `null`. The column is added by a new migration in `src/server/db.ts`.
- [ ] `POST /api/tasks` creates a task from `{"title": "...", "description": "...", "dueDate": "2026-05-31"}` and returns 201 with it. Title and description are trimmed; `description` and `dueDate` are optional (missing means `null`).
- [ ] Given a missing or blank title, Then 400 with `{"error": "title is required"}`; a title over 200 characters gives `{"error": "title must be at most 200 characters"}`.
- [ ] Given a `dueDate` that is not a real calendar date in the form `YYYY-MM-DD` (for example `2026-02-30`, `31/05/2026` or `2026-5-3`), Then 400 with `{"error": "dueDate must be a date like 2026-05-31"}`. `null` is allowed.
- [ ] `GET /api/tasks?overdue=true` returns only tasks whose due date is before today (the current date in UTC), newest first. Tasks due today, due later or without a due date are not overdue.
- [ ] Given `overdue` with any value other than `true`, Then 400 with `{"error": "overdue must be true"}`.

## Technical notes

- `Task` is in `src/shared/types.ts`; routes in `src/server/app.ts`; database access in `src/server/tasks.ts`.
- Compare dates as `YYYY-MM-DD` strings, and take "today" from `new Date()` so tests can control the clock.
