from __future__ import annotations

import asyncio
import json
import socket
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from codeit.config import Config
from codeit.db.models import Event, TicketCache
from codeit.jira_client import JiraClient
from codeit.orchestrator.api import COOKIE, ApiContext, create_app, session_value
from codeit.orchestrator.budget import Budget
from codeit.orchestrator.bus import EventBus
from codeit.orchestrator.scheduler import Job, RunRefused
from codeit.runs import record_run
from tests.unit.orchestrator.fake_jira import IDS, FakeJira

TOKEN = "s3cret-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def context(cfg: Config, engine: Engine, **kw: Any) -> ApiContext:
    return ApiContext(
        cfg,
        engine,
        TOKEN,
        EventBus(),
        Budget(cfg, engine, has_openrouter_key=True),
        static_dir=kw.pop("static_dir", None),
        tail_poll_s=0.02,
        keepalive_s=0.2,
        **kw,
    )


@pytest.fixture
def client(cfg: Config, engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(context(cfg, engine))) as c:
        yield c


def test_auth_bearer_cookie_and_host(client: TestClient) -> None:
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/runs", headers=BEARER).status_code == 200
    assert client.get("/api/session").json() == {"authenticated": False}

    assert client.post("/api/login", json={"token": "nope"}).status_code == 401
    r = client.post("/api/login", json={"token": TOKEN})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and TOKEN not in cookie
    assert client.get("/api/runs").status_code == 200  # the cookie works
    assert client.get("/api/session").json() == {"authenticated": True}
    client.post("/api/logout")
    assert client.get("/api/runs").status_code == 401

    evil = client.get("/api/session", headers={"host": "evil.example"})
    assert evil.status_code == 400  # DNS rebinding guard


def test_agents_and_slots(cfg: Config, engine: Engine) -> None:
    orch = MagicMock()
    orch.agent_states.return_value = [{"name": "coder-1", "state": "busy"}]
    orch.slots = {"coder": 1, "reviewer": 2}
    orch.set_slots.side_effect = lambda changes: {"coder": 1, "reviewer": 2, **changes}
    with TestClient(create_app(context(cfg, engine, orchestrator=orch))) as c:
        body = c.get("/api/agents", headers=BEARER).json()
        assert body["running"] and body["agents"][0]["state"] == "busy"
        r = c.patch("/api/agents/slots", headers=BEARER, json={"reviewer": 3})
        assert r.json()["slots"]["reviewer"] == 3
        orch.set_slots.side_effect = ValueError("reviewer: slots must be 0 to 10")
        r = c.patch("/api/agents/slots", headers=BEARER, json={"reviewer": 30})
        assert r.status_code == 400 and "0 to 10" in r.json()["detail"]


def test_without_an_orchestrator(client: TestClient) -> None:
    assert client.get("/api/agents", headers=BEARER).json()["running"] is False
    r = client.patch("/api/agents/slots", headers=BEARER, json={"coder": 1})
    assert r.status_code == 503


def test_tickets_runs_and_detail(client: TestClient, engine: Engine) -> None:
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        for key, status in (("CODEIT-1", "Human Review"), ("CODEIT-2", "Ready for Dev")):
            s.add(TicketCache(key=key, status=status, summary=key, updated_at=now))
    all_tickets = client.get("/api/tickets", headers=BEARER).json()
    assert {t["key"] for t in all_tickets} == {"CODEIT-1", "CODEIT-2"}
    ready = client.get("/api/tickets", params={"status": "Ready for Dev"}, headers=BEARER).json()
    assert [t["key"] for t in ready] == ["CODEIT-2"]

    record_run(engine, "R1", "coder", instance="coder-1", ticket_key="CODEIT-1", status="pr_opened")
    record_run(engine, "R2", "reviewer", instance="reviewer-1", ticket_key="CODEIT-1")
    with Session(engine) as s, s.begin():
        s.add(Event(run_id="R1", ts=now, type="transition", payload_json={"to": "Agent Review"}))
    runs = client.get("/api/runs", params={"role": "coder"}, headers=BEARER).json()
    assert [r["id"] for r in runs] == ["R1"]
    by_ticket = client.get("/api/runs", params={"ticket": "codeit-1"}, headers=BEARER).json()
    assert {r["id"] for r in by_ticket} == {"R1", "R2"}
    detail = client.get("/api/runs/R1", headers=BEARER).json()
    assert (
        detail["status"] == "pr_opened" and detail["events"][0]["payload"]["to"] == "Agent Review"
    )
    assert detail["has_transcript"] is False
    assert client.get("/api/runs/NOPE", headers=BEARER).status_code == 404


def test_transcript_pages(client: TestClient, engine: Engine, cfg: Config) -> None:
    record_run(engine, "R1", "coder", ticket_key="CODEIT-1", status="pr_opened")
    path = cfg.data_dir / "transcripts" / "R1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(5)) + "not json\n")
    first = client.get("/api/runs/R1/transcript", params={"limit": 4}, headers=BEARER).json()
    assert [line["n"] for line in first["lines"]] == [0, 1, 2, 3] and not first["done"]
    rest = client.get(
        "/api/runs/R1/transcript", params={"offset": first["next_offset"]}, headers=BEARER
    ).json()
    assert rest["lines"][-1] == {"type": "raw", "text": "not json"} and rest["done"]


def test_trigger_run(cfg: Config, engine: Engine) -> None:
    orch = MagicMock()
    orch.request_run = AsyncMock(
        return_value=Job("coder", "CODEIT-7", "coder-1", "R9", datetime.now(UTC))
    )
    with TestClient(create_app(context(cfg, engine, orchestrator=orch))) as c:
        ok = c.post("/api/runs/coder", headers=BEARER, json={"ticket_key": "codeit-7"})
        assert ok.status_code == 202 and ok.json()["run_id"] == "R9"
        orch.request_run.assert_called_once_with("coder", "CODEIT-7")
        assert c.post("/api/runs/coder", headers=BEARER, json={}).status_code == 400
        orch.request_run.side_effect = RunRefused("no free coder slot")
        busy = c.post("/api/runs/coder", headers=BEARER, json={"ticket_key": "CODEIT-8"})
        assert busy.status_code == 409 and busy.json()["detail"] == "no free coder slot"


def test_budget_and_evals(client: TestClient) -> None:
    budget = client.get("/api/budget", headers=BEARER).json()
    assert budget["claude"]["window"] == ["23:00-08:00"] and budget["openrouter"]["key"]
    assert client.get("/api/evals", headers=BEARER).json() == []


def test_dashboard_is_served(cfg: Config, engine: Engine, tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "index.html").write_text("<h1>CodeIt</h1>")
    with TestClient(create_app(context(cfg, engine, static_dir=tmp_path / "out"))) as c:
        assert "CodeIt" in c.get("/").text
    with TestClient(create_app(context(cfg, engine))) as c:
        assert "codeit dashboard build" in c.get("/").text


# live streams, over a real server ------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def serving(ctx: ApiContext) -> AsyncIterator[str]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(ctx), port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
    event = ""
    async for line in response.aiter_lines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            yield event, json.loads(line.removeprefix("data: "))


async def test_live_stream_delivers_bus_events(cfg: Config, engine: Engine) -> None:
    ctx = context(cfg, engine)
    async with serving(ctx) as base, httpx.AsyncClient(base_url=base) as http:
        cookies = {COOKIE: session_value(TOKEN)}
        async with http.stream("GET", "/api/stream", cookies=cookies) as r:
            assert r.status_code == 200
            events = sse_events(r)
            assert (await anext(events))[0] == "agent_state"  # snapshot first
            assert (await anext(events))[0] == "budget_update"
            started = asyncio.get_running_loop().time()
            ctx.bus.publish("agent_state", {"agents": [{"name": "coder-1", "state": "busy"}]})
            kind, data = await asyncio.wait_for(anext(events), 2)
            assert kind == "agent_state" and data["agents"][0]["state"] == "busy"
            assert asyncio.get_running_loop().time() - started < 2  # PRD M8: within 2 s


async def test_transcript_stream_follows_a_running_run(cfg: Config, engine: Engine) -> None:
    record_run(engine, "R1", "coder", ticket_key="CODEIT-1")  # status running
    path = cfg.data_dir / "transcripts" / "R1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"n": 0}) + "\n")

    async def agent_keeps_writing() -> None:
        await asyncio.sleep(0.1)
        with path.open("a") as f:
            f.write(json.dumps({"n": 1}) + "\n" + json.dumps({"n": 2}))  # last line partial
        await asyncio.sleep(0.1)
        with path.open("a") as f:
            f.write("\n")
        record_run(engine, "R1", "coder", status="pr_opened", ended_at=datetime.now(UTC))

    async with serving(context(cfg, engine)) as base, httpx.AsyncClient(base_url=base) as http:
        writer = asyncio.create_task(agent_keeps_writing())
        async with http.stream("GET", "/api/runs/R1/transcript/stream", headers=BEARER) as r:
            got = [e async for e in sse_events(r)]
        await writer
    assert [d["n"] for kind, d in got if kind == "line"] == [0, 1, 2]
    assert got[-1] == ("end", {"run_id": "R1", "lines": 3})


def test_session_value_changes_with_the_token() -> None:
    assert session_value("a") != session_value("b") and len(session_value("a")) == 64


async def test_trigger_starts_a_task_on_the_loop(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    """A real orchestrator: the route must run on the event loop to start the job."""
    from codeit.orchestrator.scheduler import Orchestrator

    fake_jira.add("CODEIT-3", "Agent Review")
    started = asyncio.Event()

    async def runner(*args: Any, **kw: Any) -> None:
        started.set()

    watcher = MagicMock()
    orch = Orchestrator(
        cfg,
        MagicMock(),
        IDS,
        engine=engine,
        jira=jira,
        budget=Budget(cfg, engine, has_openrouter_key=True),
        merge_watcher=watcher,
        sandbox=MagicMock(),
        runners={"reviewer": runner},  # type: ignore[dict-item]
        echo=lambda _: None,
    )
    ctx = context(cfg, engine, orchestrator=orch)

    def call(method: str, url: str, body: dict[str, Any]) -> tuple[int, Any]:
        # urllib, not httpx: respx (the fake Jira) intercepts every httpx request.
        req = urllib.request.Request(
            url,
            json.dumps(body).encode(),
            {**BEARER, "Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read())

    async with serving(ctx) as base:
        status, _ = await asyncio.to_thread(
            call, "POST", f"{base}/api/runs/reviewer", {"ticket_key": "CODEIT-3"}
        )
        assert status == 202
        await asyncio.wait_for(started.wait(), 2)
        _, slots = await asyncio.to_thread(
            call, "PATCH", f"{base}/api/agents/slots", {"reviewer": 1}
        )
        assert slots["slots"]["reviewer"] == 1
