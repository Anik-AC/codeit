from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import docker
import pytest
from docker.errors import NotFound

from codeit.config import load_config
from codeit.sandbox.containers import ContainerSpec, Sandbox
from codeit.sandbox.egress import (
    OUT_NETWORK,
    PROXY_NAME,
    WORKERS_NETWORK,
    EgressError,
    EgressSettings,
    ensure_proxy,
    filter_patterns,
    proxy_env,
)
from tests.conftest import REPO_ROOT

CFG = load_config(REPO_ROOT / "config" / "config.yaml")


def allowed(url: str, patterns: list[str]) -> bool:
    # The patterns are plain enough that Python's re reads them like POSIX ERE.
    return any(re.search(p, url) for p in patterns)


def test_filter_allows_listed_hosts_and_only_the_mcp_port() -> None:
    patterns = filter_patterns(["registry.npmjs.org", "github.com"], "host.docker.internal", 8765)
    assert allowed("registry.npmjs.org:443", patterns)  # CONNECT
    assert allowed("https://registry.npmjs.org/react", patterns)
    assert allowed("github.com:443", patterns)
    assert not allowed("api.github.com:443", patterns)  # subdomains need their own entry
    assert not allowed("github.com.evil.io:443", patterns)
    assert not allowed("evilgithub.com:443", patterns)
    assert not allowed("github.com:22", patterns)
    assert allowed("http://host.docker.internal:8765/mcp", patterns)
    assert not allowed("http://host.docker.internal:8766/", patterns)
    assert not allowed("host.docker.internal:443", patterns)


def test_filter_rejects_odd_entries() -> None:
    with pytest.raises(EgressError, match="plain host name"):
        filter_patterns(["*.github.com"], "h", 1)


def test_proxy_env_keeps_localhost_direct() -> None:
    env = proxy_env()
    assert env["HTTPS_PROXY"] == env["http_proxy"] == f"http://{PROXY_NAME}:8888"
    assert "localhost" in env["NO_PROXY"] and env["NODE_USE_ENV_PROXY"] == "1"


def settings(tmp_path: Path) -> EgressSettings:
    cfg = CFG.model_copy(update={"data_dir": tmp_path})
    s = EgressSettings.from_config(cfg)
    assert s is not None
    return s


def test_ensure_proxy_creates_networks_and_proxy(tmp_path: Path) -> None:
    client = MagicMock()
    client.networks.get.side_effect = [NotFound("x"), NotFound("x"), MagicMock()]
    client.containers.get.side_effect = NotFound("x")
    assert ensure_proxy(client, settings(tmp_path)) == WORKERS_NETWORK
    created = {c.args[0]: c.kwargs["internal"] for c in client.networks.create.call_args_list}
    assert created == {WORKERS_NETWORK: True, OUT_NETWORK: False}
    kw: dict[str, Any] = client.containers.run.call_args.kwargs
    assert kw["name"] == PROXY_NAME and kw["network"] == OUT_NETWORK
    assert kw["cap_drop"] == ["ALL"] and kw["read_only"] is True
    allow = (tmp_path / "proxy" / "allow.txt").read_text()
    assert r"registry\.npmjs\.org" in allow and "8765" in allow
    assert "atlassian" not in allow  # never the Jira site


def test_ensure_proxy_reuses_a_current_proxy_and_replaces_a_stale_one(tmp_path: Path) -> None:
    s = settings(tmp_path)
    client = MagicMock()
    ensure_proxy(client, s)  # learn the config hash
    conf_hash = client.containers.run.call_args.kwargs["labels"]["codeit.conf_hash"]

    running = MagicMock(status="running", labels={"codeit.conf_hash": conf_hash})
    client = MagicMock()
    client.containers.get.return_value = running
    ensure_proxy(client, s)
    client.containers.run.assert_not_called()

    stale = MagicMock(status="running", labels={"codeit.conf_hash": "old"})
    client = MagicMock()
    client.containers.get.return_value = stale
    ensure_proxy(client, s)
    stale.remove.assert_called_once_with(force=True)
    client.containers.run.assert_called_once()


def test_missing_proxy_image(tmp_path: Path) -> None:
    client = MagicMock()
    client.images.get.side_effect = docker.errors.ImageNotFound("x")
    with pytest.raises(EgressError, match="codeit sandbox build"):
        ensure_proxy(client, settings(tmp_path))


def test_worker_joins_the_internal_network(tmp_path: Path) -> None:
    client = MagicMock()
    spec = ContainerSpec.for_role(
        CFG.model_copy(update={"data_dir": tmp_path}),
        run_id="01ABC",
        role="reviewer",
        workspace=tmp_path / "ws",
        run_dir=tmp_path / "io",
        env={"GH_TOKEN": "t"},
    )
    Sandbox(client).start(spec)
    worker = client.containers.run.call_args_list[-1]
    assert worker.args == ("codeit-worker:0.1.0",)
    assert worker.kwargs["network"] == WORKERS_NETWORK
    assert worker.kwargs["environment"]["GH_TOKEN"] == "t"
    assert worker.kwargs["environment"]["HTTPS_PROXY"] == f"http://{PROXY_NAME}:8888"
