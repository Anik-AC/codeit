# evals

The evaluation harness (PRD 17, ADR-0015).

```
suites/golden/
  suite.yaml                 target repo, base commit, how hidden tests run
  tasks/T001-delete-task/
    task.yaml                id, title, points, kind, tags, hidden_total
    ticket.md                exactly what the Coder receives
    hidden_tests/            copied into the workspace only for scoring
    reference.patch          a known-good solution
    mutants/M1.patch, M2.patch, mutants.yaml   seeded bugs for the Reviewer
configs/default.yaml         a Coder configuration to evaluate (backend, model)
```

```bash
uv run codeit eval verify                    # base fails the hidden tests, reference passes
uv run codeit eval run --repeats 3           # the Coder on every task (Claude run window applies)
uv run codeit eval run --tasks T001,T004 --repeats 3 --review --any-time
uv run codeit eval review                    # the Reviewer on clean and seeded-bug patches
uv run codeit eval report                    # the latest run; or: report RUN_ID
uv run codeit eval report --compare default,other
```

## Adding a task

1. Branch the sandbox app at the suite's `base_commit` and implement the reference, with the tests a good PR would add.
2. Save `git diff <base_commit>` as `reference.patch`.
3. Write `ticket.md`, exact about anything the hidden tests check (paths, codes, messages, labels).
4. Put hidden tests under `hidden_tests/` with repo-relative paths (`tests/unit/hidden/...`, `e2e/hidden-...`).
5. Run `uv run codeit eval verify --tasks T0NN --update`.
6. Optionally add mutants: the reference plus one planted defect whose own tests still pass.
