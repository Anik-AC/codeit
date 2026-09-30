# T009: Search tasks through the API

**User story:** As an API client, I want to search tasks by text, so that I can find them without downloading everything.

## Acceptance criteria

- [ ] Given the demo tasks, When I call `GET /api/tasks?q=agent`, Then it returns the tasks whose title or description contains `agent`, ignoring case, newest first ("Review open pull requests", whose description mentions "agent PRs").
- [ ] Given `q=PLAN`, Then it matches "Write the project plan" (case does not matter).
- [ ] Leading and trailing spaces in `q` are ignored; an empty `q` returns every task.
- [ ] `%` and `_` in `q` match those characters literally, not as wildcards (for example `q=%` matches nothing in the demo tasks).
- [ ] Given `q` longer than 100 characters after trimming, Then it returns 400 with `{"error": "q must be at most 100 characters"}`.

## Technical notes

- `listTasks` is in `src/server/tasks.ts`; the route is in `src/server/app.ts`. Use a bound parameter, never string concatenation, for the search text.
