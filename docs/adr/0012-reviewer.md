# 0012. Reviewer

- **Status:** Accepted
- **Date:** 2026-09-29

## Context

M6 builds the Reviewer (PRD 11.3) and the review routing (PRD 6.2). The PRD describes the two phases and the output schema. Several details were left open: the model, which checks block, how `new_tests_fail_on_base` runs, how CI is waited on, and how the Reviewer's own comments are kept out of the Coder's rework feedback.

## Decision

### Model

- **Model:** the owner chose `deepseek/deepseek-v4.1-flash` for `openrouter_paid_review`. The Reviewer is deliberately not an Anthropic model (PRD 9.3), so it does not share the Coder's blind spots. It can be changed with `OPENROUTER_MODELS_PAID_REVIEW` in `.env` (ADR-0008).
- **Invalid answers:** retried once with the validation error (`prompts/reviewer/retry.md`). If the backend is unavailable, the next backend in `routing.reviewer` is tried.
- **Big diffs:** a diff over 60 KB is first summarized per file, then the verdict call sees the summaries. Lockfiles are left out of the diff.
- **No model answer:** the review is marked incomplete and goes to Human Review with `needs-human`. Phase 1 results are still posted.

### Phase 1

- **Where it runs:** in a worker container on a detached checkout of the PR head, `data/repos/<repo>/{KEY}-review`. It is separate from the Coder's clone, so a review never touches the Coder's working tree.
- **Container environment:** the container gets only the read-only GitHub token and `CI=1`. The Reviewer needs no jira-mcp token and no Claude token.
- **Order:** install, lint, typecheck, unit and e2e run in order. If install fails, the rest are skipped.
- **Blocking checks:** `install`, `typecheck`, `unit`, `e2e` and `new_tests_fail_on_base`. PRD 11.3 does not list `install`, but when install fails no other check ran, so it blocks too.
- **Non-blocking checks:**
  - `lint`: a failure adds a **major** finding but does not force `fail_critical`.
  - `ci_status`: shown in the review but never forces a verdict. The local checks run the same commands, and CI can be slow or flaky.
- **`new_tests_fail_on_base`:**
  - It finds the test files the PR added or modified (`test_globs` in `codeit.yaml`).
  - It checks out the PR's **merge base**, rather than `origin/main`. That way it compares against what the PR started from, even if `main` moved since.
  - It brings back only those test files from the head and runs them. Files matching `e2e_globs` (default `e2e/**`) run with the e2e command; all others run with the unit command.
  - The head is always restored afterwards.
  - Result: if at least one test fails on the base, the check passes. If all pass, the check fails with `TESTS_DO_NOT_EXERCISE_CHANGE`. If the PR changes no test files, the check is skipped, and that is a critical finding when the ticket has acceptance criteria.
- **CI status:** the Reviewer waits up to 10 minutes, polling every 15 seconds, for the GitHub check runs on the head SHA. If they are still running at the end, the result is `pending`. `skipped` and `neutral` conclusions count as passing.

### Verdict and routing

- **Override:** each blocking failure becomes a critical finding placed before the model's findings, and forces `fail_critical`. The model's own findings are kept.
- **Routing (PRD 6.2):**
  - `pass` and `pass_with_notes` go to Human Review.
  - `fail_critical` adds 1 to `Review Loop`. Below `max_review_loops` (3), the ticket goes back to Ready for Dev. On the 3rd failure, it goes to Human Review with `needs-human`.
- **Human moves win:** before routing, the ticket is re-read. If a human moved it during the review, nothing is changed (PRD 12.5).

### Output

- **PR review:** posted with `GITHUB_TOKEN_AGENT` as a review with event `COMMENT`. It never uses `APPROVE` or `REQUEST_CHANGES`: a human approves and merges, and a blocking review from the bot would get in the way.
- **Jira comment:** a short version with the verdict, the loop count, the failed checks, the critical and major findings, and a link to the PR review.
- **Marker:** both end with `_(CodeIt reviewer, run <id>)_`.
  - The Coder's rework feedback skips PR reviews carrying this marker. The Jira comment already carries the findings, so the Coder would otherwise see each finding twice.
  - The Reviewer skips its own earlier comments when it collects human test suggestions.
- **Run record:** every run is recorded in `runs` with the verdict, the route, the check statuses, the model and the cost.

## Consequences

- A PR can reach Human Review only when its install, typecheck, unit and e2e checks pass locally, and its new tests fail without the change, or with a human's `needs-human` escalation.
- CI failures alone do not bounce a PR. A red CI is shown in the review and the Jira comment for the human.
- A review takes about 1 to 1.5 minutes on the sandbox app, most of it `npm ci` and Playwright.

## Live acceptance (2026-09-29)

`tests/live/test_reviewer_live.py`, 3 passed in 4 min 52 s. Each scenario made a real PR in codeit-sandbox-app plus a ticket, and removed both afterwards:

1. **Failing test** (CODEIT-73, PR #4): `unit` failed, the verdict was `fail_critical`, and the ticket went to Ready for Dev with Review Loop 1. After Review Loop was set to 2 and the ticket returned to Agent Review, the 3rd failure moved it to Human Review with `needs-human` and Review Loop 3.
2. **Tautological test** (CODEIT-74, PR #5): every other check passed, but `new_tests_fail_on_base` failed with `TESTS_DO_NOT_EXERCISE_CHANGE`. The verdict was `fail_critical`, with the finding critical.
3. **Good PR** (CODEIT-75, PR #6): all checks passed, including CI. The model said `pass_with_notes`, and the ticket went to Human Review.

The PRD's accuracy target, with its catch rate and false-fail rate on a seeded-bug eval set, is measured once the eval harness exists in M9 (PRD 17.4).
