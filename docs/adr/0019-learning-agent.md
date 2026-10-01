# 0019. Learning agent

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

M12 builds the Learning agent (PRD 11.7) and the Planner evals that ADR-0015 moved here. The agent reads feedback on the agents' work, finds lessons that recur, proposes steering changes as pull requests, and scores each change with an eval before anyone merges it. The PRD gives the signals, the themes, the qualification rule, the target files and the gate. It leaves open:

- how signals are told apart from CodeIt's own messages
- how lessons are matched across runs
- how much of a steering file the model may change
- where CodeIt's own prompt changes go when no token can push to the CodeIt repo
- how a gate that needs Claude fits the run window

## Decision

### Signals (`agents/learning/signals.py`)

- **Human signals:**
  - Jira comments on tickets updated in the last `learning.lookback_days` (30). A comment on a Rejected ticket is a `jira_rejection`.
  - Reviews and comments on the target repo's agent PRs (branches named `{KEY}-...`).
- **CodeIt signals:**
  - critical findings of Reviewer runs that ended `fail_critical`, except bare check failures ("Check `unit` failed")
  - failed results of the latest finished run of each eval suite, except gate runs. Older runs scored older prompts, and their failures may be fixed already. The first accidental run learned from the M9 tuning runs, which is how this rule was found.
- **CodeIt's own comments are left out:** those carrying the orchestrator or reviewer mark, or an agent's "Posted by" signature.
  - Jira hands `_mark_` back as `*mark*`, so detection now ignores the emphasis around a mark.
  - This also fixes the Coder, which had been reading orchestrator status comments as rework feedback.
- **Dedup:** each signal has an `external_id` (`jira-comment:{id}`, `gh-review:{id}`, `review:{run}:{i}`, `eval:{run}:{task}:{repeat}`), so collecting again adds only what is new.
- **`#learn`:** a human writes `#learn` in a comment (or labels the ticket `learn`) to have one signal count on its own.

### Lessons (`lessons.py`)

- One model call per 40 new signals (`routing.learning`, the Claude subscription) marks each signal with:
  - `actionable`
  - a target agent (`coder`, `reviewer` or `planner`)
  - one of the PRD's themes
  - a one-line lesson
  - a kebab-case `lesson_key`
- The call sees the lessons already open, so the same lesson keeps its key across runs.
- **Qualification:** a lesson qualifies with two or more signals, or one with `#learn`.
- **Signal states** (`signals.status`): `new`, then `classified` or `ignored`; later `addressed` (a proposal took it up) or `skipped` (the model declined it). Unqualified lessons stay open and can qualify in a later run.

### Proposals (`steering.py`, `run.py`)

- **One call per agent** with qualifying lessons. It gets the steering file's current text and the evidence, and returns one-line rules (or a skip with a reason).
- **CodeIt writes the rules**, under one managed heading, `## Learned from feedback`, at the end of the file.
  - The model never rewrites what people wrote.
  - Rules with template syntax, headings, or text already in the file are dropped.
- **Where rules go:**

| Agent | File | Repo |
|---|---|---|
| Coder | `CLAUDE.md` | target (a PR on `learning/{date}-{run}`) |
| Reviewer | `prompts/reviewer/checklist.md` | CodeIt |
| Planner | `prompts/planner/system.md` | CodeIt |

- **CodeIt's own repo:** the GitHub tokens are scoped to the sandbox (owner decision), so the agent commits to a separate clone (`data/repos/codeit-self`) and writes `data/learning/{proposal}/codeit.patch` and `pr.md`.
  - With an optional `GITHUB_TOKEN_CODEIT` (contents and pull requests on the CodeIt repo only), it pushes and opens a PR instead.
  - Adding that token is the owner's call.
- **The PR body** lists each rule with its lesson, count, expected effect and evidence (with links), plus what was skipped and why. A run from a fixture says so at the top.
- **Never merges.**

### The eval gate (`gate.py`)

- **A subset, run before and after,** so the comparison is paired and fits a night:
  - **Coder:** the golden Coder eval on `learning.gate_coder_tasks` (T004, T005, T010, one repeat), with the current and the proposed `CLAUDE.md` written over each workspace. The steering commit becomes the diff base.
  - **Reviewer:** the seeded-bug eval on `learning.gate_review_tasks` (six tasks, 18 variants).
  - **Planner:** the Planner eval (below).
- **No checkout:** proposed prompts are loaded through `prompts.overlay(dir)`, which makes `render`, `prompt_path` and `prompt_hash` see the proposed files inside the block only.
- **Metrics compared:**
  - Coder: pass@1 and hidden-test ratio
  - Reviewer: planted bugs caught, and correct patches failed (lower is better)
  - Planner: valid plans, INVEST and coverage
- **Regression:** a metric worse by more than `eval.regression_tolerance` (0.05). The PR gets the `regression` label.
- **The table goes in the PR's `## Eval gate` section** (or in `pr.md`). Finished parts are shown while others wait.
- **Waiting for Claude:** the Coder part needs Claude, and waits for the run window as evals always do (ADR-0015). Until then the proposal stays `pending`.
- **Usage limits and outages:** a model error in a part (such as Claude's session limit) leaves that part pending instead of failing the run.
- **Mixed models:** before and after runs made by different models (Claude limited, so the free backup answered one side) are not compared; that part runs again.
- **`codeit learning gate`** finishes pending gates without collecting or classifying anything.
- **Gate runs are marked:** config names start with `gate-`, and they are never signals.

### Planner evals (`evals/planner.py`, `codeit eval planner`)

- **The suite:** three sample plans in `evals/suites/planner/plans` (a large, a medium and a small one), drafted against a fixed snapshot of the sandbox's `CLAUDE.md` and file tree at the golden base commit.
- **Metrics:**
  - `schema_valid` and `first_try_valid`
  - `invest`: the mean story score, from 0 to 1, from an LLM judge (`routing.ops`, rubric `evals/rubrics/invest.md`, which sees each story's test plan)
  - `coverage` of the plan
- **Left out:** the owner's approval rate and the edit distance of approved tickets. Both need real use, not a suite.

### Scheduling

`codeit up` starts the agent (lease `LEARNING`, one slot) when any of these hold:
- the `learning.cron` time (Sunday 22:00) has passed since the last run
- `learning.min_new_signals` (10) new human signals are waiting; they are counted every `learning.check_minutes` (60)
- a proposal's gate is pending and Claude may run now

`codeit run learning` runs it once:
- `--signals FILE` learns from a YAML of signals instead of collecting
- `--no-gate` skips the gate
- `--any-time` lets the Coder part of the gate ignore the window

The cron matcher is a small module (`codeit/cron.py`), not a dependency.

### Tests never touch real services

A CLI test that expected `run learning` to be a stub started a real run, which collected real signals and began a gate eval. Unit tests now run with `.env` ignored and every secret variable unset (`tests/unit/conftest.py`).

## Consequences

- **The gate is small and noisy.** Three Coder tasks with one repeat move pass@1 in steps of 0.33, so one flaky task marks a regression. The label means "look at this", not "reject". A larger subset or more repeats (config) trades Claude time for confidence.
- **A proposal whose Coder part waits** stays open with a partial table until the next night.
- **Reviewer and Planner rules need the owner:** they arrive as a patch unless `GITHUB_TOKEN_CODEIT` is added. The owner applies the patch (or pushes the branch) and opens the PR by hand.
- **Lesson keys depend on the model reusing them.** A lesson worded differently in a later run may start a new key. The open lessons in the prompt keep this rare, and `codeit learning status` shows them.
- **Rules only accumulate.** Removing or merging old rules is left to people reviewing the PRs.
