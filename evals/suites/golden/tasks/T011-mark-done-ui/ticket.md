# T011: Mark tasks done from the page

**User story:** As a user, I want to tick off tasks on the page, so that I can see what is finished.

## Acceptance criteria

### API

- [ ] Every task in the API has a boolean `done` field; existing and new tasks start with `done: false`. The column is added by a new migration in `src/server/db.ts`.
- [ ] `PUT /api/tasks/:id/done` with `{"done": true}` or `{"done": false}` sets it and returns 200 with the task.
- [ ] Given `done` missing or not a boolean, Then 400 with `{"error": "done must be true or false"}`. Given no such task, Then 404 with `{"error": "Task not found"}`. Given an id that is not a positive whole number, Then 400 with `{"error": "Invalid task id"}`.

### Page

- [ ] Each task in the list has a checkbox whose accessible name is `Mark <title> as done` (for example `Mark Set up CI as done`), checked when the task is done.
- [ ] Clicking it sends `PUT /api/tasks/<id>/done` with the new value, and the checkbox and task update from the API's answer.
- [ ] The title of a done task (the `<strong>` element) has the CSS class `done`.
- [ ] When at least one task is done, the count reads `You have 3 tasks, 1 done`; when none are done it stays `You have 3 tasks`.
- [ ] Given the API call fails, Then the checkbox goes back to its previous state and the page shows `Could not update the task` in an element with `role="alert"`.

## Technical notes

- `Task` is in `src/shared/types.ts`. Page: `src/client/App.tsx`, `TaskList.tsx`, `TaskCount.tsx`; API calls: `src/client/api.ts`.
