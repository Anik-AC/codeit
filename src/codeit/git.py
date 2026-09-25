"""Async wrapper over the `git` CLI (PRD 0.8, 22). No GitPython.

A GitHub token, when given, is passed through `GIT_CONFIG_*` environment variables as an
HTTP header, so it never appears in argv (visible to other processes) or in `.git/config`
(which is mounted into worker containers).
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

from codeit.log import get_logger

log = get_logger(__name__)


class GitError(Exception):
    def __init__(self, args: list[str], code: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({code}): {stderr.strip()[:500]}")
        self.code = code
        self.stderr = stderr


class Git:
    def __init__(self, token: str | None = None, binary: str = "git") -> None:
        self._binary = binary
        self._env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if token:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            self._env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}",
            )

    async def run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            *args,
            cwd=cwd,
            env=self._env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        code = proc.returncode or 0
        if check and code != 0:
            raise GitError(list(args), code, err.decode(errors="replace"))
        return out.decode(errors="replace").strip()

    async def ok(self, *args: str, cwd: Path | None = None) -> bool:
        """Run a command whose exit code is the answer (e.g. `rev-parse --verify`)."""
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            *args,
            cwd=cwd,
            env=self._env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0
