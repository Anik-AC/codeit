# Plan: task stats for a dashboard widget

A separate team wants to show a small widget with task counts. They only need an API.

## Features

1. **Stats endpoint.** `GET /api/stats` returns the number of open tasks, done tasks and
   overdue open tasks, as JSON.

2. **Stats in the UI header.** The task list header shows "N open, M done".

## Constraints

- The endpoint needs no authentication (the app has none yet).
- Counts must match the list exactly, including tasks without a due date.
