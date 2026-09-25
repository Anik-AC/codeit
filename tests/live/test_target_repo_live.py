"""M4.5 acceptance: the target repo's checks pass inside the worker image.

Clones the target repo from GitHub with the real CloneManager, starts a worker container
on the clone, and runs every `codeit.yaml` command in it, as agents and the Reviewer will.
No Claude call. Set CODEIT_TARGET_BRANCH to check a branch other than the default.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeit.config import Secrets, load_config
from codeit.git import Git
from codeit.sandbox.clone import CloneManager
from codeit.sandbox.containers import ContainerSpec, ExecResult, Sandbox, image_exists
from codeit.target import load_target_config
from tests.conftest import REPO_ROOT

CFG = load_config(REPO_ROOT / "config" / "config.yaml")


def _ready() -> bool:
    try:
        return image_exists(Sandbox().client, CFG.sandbox.image)
    except Exception:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _ready(), reason="Docker or the worker image is not available"),
]


async def test_target_repo_checks_pass_in_worker(tmp_path: Path) -> None:
    repo = CFG.project.target_repo
    token = Secrets(_env_file=REPO_ROOT / ".env").github_token_readonly
    clones = CloneManager(
        tmp_path / "data",
        repo.name,
        repo.url,
        os.environ.get("CODEIT_TARGET_BRANCH", repo.default_branch),
        Git(token.get_secret_value() if token else None),
    )
    prepared = await clones.prepare("LIVE-1", "LIVE-1-target-checks")
    if not (prepared.path / "codeit.yaml").exists():
        pytest.skip("the target repo has no codeit.yaml on this branch yet")
    target = load_target_config(prepared.path)

    sandbox = Sandbox()
    container = sandbox.start(
        ContainerSpec.for_role(
            CFG,
            run_id="LIVE1",
            role="reviewer",
            workspace=prepared.path,
            run_dir=tmp_path / "io",
            env={"CI": "1"},
        )
    )
    failures: list[str] = []
    try:
        for name, command in target.commands.in_order():
            result = ExecResult()
            output = [
                line
                async for line in sandbox.exec_lines(
                    str(container.id), ["bash", "-lc", command], timeout_s=600, result=result
                )
            ]
            if result.exit_code != 0 or result.timed_out:
                failures.append(
                    f"{name} ({command}): exit {result.exit_code}\n" + "\n".join(output[-30:])
                )
    finally:
        sandbox.stop(container)
    assert not failures, "\n\n".join(failures)
