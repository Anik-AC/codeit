---
name: write-playwright-test
description: How to write Playwright end-to-end tests for user-facing acceptance criteria.
---

# Write a Playwright test

- Specs live in `e2e/`. The Playwright config builds the client and starts the server
  with a fresh in-memory database and demo data, so tests start from a known state.
- **Locators:** `page.getByRole(...)`, `getByLabel`, `getByText` for content. Never CSS
  classes, XPath or `nth-child`.
- **Assertions:** web-first only, `await expect(locator).toBeVisible()`, `toHaveText`,
  `toHaveCount`. Never `waitForTimeout` or sleeps.
- **Test data:** create what a test needs through the UI or the API (`request` fixture)
  inside the test, so tests do not depend on each other or on run order.
- **One user journey per test**, named after what the user achieves.
- Run a single spec with `npx playwright test e2e/<file>.spec.ts`.
- If a test is flaky, fix the cause (a missing wait on the right locator), never add
  retries or timeouts.
