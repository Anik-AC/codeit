"""Worker containers (PRD 10.3).

A container per run, created from the worker image and kept alive with `sleep infinity`.
Agents run inside it with `exec`, streaming stdout line by line. Only two host paths are
mounted: the ticket's clone at `/workspace` and the run directory at `/run/codeit`.

Secrets are set in the container's environment at creation (Docker SDK), so they never
appear on a command line. Containers run as `agent` (UID 1000) with every capability
dropped and `no-new-privileges`.

The container lifecycle uses the Docker SDK; streaming `exec` uses the `docker` CLI
through asyncio, which gives line-by-line output and a clean kill on timeout.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container

from codeit.config import Config
from codeit.log import get_logger

log = get_logger(__name__)

WORKSPACE = "/workspace"
RUN_DIR = "/run/codeit"
NETWORK = "codeit-sandbox"
LABEL = "codeit.run_id"


class SandboxError(Exception):
    pass


@dataclass(frozen=True)
class ContainerSpec:
    run_id: str
    role: str
    image: str
    workspace: Path
    run_dir: Path
    env: Mapping[str, str] = field(default_factory=dict)
    cpus: float = 2
    mem: str = "4g"
    pids_limit: int = 512

    @classmethod
    def for_role(
        cls,
        cfg: Config,
        *,
        run_id: str,
        role: str,
        workspace: Path,
        run_dir: Path,
        env: Mapping[str, str],
    ) -> ContainerSpec:
        s = cfg.sandbox
        return cls(
            run_id=run_id,
            role=role,
            image=s.image,
            workspace=workspace,
            run_dir=run_dir,
            env=dict(env),
            cpus=s.cpus,
            mem=s.mem,
            pids_limit=s.pids_limit,
        )


@dataclass
class ExecResult:
    exit_code: int | None = None
    timed_out: bool = False
    stderr: str = ""


class Sandbox:
    def __init__(self, client: docker.DockerClient | None = None, docker_bin: str = "docker"):
        self._client = client
        self._docker_bin = docker_bin

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.from_env()
            except docker.errors.DockerException as e:
                raise SandboxError(f"cannot reach Docker: {e}") from e
        return self._client

    def ensure_network(self) -> str:
        """A dedicated network for workers. M7 routes its egress through the proxy."""
        try:
            self.client.networks.get(NETWORK)
        except NotFound:
            self.client.networks.create(NETWORK, driver="bridge", labels={"codeit": "sandbox"})
        return NETWORK

    def start(self, spec: ContainerSpec) -> Container:
        spec.workspace.mkdir(parents=True, exist_ok=True)
        spec.run_dir.mkdir(parents=True, exist_ok=True)
        try:
            container: Container = self.client.containers.run(
                spec.image,
                command=["sleep", "infinity"],
                detach=True,
                name=f"codeit-{spec.role}-{spec.run_id.lower()}",
                user="1000:1000",
                working_dir=WORKSPACE,
                environment=dict(spec.env),
                volumes={
                    str(spec.workspace.resolve()): {"bind": WORKSPACE, "mode": "rw"},
                    str(spec.run_dir.resolve()): {"bind": RUN_DIR, "mode": "rw"},
                },
                network=self.ensure_network(),
                extra_hosts={"host.docker.internal": "host-gateway"},
                nano_cpus=int(spec.cpus * 1e9),
                mem_limit=spec.mem,
                pids_limit=spec.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                labels={LABEL: spec.run_id, "codeit.role": spec.role},
                init=True,
            )
        except (APIError, docker.errors.ImageNotFound) as e:
            raise SandboxError(f"cannot start {spec.image}: {e}") from e
        log.info("sandbox.started", run_id=spec.run_id, container=container.short_id)
        return container

    async def exec_lines(
        self,
        container_id: str,
        argv: list[str],
        *,
        stdin: str = "",
        workdir: str = WORKSPACE,
        timeout_s: float,
        result: ExecResult,
    ) -> AsyncIterator[str]:
        """Run `argv` in the container; yield stdout lines as they arrive.

        On timeout the exec is killed and `result.timed_out` is set. `result` also receives
        the exit code and stderr once the stream ends.
        """
        proc = await asyncio.create_subprocess_exec(
            self._docker_bin,
            "exec",
            "-i",
            "-w",
            workdir,
            container_id,
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,  # stream-json lines can be large
        )
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(stdin.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        stderr_task = asyncio.create_task(proc.stderr.read())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                line = await asyncio.wait_for(proc.stdout.readline(), remaining)
                if not line:
                    break
                yield line.decode(errors="replace").rstrip("\n")
            result.exit_code = await proc.wait()
        except TimeoutError:
            result.timed_out = True
            proc.kill()
            await proc.wait()
            # `docker exec` dying does not stop the process inside; the caller stops the
            # container, which does.
        finally:
            result.stderr = (await stderr_task).decode(errors="replace")

    def stop(self, container: Container, log_path: Path | None = None) -> None:
        """Save the container's logs, then remove it."""
        try:
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_bytes(container.logs(stdout=True, stderr=True))
            container.remove(force=True)
        except NotFound:
            pass

    def reap(self) -> list[str]:
        """Remove every CodeIt worker container, e.g. left behind by a crash."""
        removed = []
        for c in self.client.containers.list(all=True, filters={"label": LABEL}):
            c.remove(force=True)
            removed.append(str(c.name))
        return removed


def build_image(context: Path, tag: str) -> int:
    """`docker build` with BuildKit and live progress. Returns the exit code."""
    return subprocess.call(["docker", "build", "-t", tag, str(context)])  # noqa: S603, S607


def image_exists(client: Any, tag: str) -> bool:
    try:
        client.images.get(tag)
        return True
    except docker.errors.ImageNotFound:
        return False
