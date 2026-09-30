# 0015. Eval harness

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

M9 builds the evaluation harness (PRD 17):

- a golden suite of 12 tasks with hidden tests and reference patches
- a runner that scores the Coder with them
- the Reviewer's seeded-bug suite
- reports and a dashboard page

The acceptance criteria:

- reference patches pass their hidden tests, and base commits fail them
- `codeit eval run --repeats 3` completes and reports pass@1 and pass^3

The PRD leaves open how the tasks are pinned and written, how hidden tests are counted, and how evals share Claude with the orchestrator.

## Decision

### The golden suite (`evals/suites/golden/`)

- **Base commit:** every task starts from one commit of codeit-sandbox-app, `d576021` (`main` on 2026-09-30), set in `suite.yaml`. Later changes to the app never change the tasks. The two sandbox PRs still open (#9 version, #11 single task) were left out of the tasks on purpose.
- **12 tasks,** with points as PRD 17.1 asks:

  | Points | Tasks |
  |---|---|
  | 1 | T001 delete, T002 created dates, T003 `?limit=`, T004 description placeholder |
  | 2 | T005 create, T006 edit, T007 done flag + migration, T008 search box, T009 `?q=` search |
  | 3 | T010 add-task form (API + UI + e2e), T011 mark done from the page (migration + API + UI + e2e), T012 due dates and overdue |

  By kind: 6 API, 3 UI, 3 full-stack.
- **Tickets** are written like real Planner tickets, with a user story, Given/When/Then criteria and technical notes. They are exact about anything a hidden test checks: paths, status codes, error messages, labels and texts.
- **Hidden tests:**
  - Vitest, under `tests/unit/hidden/`, with one Playwright spec each for T010 and T011 under `e2e/hidden-*`.
  - They pass the target's own lint and typecheck when copied in.
  - They are copied into the workspace only when scoring.
- **Reference patches** include the tests a good PR would add and any updates to existing tests the change needs. For example, T002 changes a test that matched `"FirstDetails"` exactly, and T007 and T011 add `done` to test fixtures.
- **Owner review:** PRD 17.1 asks for every hidden test to be reviewed by the owner. That is a pending item for the owner.

### The seeded-bug suite (`mutants/`)

- **Two mutants per task,** 24 in total. Each is the reference patch with one planted defect.
- **Kinds:** off-by-one, missing check or null check, wrong status code, tests that assert nothing, UI not wired, logic error, a migration edited in place, and SQL injection. Each is described in `mutants.yaml`.
- **Each mutant's own tests pass,** as do lint and typecheck. Where a planted bug would have broken the reference's visible tests, the mutant adjusts those tests, as a careless author would. So the Reviewer must catch the bug from the diff and ticket, or with `new_tests_fail_on_base`, not from red CI.
- **The hidden tests fail on every mutant,** which proves the defect is real. The exception is `test_asserts_nothing`, where the code is correct and only the tests are empty.

### Scoring

- **Setup:** each run gets a fresh clone at the base commit (`data/evals/work/`). Hidden tests run in a worker container with no credentials, behind the egress proxy, after `npm ci`.
- **Counts** come from the test runners' JSON reports: Vitest's `--outputFile`, and Playwright's JSON reporter.
- **`hidden_total`:** each task stores the total number of hidden tests, written by `codeit eval verify --update`. A hidden file that does not even load, for example because it imports a module the change never created, therefore counts all its tests as failed, not as zero tests.
- **Scoring what the agent produced:** anything it left uncommitted is committed before scoring and reviewing.

### Running the Coder

- **Production code path minus Jira:** `run_coder_in`, extracted from local mode. The ticket comes from `ticket.md`, and the agent commits locally.
- **Named configs:** `evals/configs/<name>.yaml` (backend, model). Only `claude_code` exists until M13.
- **`steering_sha`:** 12 hex characters hashing the Coder prompts and the target's steering kit (`CLAUDE.md`, `.claude/`) at the base commit. Runs are compared and charted by it.
- **Sharing Claude with the orchestrator:**
  - an eval holds a `coder` lease named `EVAL-...` for its whole run, so the orchestrator counts it against `claude.max_concurrent_runs`
  - before each task it waits while another Claude run holds the slot
  - it respects the run window unless `--any-time`
  - it stops cleanly, keeping partial results, if Claude is parked or hits a usage limit
- **`--review`** also runs the Reviewer on each result, which gives `reviewer_first_pass`.

### Running the Reviewer

- **Shared code:** the Reviewer's phase 1 checks and its verdict were split out of `run_reviewer` (`run_phase1`, `judge`, `workspace_diff`), so the eval uses exactly the production logic, minus Jira, GitHub and CI.
- **What it reviews:** `codeit eval review` reviews each clean reference and each mutant.
- **Scoring:**
  - A mutant is caught if the verdict is `fail_critical`.
  - A clean patch is a false fail if the verdict is `fail_critical`.
  - No verdict counts as wrong either way.

### Reports

- **`codeit eval report [RUN]`** prints the summary and per-task results.
- **`--compare a,b`** compares the latest run of each config and saves the table to `data/evals/reports/`.
- **Storage:** results go to `eval_runs` and `eval_results` (PRD 13). The notes hold the Coder's status, hidden-test failures, and the checks and findings of any review.
- **Dashboard:** an Evals page charts pass@1 over time, one colour per steering hash, and each run has a detail page.

### Not in M9

- **Planner evals (PRD 17.5)** are not part of the plan's M9 scope. They move to M12, where the Learning agent needs them.

## Consequences

- A full Coder eval with 3 repeats is 36 Claude runs, several hours of subscription use. It belongs in the overnight run window, and is not run on every change.
- The suite is tied to the sandbox app's stack (Vitest, Playwright). Another target needs its own suite and commands in its `suite.yaml`.
- Reviewer evals cost little (DeepSeek), about 36 container runs of 1 to 2 minutes each.
