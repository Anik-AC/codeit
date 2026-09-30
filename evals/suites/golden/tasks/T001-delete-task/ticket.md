# T001: Delete a task

**User story:** As a user, I want to delete a task, so that my list only shows work I still care about.

## Acceptance criteria

- [ ] Given a task with id 2 exists, When I call `DELETE /api/tasks/2`, Then it returns 204 with an empty body, and `GET /api/tasks` no longer includes it.
- [ ] Given no task with id 99 exists, When I call `DELETE /api/tasks/99`, Then it returns 404 with `{"error": "Task not found"}`.
- [ ] Given an id that is not a positive whole number (for example `abc` or `0`), When I call `DELETE /api/tasks/abc`, Then it returns 400 with `{"error": "Invalid task id"}`.

## Technical notes

- Routes live in `src/server/app.ts`; database access in `src/server/tasks.ts`.
- Add API tests next to the existing ones in `tests/unit/server/`.
