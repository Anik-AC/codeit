# 0002. Per-ticket local clones instead of git worktrees

- **Status:** Accepted
- **Date:** 2026-09-24

## Context

PRD 10.2 used `git worktree add` for each ticket and bind-mounted only the worktree into the worker container. A worktree's `.git` is a file pointing at `<mirror>/.git/worktrees/<name>`, an absolute host path that is not mounted. Inside the container, every git command (commit, rebase, push) would fail.

The alternatives were:
1. Also mount the mirror's git dir at the same absolute path. This exposes every ref of the mirror to an untrusted container and couples containers to host paths.
2. Use a plain local clone per ticket.

## Decision

Each ticket gets a normal clone of the local mirror at `data/worktrees/{repo}/{KEY}`:

```
git clone data/repos/{repo} data/worktrees/{repo}/{KEY}   # hardlinks objects; fast on ext4
git -C ... remote set-url origin https://github.com/...
git -C ... fetch origin && git -C ... checkout -B {branch} origin/main
```

The clone has a self-contained `.git`, so mounting only that directory at `/workspace` is enough. The PRD still calls the directory the ticket's "worktree" as a generic term. The module is `sandbox/clone.py`.

## Consequences

- Git works inside containers with no extra mounts.
- Cleanup is `rm -rf` of the directory instead of `git worktree remove`.
- Disk use is slightly higher than worktrees, since the working files are duplicated. Objects are hardlinked, so the cost is small.
- `data/` must be on the Linux filesystem (PRD D1) for hardlinks and bind-mount speed.
