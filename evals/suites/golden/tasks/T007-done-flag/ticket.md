# T007: Tasks can be marked done

**User story:** As a user, I want to mark a task done and undo it, so that I can track what is finished.

## Acceptance criteria

- [ ] Every task in the API has a boolean `done` field. Existing and new tasks start with `done: false`.
- [ ] Given task 1 is not done, When I call `POST /api/tasks/1/toggle`, Then it returns 200 with the task and `done: true`; calling it again returns `done: false`.
- [ ] `GET /api/tasks` shows the current `done` value of every task.
- [ ] Given no task with that id, Then the toggle returns 404 with `{"error": "Task not found"}`. Given an id that is not a positive whole number, Then 400 with `{"error": "Invalid task id"}`.
- [ ] The database change is a new migration in `src/server/db.ts`; databases created before this change get the column when opened.

## Technical notes

- `Task` in `src/shared/types.ts` is shared with the client; add `done: boolean` there.
- SQLite has no boolean type: store 0 or 1 and convert in `src/server/tasks.ts`.
