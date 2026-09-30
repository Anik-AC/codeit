# T003: Limit how many tasks the API returns

**User story:** As an API client, I want to ask for only the newest few tasks, so that I do not download the whole list.

## Acceptance criteria

- [ ] Given 3 tasks, When I call `GET /api/tasks?limit=2`, Then it returns 200 with the 2 newest tasks, newest first.
- [ ] Given 3 tasks, When I call `GET /api/tasks?limit=10`, Then it returns all 3.
- [ ] Given no `limit`, When I call `GET /api/tasks`, Then it returns every task, as today.
- [ ] Given `limit` is not a whole number from 1 to 100 (for example `0`, `101`, `-1`, `abc` or `2.5`), When I call `GET /api/tasks?limit=...`, Then it returns 400 with `{"error": "limit must be a whole number from 1 to 100"}`.

## Technical notes

- `listTasks` in `src/server/tasks.ts` orders newest first; the route is in `src/server/app.ts`.
