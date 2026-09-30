"""Per-ticket clones (PRD 10.2, ADR-0002).

- The mirror `data/repos/{repo}` is a normal clone of the target repo, kept at
  `origin/{default}`. The Planner reads its CLAUDE.md and file tree.
- Each ticket gets `data/worktrees/{repo}/{KEY}`: a local clone of the mirror (objects are
  hardlinked), with `origin` pointed at GitHub. Its `.git` is self-contained, so mounting
  only that directory into a container is enough.
- Rework reuses the clone: fetch, check out the ticket branch, and rebase onto the default
  branch if behind. A conflicting rebase is aborted and reported, never left half done.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from codeit.git import Git, GitError
from codeit.log import get_logger

log = get_logger(__name__)

SLUG_MAX = 40
BOT_NAME = "codeit-bot"
BOT_EMAIL = "bot@users.noreply.github.com"


def branch_name(key: str, summary: str) -> str:
    """`{KEY}-{slug}`: summary lowercased, non-alphanumerics to `-`, cut to 40 (PRD 8)."""
    slug = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")
    slug = slug[:SLUG_MAX].rstrip("-")
    return f"{key}-{slug}" if slug else key


@dataclass(frozen=True)
class Prepared:
    path: Path
    branch: str
    created: bool  # a new clone, as opposed to rework on an existing one
    conflict: bool = False  # rework rebase hit conflicts and was aborted
    head: str = ""


class CloneManager:
    def __init__(
        self,
        data_dir: Path,
        repo_name: str,
        remote_url: str,
        default_branch: str = "main",
        git: Git | None = None,
    ) -> None:
        self.repo_name = repo_name
        self.remote_url = remote_url
        self.default_branch = default_branch
        self.mirror = data_dir / "repos" / repo_name
        self.clones = data_dir / "worktrees" / repo_name
        self.git = git or Git()

    def path_for(self, key: str) -> Path:
        return self.clones / key

    async def refresh_mirror(self) -> Path:
        """Clone the target repo once, then keep it at `origin/{default}`. A mirror git
        cannot use (e.g. empty object files after a hard restart) is cloned again."""
        try:
            return await self._refresh_mirror()
        except GitError as e:
            if not (self.mirror / ".git").is_dir():
                raise
            log.warning("clone.broken_mirror_recloning", error=str(e)[:300])
            shutil.rmtree(self.mirror)
            return await self._refresh_mirror()

    async def _refresh_mirror(self) -> Path:
        base = f"origin/{self.default_branch}"
        if not (self.mirror / ".git").is_dir():
            self.mirror.parent.mkdir(parents=True, exist_ok=True)
            await self.git.run("clone", self.remote_url, str(self.mirror))
        else:
            await self.git.run("fetch", "--prune", "origin", cwd=self.mirror)
        await self.git.run("checkout", "-B", self.default_branch, base, cwd=self.mirror)
        await self.git.run("reset", "--hard", base, cwd=self.mirror)
        return self.mirror

    async def prepare(self, key: str, branch: str) -> Prepared:
        """A clone of the ticket's branch, ready to mount at /workspace."""
        await self.refresh_mirror()
        path = self.path_for(key)
        base = f"origin/{self.default_branch}"
        if (path / ".git").is_dir():
            try:
                return await self._rework(path, key, branch, base)
            except GitError as e:
                # A run killed mid-git (a crash, Docker restarting) can leave the clone
                # broken. What matters is on GitHub, so start over from there.
                log.warning("clone.broken_recloning", key=key, error=str(e)[:300])
        return await self._fresh(path, branch, base)

    async def _fresh(self, path: Path, branch: str, base: str) -> Prepared:
        shutil.rmtree(path, ignore_errors=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.git.run("clone", "--no-checkout", str(self.mirror), str(path))
        await self.git.run("remote", "set-url", "origin", self.remote_url, cwd=path)
        await self._set_identity(path)
        await self.git.run("fetch", "origin", cwd=path)
        # A branch already on GitHub (a crashed earlier run) is resumed, not recreated.
        start = f"origin/{branch}" if await self._remote_has(path, branch) else base
        await self.git.run("checkout", "-B", branch, start, cwd=path)
        return Prepared(path, branch, created=True, head=await self._head(path))

    async def _rework(self, path: Path, key: str, branch: str, base: str) -> Prepared:
        await self._set_identity(path)
        # Uncommitted edits are leftovers of a run that died; committed work is kept.
        await self.git.run("reset", "--hard", cwd=path)
        await self.git.run("clean", "-fd", cwd=path)
        await self.git.run("fetch", "origin", cwd=path)
        await self.git.run("checkout", branch, cwd=path)
        if await self._remote_has(path, branch):
            await self.git.run("reset", "--hard", f"origin/{branch}", cwd=path)
        behind = await self.git.run("rev-list", "--count", f"HEAD..{base}", cwd=path)
        if int(behind or 0) > 0:
            try:
                await self.git.run("rebase", base, cwd=path)
            except GitError:
                conflicted = await self.git.run(
                    "diff", "--name-only", "--diff-filter=U", cwd=path, check=False
                )
                await self.git.run("rebase", "--abort", cwd=path, check=False)
                if not conflicted:
                    raise
                log.warning("clone.rebase_conflict", key=key, branch=branch, files=conflicted)
                return Prepared(path, branch, False, conflict=True, head=await self._head(path))
        return Prepared(path, branch, created=False, head=await self._head(path))

    async def prepare_review(self, key: str, sha: str) -> Path:
        """A clone at exactly `sha` for the Reviewer, apart from the Coder's clone, so a
        review never sees or changes the Coder's working state."""
        await self.refresh_mirror()
        path = self.clones / f"{key}-review"
        try:
            return await self._checkout_review(path, sha)
        except GitError as e:
            if not (path / ".git").is_dir():
                raise
            log.warning("clone.broken_review_recloning", key=key, error=str(e)[:300])
            shutil.rmtree(path)
            return await self._checkout_review(path, sha)

    async def _checkout_review(self, path: Path, sha: str) -> Path:
        if not (path / ".git").is_dir():
            shutil.rmtree(path, ignore_errors=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            await self.git.run("clone", "--no-checkout", str(self.mirror), str(path))
            await self.git.run("remote", "set-url", "origin", self.remote_url, cwd=path)
        await self._set_identity(path)
        await self.git.run("fetch", "origin", cwd=path)
        await self.git.run("fetch", "origin", sha, cwd=path)
        await self.git.run("checkout", "--force", "--detach", sha, cwd=path)
        await self.git.run("clean", "-fd", cwd=path)
        return path

    async def _set_identity(self, path: Path) -> None:
        """Commits made here, including host-side rebases, are the bot's (PRD D5)."""
        await self.git.run("config", "user.name", BOT_NAME, cwd=path)
        await self.git.run("config", "user.email", BOT_EMAIL, cwd=path)

    async def _remote_has(self, path: Path, branch: str) -> bool:
        return await self.git.ok(
            "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=path
        )

    async def _head(self, path: Path) -> str:
        return await self.git.run("rev-parse", "HEAD", cwd=path)

    def remove(self, key: str) -> bool:
        path = self.path_for(key)
        if not path.exists():
            return False
        shutil.rmtree(path)
        return True

    def list_clones(self) -> dict[str, float]:
        """Ticket key -> age in days of each clone (by last modification)."""
        if not self.clones.is_dir():
            return {}
        now = time.time()
        return {
            p.name: (now - p.stat().st_mtime) / 86400
            for p in sorted(self.clones.iterdir())
            if p.is_dir()
        }
