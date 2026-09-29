# Reviewer checklist

The Learning agent (M12) may edit this file from review feedback. Keep each item one line.

- Every acceptance criterion has at least one test that would fail without the change.
- Tests assert behaviour a user or caller sees, not implementation details.
- Playwright tests use role or label locators and web-first assertions, no sleeps.
- API input is validated; bad input returns 400 with a JSON error body.
- Unknown IDs return 404; nothing returns 500 for user mistakes.
- No existing test was deleted, skipped or weakened.
- No CI configuration, lockfile or `.claude/` change unless the ticket asks for it.
- No secrets, tokens or `.env` files committed.
- Database changes are new migrations, never edits to old ones.
- User-facing text matches the ticket's wording exactly.
- Code follows the repository's CLAUDE.md conventions.
