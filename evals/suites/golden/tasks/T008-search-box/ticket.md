# T008: Search box for the task list

**User story:** As a user with many tasks, I want to filter the list as I type, so that I can find a task quickly.

## Acceptance criteria

- [ ] Once tasks have loaded, the page shows a text input with the label `Search tasks`, above the task list.
- [ ] Given tasks "Write the project plan", "Set up CI" and "Review open pull requests", When I type `pro` in the search box, Then the list shows only the tasks whose title contains `pro`, ignoring case ("Write the project plan"), in their usual order.
- [ ] Leading and trailing spaces in the search are ignored. An empty search shows every task.
- [ ] Given a search that matches nothing, Then instead of the list the page shows `No tasks match your search.`
- [ ] The task count under the heading still shows the total number of tasks, not the number of matches.
- [ ] Filtering happens in the browser; no extra API calls are made while typing.

## Technical notes

- The page is `src/client/App.tsx`; the list is `src/client/TaskList.tsx`. Component tests live in `tests/unit/client/`.
