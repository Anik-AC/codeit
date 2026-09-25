# Plan: task tracker basics

The sandbox app (`codeit-sandbox-app`) is a small task tracker: a React + Vite frontend,
an Express + TypeScript API and SQLite. Today it can only list tasks. This plan makes it
useful for one person managing their own work.

## Features

1. **Create tasks.** A form on the task list adds a task with a title (required, at most
   120 characters) and an optional description. The new task appears at the top of the
   list without a page reload.

2. **Complete and reopen tasks.** Each task has a checkbox. Checking it marks the task
   done and shows it struck through; unchecking reopens it. The state survives a reload.

3. **Due dates.** A task can have an optional due date. Overdue open tasks are shown in
   red, and the list can be sorted by due date (tasks without one go last).

4. **Filter by status.** Tabs above the list show All, Open and Done tasks. The chosen
   tab is kept in the URL so a reload or shared link keeps it.

5. **Edit and delete.** A task's title, description and due date can be edited inline.
   Deleting asks for confirmation and cannot delete a task that another task depends on
   (see feature 6).

6. **Task dependencies.** A task can be marked as blocked by other tasks. Blocked tasks
   show which tasks block them and cannot be completed until those are done.

## Constraints

- The API validates every input and returns 400 with a JSON error body on bad input.
- Every feature needs unit tests for the API and Playwright tests for what the user sees.
