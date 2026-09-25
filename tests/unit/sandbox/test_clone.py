"""CloneManager against real git, with a local bare repository standing in for GitHub."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeit.git import Git, GitError
from codeit.sandbox.clone import CloneManager, branch_name


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    sh("add", name, cwd=repo)
    sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", message, cwd=repo)
    return sh("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, Path]:
    """(bare 'GitHub' repo, a working copy used to push to it)."""
    bare = tmp_path / "remote.git"
    sh("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "upstream"
    sh("clone", str(bare), str(work), cwd=tmp_path)
    sh("checkout", "-b", "main", cwd=work)
    commit(work, "README.md", "hello\n", "init")
    sh("push", "origin", "main", cwd=work)
    return bare, work


@pytest.fixture
def manager(tmp_path: Path, remote: tuple[Path, Path]) -> CloneManager:
    return CloneManager(tmp_path / "data", "app", str(remote[0]))


def test_branch_name() -> None:
    assert branch_name("CODEIT-12", "Add due dates to tasks!") == "CODEIT-12-add-due-dates-to-tasks"
    long = branch_name("CODEIT-1", "x" * 30 + " and " + "y" * 30)
    assert long == "CODEIT-1-" + "x" * 30 + "-and-yyyyy"
    assert len(long.removeprefix("CODEIT-1-")) <= 40
    assert branch_name("CODEIT-2", "!!!") == "CODEIT-2"


async def test_new_clone(manager: CloneManager, remote: tuple[Path, Path]) -> None:
    prepared = await manager.prepare("CODEIT-5", "CODEIT-5-thing")
    assert prepared.created and not prepared.conflict
    path = prepared.path
    assert path == manager.path_for("CODEIT-5")
    assert (path / ".git").is_dir()  # self-contained, not a worktree pointer file
    assert sh("branch", "--show-current", cwd=path) == "CODEIT-5-thing"
    assert sh("remote", "get-url", "origin", cwd=path) == str(remote[0])
    assert (path / "README.md").read_text() == "hello\n"
    assert sh("config", "user.name", cwd=path) == "codeit-bot"
    assert (manager.mirror / "README.md").exists()  # the Planner reads the mirror


async def test_existing_remote_branch_is_resumed(
    manager: CloneManager, remote: tuple[Path, Path]
) -> None:
    _, work = remote
    sh("checkout", "-b", "CODEIT-6-x", cwd=work)
    pushed = commit(work, "wip.txt", "wip\n", "CODEIT-6: wip")
    sh("push", "origin", "CODEIT-6-x", cwd=work)
    prepared = await manager.prepare("CODEIT-6", "CODEIT-6-x")
    assert prepared.head == pushed


async def test_rework_rebases_onto_new_main(
    manager: CloneManager, remote: tuple[Path, Path]
) -> None:
    _, work = remote
    first = await manager.prepare("CODEIT-7", "CODEIT-7-a")
    commit(first.path, "feature.txt", "f\n", "CODEIT-7: feature")
    sh("-c", "user.name=t", "-c", "user.email=t@t", "push", "origin", "CODEIT-7-a", cwd=first.path)
    sh("checkout", "main", cwd=work)
    commit(work, "other.txt", "o\n", "unrelated change on main")
    sh("push", "origin", "main", cwd=work)

    again = await manager.prepare("CODEIT-7", "CODEIT-7-a")
    assert not again.created and not again.conflict
    assert (again.path / "other.txt").exists() and (again.path / "feature.txt").exists()
    assert sh("rev-list", "--count", "HEAD..origin/main", cwd=again.path) == "0"


async def test_rework_conflict_is_aborted(manager: CloneManager, remote: tuple[Path, Path]) -> None:
    _, work = remote
    first = await manager.prepare("CODEIT-8", "CODEIT-8-a")
    commit(first.path, "README.md", "ours\n", "CODEIT-8: ours")
    sh("push", "origin", "CODEIT-8-a", cwd=first.path)
    sh("checkout", "main", cwd=work)
    commit(work, "README.md", "theirs\n", "theirs on main")
    sh("push", "origin", "main", cwd=work)

    again = await manager.prepare("CODEIT-8", "CODEIT-8-a")
    assert again.conflict
    assert sh("status", "--porcelain", cwd=again.path) == ""  # no half-done rebase
    assert (again.path / "README.md").read_text() == "ours\n"


async def test_remove_and_list(manager: CloneManager) -> None:
    assert manager.list_clones() == {}
    await manager.prepare("CODEIT-9", "CODEIT-9-a")
    ages = manager.list_clones()
    assert list(ages) == ["CODEIT-9"] and ages["CODEIT-9"] < 1
    assert manager.remove("CODEIT-9") is True
    assert manager.remove("CODEIT-9") is False


async def test_git_errors_and_token_env(tmp_path: Path) -> None:
    git = Git(token="ghp_secret")
    env = git._env
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghp_secret" not in env["GIT_CONFIG_VALUE_0"]  # base64, and never in argv
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    with pytest.raises(GitError, match="not a git repository"):
        await git.run("status", cwd=tmp_path)
    assert await git.ok("status", cwd=tmp_path) is False
    assert await git.run("status", cwd=tmp_path, check=False) == ""


async def test_non_conflict_rebase_error_is_raised(
    manager: CloneManager, remote: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work = remote
    first = await manager.prepare("CODEIT-10", "CODEIT-10-a")
    sh("checkout", "main", cwd=work)
    commit(work, "other.txt", "o\n", "advance main")
    sh("push", "origin", "main", cwd=work)
    real_run = manager.git.run

    async def failing_rebase(*args: str, **kw: object) -> str:
        if args[:1] == ("rebase",) and args[1:2] != ("--abort",):
            raise GitError(list(args), 128, "fatal: something else")
        return await real_run(*args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(manager.git, "run", failing_rebase)
    with pytest.raises(GitError, match="something else"):
        await manager.prepare("CODEIT-10", "CODEIT-10-a")
    assert first.path.exists()
