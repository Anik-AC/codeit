# T010: Add tasks from the page

**User story:** As a user, I want to add a task from the task page, so that I do not need another tool to create tasks.

## Acceptance criteria

### API

- [ ] `POST /api/tasks` with `{"title": "...", "description": "..."}` creates a task and returns 201 with it (`id`, `title`, `description`, `createdAt`). Title and description are trimmed; a missing or blank description is stored as `null`.
- [ ] Given a missing or blank title, Then 400 with `{"error": "title is required"}`. Given a title longer than 200 characters, Then 400 with `{"error": "title must be at most 200 characters"}`.

### Page

- [ ] Once tasks have loaded, the page shows a form named `Add a task` (`aria-label`) with a text input labelled `Title`, a text input labelled `Description` and a button `Add task`.
- [ ] The `Add task` button is disabled while the title is empty or only spaces.
- [ ] Given I type a title (and optionally a description) and press `Add task`, Then the page sends `POST /api/tasks` with a JSON body, the new task appears at the top of the list, the task count goes up by one, and both inputs are cleared.
- [ ] Given the API answers with an error, Then the form shows the API's `error` message in an element with `role="alert"` inside the form, and keeps what I typed.

## Technical notes

- Page: `src/client/App.tsx`; API calls: `src/client/api.ts`; routes: `src/server/app.ts`.
- Tests in `tests/unit/`. The end-to-end tests in `e2e/` share one server with fixed demo data, so do not add tasks there.
