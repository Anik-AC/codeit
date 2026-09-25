---
name: write-unit-test
description: How to write unit tests here, one or more per acceptance criterion. Use for logic, API endpoints and components.
---

# Write a unit test

- **One behaviour per test**, named as a sentence: `it("returns 400 when the title is empty")`.
- **Map tests to criteria.** Each acceptance criterion has at least one test whose name
  makes the mapping obvious. Put the criterion's Given / When / Then in the arrange / act /
  assert structure.
- **API tests** call the Express app in memory with `supertest` and a fresh in-memory
  database (`openDb(":memory:")`) per test. Assert the status code and the JSON body.
- **Component tests** use Testing Library with jsdom (`/** @vitest-environment jsdom */` at
  the top of the file). Query by role or label, as a user would.
- **Cover the edges** the ticket names: empty input, too long, not found, conflicts.
- **No shared mutable state** between tests, no network, no real clock (use fixed dates).
- A test that passes before your change proves nothing: check it fails first.
