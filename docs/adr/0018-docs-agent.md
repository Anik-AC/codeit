# 0018. Docs agent

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

M11 builds the Docs agent (PRD 11.6). Once a day it writes up the tickets merged since the last run as a changelog, a work log, ADRs and a README status block, in one PR. The PRD gives the inputs and outputs. It leaves open:

- which model writes the text
- how an ADR is detected
- how much of the output the model controls
- how the agent behaves on repeated runs

On 2026-09-30 the owner also moved every role except the Reviewer onto the Claude subscription.

## Decision

### Routing (owner, 2026-09-30)

- `routing.docs`, `routing.learning` and `routing.ops` use `claude_code_chat` (`claude -p` on the host with `--json-schema`), with `openrouter_free` as the backup when Claude is usage-limited.
- A role whose primary backend is `claude_code_chat` is not held by the OpenRouter caps or the Claude run window in `Budget.can_run`. It is a short text call, not a coding session, and does not count towards Claude concurrency.
- The Reviewer stays on paid OpenRouter models: it was tuned and evaluated there (ADR-0015). M13's fallback stays on OpenRouter by design.

### One model call writes, CodeIt assembles

- The model gets each ticket's:
  - summary, description and Epic
  - review loops and human returns
  - PR title and body
  - the Reviewer's summary: the latest PR review carrying the CodeIt reviewer mark
- It returns one JSON object:
  - `entries`: one per ticket, with a Keep a Changelog category and one user-facing sentence
  - `decisions`: design decisions worth an ADR; usually none
  - `highlights`: at most three lines for the work log
- **ADR detection happens in the same call.** The PRD asked for a separate ops-model classification call; one call sees the same evidence at half the cost and latency. A decision naming a ticket that is not in the batch is dropped.
- **Everything else is deterministic code** (`agents/docs.py`), so the model cannot break the files' structure:
  - changelog grouping by Epic, with `(KEY, #PR)` references
  - the work log table and the cost per role, taken from the `runs` table
  - ADR numbering (the next free `NNNN` in the target's `docs/adr/`) and the Nygard template
  - the README status block
- **A ticket the model leaves out** still gets a line, made from its summary under "Changed".

### Output and repeated runs

- **One branch per day:** `docs/{date}`, from `origin/main` (resumed if it already exists on GitHub), and one PR. An open PR for the branch is updated, not duplicated.
- **Same day again:**
  - new changelog groups go under the existing heading for that date
  - the work log gets another section after a rule
- **After the push,** each ticket gets the `docs-logged` label. The Docs JQL excludes that label, and the code skips labelled tickets too, since Jira's `!=` on labels is unreliable.
- **Status block rows:**
  - Tickets shipped: all Done tickets
  - Median review loops
  - Reviewer first-pass rate: the share of tickets whose first Reviewer verdict was not `fail_critical`
  - The latest Coder eval (pass@1 and pass^k, with its task count) and Reviewer eval (critical bugs caught, false fails), each only when one exists
- **Missing status markers:** the block is added under the README's first heading.

### Scheduling

- `codeit up` starts the Docs agent once per local day, at or after `docs.run_at` (default 07:00). This is at the end of the Claude window, after the night's merges.
- **A restart** the same day does not run it again: a finished docs run started today counts.
- **One run at a time:** the run leases `DOCS`, so a manual `codeit run docs` and the scheduler cannot overlap.
- **The dashboard** shows the agent as the "Scribe" (lime, a page whose lines write themselves while it works), with a slot control and a runs filter.

## Consequences

- **Only the target repo gets docs PRs.** The PRD also wanted a PR on the CodeIt repo itself. Both GitHub tokens are scoped to codeit-sandbox-app only (owner decision), so CodeIt's own worklog stays hand-written. Supporting it later means a token for the CodeIt repo and a second `target_repo`-style entry.
- **Docs are not reviewed by the Reviewer agent.** The PR is small and plain text, and the owner merges it.
- **Labels are written after the push.** A run that dies between the push and the labels leaves tickets unlabelled. The next run then writes them up again on the same branch, which is visible and harmless, rather than silently losing them.
- **Claude outages:** runs fall back to the free OpenRouter list, whose text quality is lower. The run records which backend wrote it.
