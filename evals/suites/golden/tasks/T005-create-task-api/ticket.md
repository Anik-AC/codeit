# T005: Create a task through the API

**User story:** As an API client, I want to create tasks, so that the list is not limited to the demo data.

## Acceptance criteria

- [ ] Given `{"title": "Buy milk", "description": "Two litres"}`, When I call `POST /api/tasks`, Then it returns 201 with the new task (`id`, `title`, `description`, `createdAt` as an ISO 8601 UTC string) and a `Location` header of `/api/tasks/<id>`.
- [ ] Given a created task, When I call `GET /api/tasks`, Then it is first in the list.
- [ ] Title and description are trimmed. A missing, blank or empty description is stored as `null`.
- [ ] Given no title, a title that is not a string, or a blank title, Then it returns 400 with `{"error": "title is required"}`.
- [ ] Given a title longer than 200 characters after trimming, Then it returns 400 with `{"error": "title must be at most 200 characters"}`.
- [ ] Given a description that is present but neither a string nor `null`, Then it returns 400 with `{"error": "description must be a string"}`.

## Technical notes

- `zod` is already a dependency. Routes are in `src/server/app.ts`, database access in `src/server/tasks.ts`.
