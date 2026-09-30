# T006: Edit a task through the API

**User story:** As an API client, I want to change a task's title or description, so that I can fix mistakes.

## Acceptance criteria

- [ ] Given task 1 exists, When I call `PATCH /api/tasks/1` with `{"title": "New title"}`, Then it returns 200 with the whole updated task, and only the title changed.
- [ ] Given `{"description": "New details"}` or `{"description": null}`, Then only the description changes. A blank description is stored as `null`. Title and description are trimmed.
- [ ] Given both fields, Then both change.
- [ ] Given a body with neither `title` nor `description`, Then it returns 400 with `{"error": "nothing to update"}`.
- [ ] Given a `title` that is not a string, or is blank, Then it returns 400 with `{"error": "title is required"}`. Given a title longer than 200 characters, Then `{"error": "title must be at most 200 characters"}`. Given a `description` that is neither a string nor `null`, Then `{"error": "description must be a string"}`.
- [ ] Given no task with that id, Then it returns 404 with `{"error": "Task not found"}`. Given an id that is not a positive whole number, Then it returns 400 with `{"error": "Invalid task id"}`.
- [ ] `createdAt` never changes.

## Technical notes

- Routes are in `src/server/app.ts`, database access in `src/server/tasks.ts`.
