from __future__ import annotations

import stat
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import docker
import pytest
from docker.errors import NotFound

from codeit.config import load_config
from codeit.sandbox import containers
from codeit.sandbox.containers import (
    LABEL,
    NETWORK,
    ContainerSpec,
    ExecResult,
    Sandbox,
    SandboxError,
    build_image,
    image_exists,
)
from tests.conftest import REPO_ROOT

CFG = load_config(REPO_ROOT / "config" / "config.yaml")


def spec(tmp_path: Path, **env: str) -> ContainerSpec:
    return ContainerSpec.for_role(
        CFG,
        run_id="01ABC",
        role="coder",
        workspace=tmp_path / "ws",
        run_dir=tmp_path / "io",
        env=env,
    )


def test_start_hardens_and_mounts_only_two_paths(tmp_path: Path) -> None:
    client = MagicMock()
    client.networks.get.side_effect = NotFound("no")
    Sandbox(client).start(spec(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="tok"))
    client.networks.create.assert_called_once()
    kw: dict[str, Any] = client.containers.run.call_args.kwargs
    assert client.containers.run.call_args.args == ("codeit-worker:0.1.0",)
    assert kw["user"] == "1000:1000"
    assert kw["cap_drop"] == ["ALL"]
    assert kw["security_opt"] == ["no-new-privileges"]
    assert kw["environment"] == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    assert {v["bind"] for v in kw["volumes"].values()} == {"/workspace", "/run/codeit"}
    assert kw["network"] == NETWORK
    assert kw["extra_hosts"] == {"host.docker.internal": "host-gateway"}
    assert (kw["nano_cpus"], kw["mem_limit"], kw["pids_limit"]) == (2_000_000_000, "4g", 512)
    assert kw["labels"][LABEL] == "01ABC"
    assert kw["command"] == ["sleep", "infinity"]
    assert (tmp_path / "ws").is_dir() and (tmp_path / "io").is_dir()


def test_start_failure(tmp_path: Path) -> None:
    client = MagicMock()
    client.containers.run.side_effect = docker.errors.ImageNotFound("gone")
    with pytest.raises(SandboxError, match="cannot start"):
        Sandbox(client).start(spec(tmp_path))


def test_stop_saves_logs_and_removes(tmp_path: Path) -> None:
    container = MagicMock()
    container.logs.return_value = b"log output"
    Sandbox(MagicMock()).stop(container, tmp_path / "logs" / "r.log")
    assert (tmp_path / "logs" / "r.log").read_bytes() == b"log output"
    container.remove.assert_called_once_with(force=True)
    gone = MagicMock()
    gone.remove.side_effect = NotFound("x")
    Sandbox(MagicMock()).stop(gone)  # already removed: no error


def test_reap() -> None:
    client = MagicMock()
    c = MagicMock()
    c.name = "codeit-coder-x"
    client.containers.list.return_value = [c]
    assert Sandbox(client).reap() == ["codeit-coder-x"]
    assert client.containers.list.call_args.kwargs["filters"] == {"label": LABEL}


def test_unreachable_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise docker.errors.DockerException("no socket")

    monkeypatch.setattr(containers.docker, "from_env", fail)
    with pytest.raises(SandboxError, match="cannot reach Docker"):
        _ = Sandbox().client


def test_image_exists_and_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = MagicMock()
    assert image_exists(client, "x") is True
    client.images.get.side_effect = docker.errors.ImageNotFound("x")
    assert image_exists(client, "x") is False
    calls: list[list[str]] = []
    monkeypatch.setattr(containers.subprocess, "call", lambda argv: calls.append(argv) or 0)
    assert build_image(tmp_path, "codeit-worker:9") == 0
    assert calls == [["docker", "build", "-t", "codeit-worker:9", str(tmp_path)]]


@pytest.fixture
def fake_docker(tmp_path: Path) -> Path:
    """A `docker` stand-in: `docker exec -i -w DIR ID CMD...` echoes stdin and args."""
    script = tmp_path / "docker"
    script.write_text(
        """#!/usr/bin/env python3
import sys, time
args = sys.argv[1:]
assert args[:2] == ["exec", "-i"] and args[2] == "-w"
workdir, container, cmd = args[3], args[4], args[5:]
data = sys.stdin.read()
if cmd[0] == "sleep":
    print("started", flush=True)
    time.sleep(float(cmd[1]))
print(f"workdir={workdir} container={container}", flush=True)
print(f"stdin={data}", flush=True)
print("", flush=True)
sys.stderr.write("warn\\n")
sys.exit(int(cmd[1]) if cmd[0] == "exit" else 0)
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def test_exec_lines_streams(fake_docker: Path) -> None:
    result = ExecResult()
    sandbox = Sandbox(MagicMock(), docker_bin=str(fake_docker))
    lines = [
        line
        async for line in sandbox.exec_lines(
            "c1", ["exit", "3"], stdin="the prompt", timeout_s=10, result=result
        )
    ]
    assert lines == ["workdir=/workspace container=c1", "stdin=the prompt", ""]
    assert result.exit_code == 3 and not result.timed_out
    assert result.stderr == "warn\n"


async def test_exec_lines_timeout(fake_docker: Path) -> None:
    result = ExecResult()
    sandbox = Sandbox(MagicMock(), docker_bin=str(fake_docker))
    lines = [
        line
        async for line in sandbox.exec_lines(
            "c1", ["sleep", "10"], stdin="", timeout_s=1, result=result
        )
    ]
    assert lines == ["started"]
    assert result.timed_out
