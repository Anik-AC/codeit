# T002: Show when each task was created

**User story:** As a user, I want to see when each task was created, so that I can spot old tasks.

## Acceptance criteria

- [ ] Given a task created at `2026-01-01T09:02:00.000Z`, When the task list shows it, Then the task's list item contains a `<time>` element whose `dateTime` attribute is `2026-01-01T09:02:00.000Z` and whose text is `Jan 1, 2026`.
- [ ] Dates are formatted in UTC as short month, day and year, like `Mar 15, 2026` (the same as `toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" })`).
- [ ] Given two tasks created on different days, Then each list item shows its own date.

## Technical notes

- The list is `src/client/TaskList.tsx`; tasks already carry `createdAt`.
- Component tests live in `tests/unit/client/`.
