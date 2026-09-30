"""Eval workspaces: a fresh clone of the target at the suite's base commit, per task and
repeat (PRD 17.2 step 1). They live under `data/evals/work/` and are removed after scoring
unless kept for debugging."""

from __future__ import annotations

import shutil
from pathlib import Path

from codeit.config import Config
from codeit.git import Git
from codeit.sandbox.clone import BOT_EMAIL, BOT_NAME, CloneManager, Prepared

EVAL_LOCKFILES = ("package-lock.json",)


class Workspaces:
    def __init__(self, cfg: Config, repo_url: str, git: Git | None = None) -> None:
        self.git = git or Git()
        repo = cfg.project.target_repo
        self.clones = CloneManager(cfg.data_dir, repo.name, repo_url, repo.default_branch, self.git)
        self.root = cfg.data_dir / "evals" / "work"

    async def prepare(self, name: str, base_commit: str, branch: str) -> Prepared:
        mirror = await self.clones.refresh_mirror()
        path = self.root / name
        shutil.rmtree(path, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.git.run("clone", "--no-checkout", "--quiet", str(mirror), str(path))
        await self.git.run("remote", "set-url", "origin", self.clones.remote_url, cwd=path)
        await self.git.run("config", "user.name", BOT_NAME, cwd=path)
        await self.git.run("config", "user.email", BOT_EMAIL, cwd=path)
        await self.git.run("checkout", "--quiet", "-B", branch, base_commit, cwd=path)
        return Prepared(path, branch, created=True, head=base_commit)

    async def apply(self, path: Path, patch: Path, message: str) -> str:
        """Apply `patch` (an absolute path) and commit it; returns the new head."""
        await self.git.run("apply", "--index", str(patch), cwd=path)
        await self.git.run("commit", "--quiet", "--no-verify", "-m", message, cwd=path)
        return await self.git.run("rev-parse", "HEAD", cwd=path)

    async def diff_lines(self, path: Path, base: str) -> int:
        """Lines added plus removed since `base`, working tree included, lockfiles excluded."""
        excludes = [f":(exclude){f}" for f in EVAL_LOCKFILES]
        out = await self.git.run("diff", "--numstat", base, "--", ".", *excludes, cwd=path)
        total = 0
        for line in out.splitlines():
            added, removed, *_ = line.split("\t")
            if added.isdigit() and removed.isdigit():
                total += int(added) + int(removed)
        return total

    def remove(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
