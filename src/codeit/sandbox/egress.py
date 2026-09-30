"""Egress allowlist for worker containers (PRD 10.3, ADR-0013).

Workers join `codeit-workers`, an internal Docker network with no route to the internet or
the host. The proxy container `codeit-proxy` sits on that network and on `codeit-egress`
(a normal bridge), and tinyproxy forwards only:

- HTTPS (CONNECT on 443) and plain HTTP to the hosts in `sandbox.egress_allowlist`
- plain HTTP to jira-mcp on the host, and to that port only

Workers get `HTTP(S)_PROXY` pointing at the proxy, with `NO_PROXY` for localhost so a
target repo's e2e tests can still reach their own dev server.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import NotFound

from codeit.config import Config
from codeit.log import get_logger

log = get_logger(__name__)

PROXY_NAME = "codeit-proxy"
WORKERS_NETWORK = "codeit-workers"
OUT_NETWORK = "codeit-egress"
PROXY_PORT = 8888
CONF_MOUNT = "/etc/codeit"
PROXY_CONTEXT = Path(__file__).resolve().parents[3] / "sandbox" / "proxy"
_HOST_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)

TINYPROXY_CONF = f"""\
Port {PROXY_PORT}
Listen 0.0.0.0
Timeout 900
LogLevel Connect
MaxClients 200
Allow 0.0.0.0/0
DisableViaHeader Yes
ConnectPort 443
Filter "{CONF_MOUNT}/allow.txt"
FilterType ere
FilterURLs On
FilterDefaultDeny Yes
"""


class EgressError(Exception):
    pass


@dataclass(frozen=True)
class EgressSettings:
    allowlist: tuple[str, ...]
    mcp_host: str
    mcp_port: int
    conf_dir: Path
    image: str

    @classmethod
    def from_config(cls, cfg: Config) -> EgressSettings | None:
        if not cfg.sandbox.egress_proxy:
            return None
        return cls(
            allowlist=tuple(cfg.sandbox.egress_allowlist),
            mcp_host=cfg.mcp.container_host,
            mcp_port=cfg.mcp.port,
            conf_dir=cfg.data_dir / "proxy",
            image=cfg.sandbox.proxy_image,
        )


def _escape(host: str) -> str:
    if not _HOST_RE.match(host):
        raise EgressError(f"egress allowlist entry {host!r} is not a plain host name")
    return host.replace(".", r"\.")


def filter_patterns(allowlist: Sequence[str], mcp_host: str, mcp_port: int) -> list[str]:
    """tinyproxy filters (ERE on the URL; for CONNECT the URL is `host:port`)."""
    patterns = [rf"^(https?://)?{_escape(h)}(:(80|443))?(/|$)" for h in allowlist]
    patterns.append(rf"^http://{_escape(mcp_host)}:{mcp_port}/")
    return patterns


def proxy_env() -> dict[str, str]:
    """Environment for a worker container behind the proxy. Both spellings, since tools
    disagree; `NODE_USE_ENV_PROXY` makes Node's built-in fetch honour it too."""
    url = f"http://{PROXY_NAME}:{PROXY_PORT}"
    no_proxy = "localhost,127.0.0.1,::1"
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
        "NODE_USE_ENV_PROXY": "1",
    }


def write_conf(settings: EgressSettings) -> str:
    """Write tinyproxy's config and filter file. Returns a hash of both."""
    allow = "\n".join(filter_patterns(settings.allowlist, settings.mcp_host, settings.mcp_port))
    settings.conf_dir.mkdir(parents=True, exist_ok=True)
    (settings.conf_dir / "tinyproxy.conf").write_text(TINYPROXY_CONF, encoding="utf-8")
    (settings.conf_dir / "allow.txt").write_text(allow + "\n", encoding="utf-8")
    return hashlib.sha256((TINYPROXY_CONF + allow).encode()).hexdigest()[:16]


def _network(client: docker.DockerClient, name: str, *, internal: bool) -> None:
    try:
        client.networks.get(name)
    except NotFound:
        client.networks.create(
            name, driver="bridge", internal=internal, labels={"codeit": "egress"}
        )


def ensure_proxy(client: docker.DockerClient, settings: EgressSettings) -> str:
    """Make sure the networks and a proxy with the current allowlist are up. Returns the
    network workers should join."""
    try:
        client.images.get(settings.image)
    except docker.errors.ImageNotFound as e:
        raise EgressError(
            f"proxy image {settings.image} is missing; run `codeit sandbox build`"
        ) from e
    conf_hash = write_conf(settings)
    _network(client, WORKERS_NETWORK, internal=True)
    _network(client, OUT_NETWORK, internal=False)
    try:
        proxy = client.containers.get(PROXY_NAME)
        current = proxy.labels.get("codeit.conf_hash")
        if proxy.status == "running" and current == conf_hash:
            return WORKERS_NETWORK
        proxy.remove(force=True)
    except NotFound:
        pass
    proxy = client.containers.run(
        settings.image,
        detach=True,
        name=PROXY_NAME,
        network=OUT_NETWORK,
        volumes={str(settings.conf_dir.resolve()): {"bind": CONF_MOUNT, "mode": "ro"}},
        extra_hosts={settings.mcp_host: "host-gateway"},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        read_only=True,
        labels={"codeit": "egress", "codeit.conf_hash": conf_hash},
    )
    client.networks.get(WORKERS_NETWORK).connect(proxy)
    log.info("egress.proxy_started", container=proxy.short_id, hosts=len(settings.allowlist))
    return WORKERS_NETWORK
