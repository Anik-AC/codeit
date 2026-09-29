---
name: implement-ticket
description: The whole procedure from a Jira ticket to a pull request. Use at the start of every ticket, new or rework.
---

# Implement a ticket

1. **Read the ticket.** Call `get_ticket` for the key you were given, then `get_comments`.
   List the acceptance criteria. If this is rework, read the feedback first and use the
   `address-feedback` skill.
2. **Read the code.** Start from `CLAUDE.md`, then the files the ticket or its technical
   notes mention. Find the existing tests for that area and follow their style.
3. **Plan briefly.** For each acceptance criterion, decide which code changes it needs
   and which test proves it. Keep the change as small as the ticket allows; do not fix
   unrelated things.
4. **Write the tests with the code.** Use `write-unit-test` for logic and API behaviour
   and `write-playwright-test` for anything a user sees in the browser. Check that each
   new test fails without your change (for example, run it before implementing).
5. **Run the checks** from `CLAUDE.md`: lint, type check, unit tests, and the end-to-end
   specs you touched. Fix everything until green. Do not skip or weaken tests.
6. **Commit** with `{KEY}: message`, in small logical commits.
7. **Open or update the PR** with the `open-pr` skill.
8. **Finish** with the RESULT line described in `open-pr`. Nothing after it.

If you cannot finish (the ticket is unclear, contradicts the code, or needs a decision),
add a Jira comment explaining exactly what is blocking, then finish with status `blocked`.
