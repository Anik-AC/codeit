# T004: Placeholder for tasks without a description

**User story:** As a user, I want tasks without a description to say so, so that I can tell an empty description from a loading problem.

## Acceptance criteria

- [ ] Given a task whose description is `null`, When the task list shows it, Then its list item shows the text `No description`.
- [ ] Given a task whose description is only spaces (for example `"   "`), Then its list item also shows `No description`.
- [ ] Given a task with a description, Then its list item shows the description and not `No description`.
- [ ] The placeholder has the CSS class `muted`, so it can be styled more quietly than real descriptions.

## Technical notes

- The list is `src/client/TaskList.tsx`. Component tests live in `tests/unit/client/`.
