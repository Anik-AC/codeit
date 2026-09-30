"""M7, live: worker egress goes only through the allowlisting proxy (PRD 10.3, ADR-0013).

Needs Docker with the worker and proxy images (`codeit sandbox build`). Starts a stand-in
HTTP server on the jira-mcp port if nothing listens there.
"""

from __future__ import annotations

import contextlib
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from ulid import ULID

from codeit.config import load_config
from codeit.sandbox.containers import ContainerSpec, ExecResult, Sandbox, image_exists
from mcp_servers.jira.app import port_in_use
from tests.conftest import REPO_ROOT

CFG = load_config(REPO_ROOT / "config" / "config.yaml")


def _ready() -> bool:
    try:
        client = Sandbox().client
        return image_exists(client, CFG.sandbox.image) and image_exists(
            client, CFG.sandbox.proxy_image
        )
    except Exception:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _ready(), reason="Docker, the worker or the proxy image missing"),
]


@contextlib.contextmanager
def stand_in_mcp() -> Iterator[None]:
    if port_in_use(CFG.mcp.host, CFG.mcp.port):
        yield
        return
    server = http.server.HTTPServer(
        (CFG.mcp.host, CFG.mcp.port), http.server.SimpleHTTPRequestHandler
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield
    finally:
        server.shutdown()


async def http_code(sandbox: Sandbox, cid: str, args: str) -> str:
    """curl's status code for `args` inside the container; 000 means no connection."""
    result = ExecResult()
    command = f'curl -s -m 15 -o /dev/null -w "%{{http_code}}" {args}'
    lines = [
        line
        async for line in sandbox.exec_lines(
            cid, ["bash", "-lc", command], timeout_s=30, result=result
        )
    ]
    return "".join(lines).strip()


async def test_allowlist_is_enforced(tmp_path: Path) -> None:
    sandbox = Sandbox()
    spec = ContainerSpec.for_role(
        CFG.model_copy(update={"data_dir": tmp_path}),
        run_id=str(ULID()),
        role="reviewer",
        workspace=tmp_path / "ws",
        run_dir=tmp_path / "io",
        env={},
    )
    container = sandbox.start(spec)
    cid = str(container.id)
    mcp = f"http://{CFG.mcp.container_host}:{CFG.mcp.port}"
    other = f"http://{CFG.mcp.container_host}:{CFG.mcp.port + 1}/"
    try:
        with stand_in_mcp():
            assert await http_code(sandbox, cid, "https://registry.npmjs.org/react") == "200"
            assert await http_code(sandbox, cid, "https://api.github.com/zen") == "200"
            assert await http_code(sandbox, cid, "https://example.com/") == "000"  # refused
            assert await http_code(sandbox, cid, "http://example.com/") == "403"  # filtered
            assert await http_code(sandbox, cid, f"{mcp}/") not in ("000", "403")
            assert await http_code(sandbox, cid, other) == "403"
            # No way around the proxy: the network itself has no route out.
            direct = "--noproxy '*' https://registry.npmjs.org/"
            assert await http_code(sandbox, cid, direct) == "000"
    finally:
        sandbox.stop(container)
