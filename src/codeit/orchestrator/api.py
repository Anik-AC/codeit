"""The dashboard API (PRD 14): FastAPI in the `codeit up` process, loopback only.

Auth: every `/api/*` route except login needs `CODEIT_API_TOKEN`, either as
`Authorization: Bearer <token>` or as the session cookie set by `POST /api/login`. The
cookie is HttpOnly and SameSite=Strict, so the browser's `EventSource` can use the live
streams without the token ever being in a URL, and other sites cannot ride on it.

Live data:
- `GET /api/stream`: SSE with `agent_state`, `run_event`, `ticket_update` and
  `budget_update`, starting with a snapshot of agents and budget.
- `GET /api/runs/{id}/transcript/stream`: SSE that follows a run's transcript file as the
  agent writes it, and ends once the run has finished.

The built dashboard (`dashboard/out`) is served at `/` when present.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from codeit.config import Config
from codeit.db.models import AgentInstance, EvalResult, EvalRun, Event, Run
from codeit.orchestrator import settings as runtime_settings
from codeit.orchestrator import tickets_cache
from codeit.orchestrator.budget import Budget
from codeit.orchestrator.bus import EventBus
from codeit.orchestrator.scheduler import Orchestrator, RunRefused, jsonable

COOKIE = "codeit_session"
KEEPALIVE_S = 15.0
TAIL_POLL_S = 0.5
MAX_RUNS = 200
MAX_LINES = 1000
DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "dashboard" / "out"


@dataclass
class ApiContext:
    cfg: Config
    engine: Engine
    token: str
    bus: EventBus
    budget: Budget
    orchestrator: Orchestrator | None = None
    static_dir: Path | None = DASHBOARD_DIR
    tail_poll_s: float = TAIL_POLL_S
    keepalive_s: float = KEEPALIVE_S
    # Set on shutdown: live streams end themselves instead of being cut off.
    closing: asyncio.Event = field(default_factory=asyncio.Event)


def session_value(token: str) -> str:
    """What the cookie holds: derived from the token, so a new token logs everyone out."""
    return hmac.new(token.encode(), b"codeit-dashboard-session", hashlib.sha256).hexdigest()


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(jsonable(data), separators=(',', ':'))}\n\n"


def run_summary(r: Run) -> dict[str, Any]:
    return {
        "id": r.id,
        "role": r.role,
        "instance": r.instance,
        "ticket_key": r.ticket_key,
        "status": r.status,
        "backend": r.backend,
        "model": r.model,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "turns": r.turns,
        "cost_usd": r.cost_usd,
    }


def authorized(request: Request) -> None:
    """Bearer token or session cookie; the app's context holds the token."""
    ctx: ApiContext = request.app.state.ctx
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and secrets.compare_digest(
        header.removeprefix("Bearer ").encode(), ctx.token.encode()
    ):
        return
    cookie = request.cookies.get(COOKIE, "")
    if cookie and secrets.compare_digest(cookie.encode(), session_value(ctx.token).encode()):
        return
    raise HTTPException(401, "log in with CODEIT_API_TOKEN")


Auth = Annotated[None, Depends(authorized)]


def eval_summary(r: EvalRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "suite": r.suite,
        "config": r.config_name,
        "steering_sha": r.steering_sha,
        "model": r.model,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "summary": r.summary_json,
    }


def eval_result(r: EvalResult) -> dict[str, Any]:
    try:
        notes = json.loads(r.notes) if r.notes else None
    except ValueError:
        notes = {"text": r.notes}
    return {
        "task_id": r.task_id,
        "repeat": r.repeat_idx,
        "passed": r.passed,
        "hidden_pass_ratio": r.hidden_pass_ratio,
        "turns": r.turns,
        "cost_usd": r.cost_usd,
        "duration_s": r.duration_s,
        "diff_lines": r.diff_lines,
        "reviewer_verdict": r.reviewer_verdict,
        "notes": notes,
    }


class LoginBody(BaseModel):
    token: str


class SettingsPatch(BaseModel):
    fast_lane: bool | None = None


class RunRequest(BaseModel):
    ticket_key: str | None = None


def create_app(ctx: ApiContext) -> FastAPI:
    app = FastAPI(title="CodeIt", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"]
    )
    app.state.ctx = ctx
    expected = session_value(ctx.token)

    def orchestrator() -> Orchestrator:
        if ctx.orchestrator is None:
            raise HTTPException(503, "the orchestrator is not running")
        return ctx.orchestrator

    # session -----------------------------------------------------------------------------

    @app.post("/api/login")
    def login(body: LoginBody, response: Response) -> dict[str, bool]:
        if not secrets.compare_digest(body.token.encode(), ctx.token.encode()):
            raise HTTPException(401, "wrong token")
        response.set_cookie(
            COOKIE, expected, httponly=True, samesite="strict", path="/", max_age=30 * 86400
        )
        return {"authenticated": True}

    @app.post("/api/logout")
    def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE, path="/")
        return {"authenticated": False}

    @app.get("/api/session")
    def session(request: Request) -> dict[str, bool]:
        try:
            authorized(request)
        except HTTPException:
            return {"authenticated": False}
        return {"authenticated": True}

    # agents ------------------------------------------------------------------------------

    # Routes that touch the orchestrator are `async`: they run on the event loop, not in
    # FastAPI's thread pool, so they can start tasks and publish on the bus safely.
    @app.get("/api/agents")
    async def agents(_: Auth) -> dict[str, Any]:
        if ctx.orchestrator is not None:
            return {
                "running": True,
                "agents": ctx.orchestrator.agent_states(),
                "slots": dict(ctx.orchestrator.slots),
                "fast_lane": ctx.orchestrator.fast_lane,
            }
        with Session(ctx.engine) as s:
            rows = s.scalars(select(AgentInstance).order_by(AgentInstance.name))
            listed = [
                {"name": r.name, "role": r.role, "state": r.state, "run_id": r.current_run_id}
                for r in rows
            ]
        return {
            "running": False,
            "agents": listed,
            "slots": dict(ctx.cfg.slots),
            "fast_lane": runtime_settings.load(ctx.cfg.data_dir).fast_lane,
        }

    @app.patch("/api/agents/slots")
    async def slots(_: Auth, changes: Annotated[dict[str, int], Body()]) -> dict[str, Any]:
        try:
            return {"slots": orchestrator().set_slots(changes)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # runtime settings ------------------------------------------------------------------

    @app.get("/api/settings")
    async def get_settings(_: Auth) -> dict[str, Any]:
        return runtime_settings.load(ctx.cfg.data_dir).model_dump(mode="json")

    @app.patch("/api/settings")
    async def patch_settings(_: Auth, body: SettingsPatch) -> dict[str, Any]:
        if body.fast_lane is not None:
            if ctx.orchestrator is not None:
                await ctx.orchestrator.set_fast_lane(body.fast_lane, by="dashboard")
            else:
                runtime_settings.save(ctx.cfg.data_dir, by="dashboard", fast_lane=body.fast_lane)
        return runtime_settings.load(ctx.cfg.data_dir).model_dump(mode="json")

    # tickets and runs --------------------------------------------------------------------

    @app.get("/api/tickets")
    def tickets(_: Auth, status: str | None = None) -> list[dict[str, Any]]:
        return tickets_cache.cached(ctx.engine, status)

    @app.get("/api/runs")
    def runs(
        _: Auth,
        role: str | None = None,
        ticket: str | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_RUNS)] = 50,
    ) -> list[dict[str, Any]]:
        q = select(Run).order_by(Run.started_at.desc()).limit(limit)
        if role:
            q = q.where(Run.role == role)
        if ticket:
            q = q.where(Run.ticket_key == ticket.upper())
        with Session(ctx.engine) as s:
            return [run_summary(r) for r in s.scalars(q)]

    def get_run(run_id: str) -> Run:
        with Session(ctx.engine) as s:
            run = s.get(Run, run_id)
            if run is None:
                raise HTTPException(404, f"no run {run_id}")
            s.expunge(run)
            return run

    def transcript_path(run: Run) -> Path:
        if run.transcript_path:
            return Path(run.transcript_path)
        return ctx.cfg.data_dir / "transcripts" / f"{run.id}.jsonl"

    @app.get("/api/runs/{run_id}")
    def run_detail(_: Auth, run_id: str) -> dict[str, Any]:
        run = get_run(run_id)
        with Session(ctx.engine) as s:
            events = [
                {"ts": e.ts.isoformat(), "type": e.type, "payload": e.payload_json}
                for e in s.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.ts))
            ]
        return {
            **run_summary(run),
            "prompt_hash": run.prompt_hash,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "result": run.result_json,
            "error": run.error,
            "has_transcript": transcript_path(run).exists(),
            "events": events,
        }

    @app.get("/api/runs/{run_id}/transcript")
    def transcript(
        _: Auth,
        run_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LINES)] = 200,
    ) -> dict[str, Any]:
        path = transcript_path(get_run(run_id))
        if not path.exists():
            return {"lines": [], "offset": offset, "next_offset": offset, "done": True}
        with path.open(encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        page = [_parse(line) for line in all_lines[offset : offset + limit]]
        next_offset = offset + len(page)
        return {
            "lines": page,
            "offset": offset,
            "next_offset": next_offset,
            "done": next_offset >= len(all_lines),
        }

    @app.get("/api/runs/{run_id}/transcript/stream")
    async def transcript_stream(
        _: Auth, run_id: str, request: Request, offset: Annotated[int, Query(ge=0)] = 0
    ) -> StreamingResponse:
        run = get_run(run_id)
        path = transcript_path(run)

        async def lines() -> AsyncIterator[str]:
            position, sent = 0, 0
            partial = ""
            idle = 0.0
            while not ctx.closing.is_set() and not await request.is_disconnected():
                if path.exists():
                    with path.open(encoding="utf-8", errors="replace") as f:
                        f.seek(position)
                        chunk = f.read()
                        position = f.tell()
                    partial += chunk
                    *complete, partial = partial.split("\n")
                    for line in complete:
                        if line.strip():
                            if sent >= offset:
                                yield sse("line", _parse(line))
                            sent += 1
                    if complete:
                        idle = 0.0
                if not complete_or_new(path, position) and _finished(ctx.engine, run_id):
                    yield sse("end", {"run_id": run_id, "lines": sent})
                    return
                await asyncio.sleep(ctx.tail_poll_s)
                idle += ctx.tail_poll_s
                if idle >= ctx.keepalive_s:
                    idle = 0.0
                    yield ": keepalive\n\n"

        return _sse_response(lines())

    @app.post("/api/runs/{role}", status_code=202)
    async def trigger(_: Auth, role: str, body: RunRequest) -> dict[str, Any]:
        if not body.ticket_key:
            raise HTTPException(400, "ticket_key is required")
        try:
            job = await orchestrator().request_run(role, body.ticket_key.strip().upper())
        except RunRefused as e:
            raise HTTPException(409, str(e)) from e
        return {"run_id": job.run_id, "instance": job.instance, "ticket_key": job.key}

    # budget, evals -----------------------------------------------------------------------

    @app.get("/api/budget")
    def budget(_: Auth) -> dict[str, Any]:
        return jsonable(ctx.budget.status())  # type: ignore[no-any-return]

    @app.get("/api/evals")
    def evals(
        _: Auth, limit: Annotated[int, Query(ge=1, le=MAX_RUNS)] = 50
    ) -> list[dict[str, Any]]:
        with Session(ctx.engine) as s:
            runs = s.scalars(select(EvalRun).order_by(EvalRun.started_at.desc()).limit(limit))
            return [eval_summary(r) for r in runs]

    @app.get("/api/evals/{eval_id}")
    def eval_detail(_: Auth, eval_id: str) -> dict[str, Any]:
        with Session(ctx.engine) as s:
            run = s.get(EvalRun, eval_id)
            if run is None:
                raise HTTPException(404, f"no eval run {eval_id}")
            results = s.scalars(
                select(EvalResult)
                .where(EvalResult.eval_run_id == eval_id)
                .order_by(EvalResult.task_id, EvalResult.repeat_idx)
            )
            return {**eval_summary(run), "results": [eval_result(r) for r in results]}

    # live stream -------------------------------------------------------------------------

    @app.get("/api/stream")
    async def stream(_: Auth, request: Request) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            async with ctx.bus.subscribe() as queue:
                yield sse("agent_state", await agents(None))
                yield sse("budget_update", jsonable(ctx.budget.status()))
                idle = 0.0
                while not ctx.closing.is_set() and not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), 1.0)
                    except TimeoutError:
                        idle += 1.0
                        if idle >= ctx.keepalive_s:
                            idle = 0.0
                            yield ": keepalive\n\n"
                        continue
                    idle = 0.0
                    yield sse(event.type, event.data)

        return _sse_response(events())

    # dashboard ---------------------------------------------------------------------------

    if ctx.static_dir is not None and (ctx.static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=ctx.static_dir, html=True), name="dashboard")
    else:

        @app.get("/", include_in_schema=False)
        def not_built() -> PlainTextResponse:
            return PlainTextResponse(
                "The API is up. Build the dashboard with `codeit dashboard build`.\n"
            )

    return app


def _parse(line: str) -> Any:
    try:
        return json.loads(line)
    except ValueError:
        return {"type": "raw", "text": line.rstrip("\n")}


def complete_or_new(path: Path, position: int) -> bool:
    """More bytes were written than we have read."""
    try:
        return path.stat().st_size > position
    except FileNotFoundError:
        return False


def _finished(engine: Engine, run_id: str) -> bool:
    with Session(engine) as s:
        run = s.get(Run, run_id)
        return run is None or run.status != "running"


def _sse_response(body: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
