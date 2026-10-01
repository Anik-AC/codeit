# Plan: tags and search

The sandbox task tracker lists, creates and completes tasks. People with more than a few
dozen tasks cannot find anything. This plan adds tags and search.

## Features

1. **Tags.** A task can have up to five tags (lowercase words, at most 20 characters
   each). Tags are added in the create form and shown as chips on each task.

2. **Filter by tag.** Clicking a tag chip shows only tasks with that tag; a clear button
   removes the filter. The filter is kept in the URL.

3. **Search.** A search box above the list filters tasks by title and description as the
   user types (case-insensitive, at least 2 characters). The API supports `GET
   /api/tasks?q=...`.

4. **Empty results.** When a search or tag filter matches nothing, the list says so and
   offers to clear the filter.

## Constraints

- Tag and search input is validated on the API; bad input returns 400 with a JSON error.
- Search must stay fast with 5,000 tasks (use an index; no full table scans in a loop).
