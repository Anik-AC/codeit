# 0016. Fast lane: skip the Reviewer agent

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

The owner wants a switch for times of fast delivery: skip the second review stage (the Reviewer agent) and go straight to human review, controlled from the dashboard. The PRD has no such mode: every PR goes through Agent Review (PRD 6.2, 11.3).

## Decision

- **The switch:** one runtime setting, `fast_lane`, stored in `data/settings.json`. It is global, not per ticket, because the need is "for now, ship faster". It survives restarts. It can be set from:
  - the dashboard's Fast lane control on the Agents page (`PATCH /api/settings`)
  - `codeit fast-lane on|off`, which works with or without `codeit up` running; a running orchestrator reads the file every loop
- **While it is on, the scheduler:**
  - starts no Reviewer runs; the reviewers show as waiting, with the reason
  - moves every ticket in Agent Review that no run holds straight to Human Review, with a Jira comment and the `fast-lane` label
- **Turning it on** moves the waiting tickets at once rather than on the next loop.
- **Nothing is interrupted:** a review already running finishes and routes as usual.
- **Kept on purpose:**
  - GitHub CI must still pass (branch protection) and the owner still merges
  - the Coder's Stop hook still runs the unit tests
  - so the fast lane removes the agent review, not the tests
- **Visibility:**
  - the `fast-lane` label marks every ticket that skipped review
  - a chip in the dashboard's nav bar shows while the lane is on
  - the pipeline strip draws the bypass from Code to You
- **Confirmation:** turning the lane on from the dashboard asks for confirmation; turning it off does not.

## Consequences

- PRs reach the owner a few minutes sooner, without the Reviewer's acceptance-criteria check. The owner reviews them knowing that, from the label.
- The review loop (`Review Loop`, the 3-bounce cap) does not apply while the lane is on.
- Evals are not affected; `codeit eval review` always runs the Reviewer.
