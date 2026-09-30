"""The orchestrator loop (PRD 12): `codeit up`.

Every `poll_seconds`:

1. reap dead runs (crash recovery, PRD 12.4)
2. for the reviewer, then the coder (finishing work beats starting it), while a slot is
   free and the budget allows (PRD 9.4): take the next ticket from Jira that is not
   running, not leased, not cooling down after an error and, for the coder, not blocked
   by an unfinished ticket (PRD 6.2 rule 5); start its run as a task
3. the merge watcher (PRD 11.4)
4. refresh the dashboard's ticket cache
5. write each agent instance's state for `codeit agents` (PRD 12.3)

Changes are published on the event bus for the dashboard's live stream: agent states as
soon as a job starts or ends, run starts and ends, changed tickets, and budget state.

Slots are re-read from config.yaml every loop, so a change applies without a restart.
Slot changes from the dashboard are kept in `data/slots.json` and win over the file.
On shutdown, running jobs get `SHUTDOWN_GRACE_S` to finish; the rest are cancelled and
their runs marked `interrupted`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from ulid import ULID

from codeit.config import Config, ConfigError, Role, Secrets, load_config
from codeit.db.models import AgentInstance
from codeit.events import record_event
from codeit.jira_client import JiraClient, JiraIds, JiraNotFound
from codeit.jira_client.issues import get_ticket
from codeit.jira_client.search import search_tickets
from codeit.log import get_logger
from codeit.orchestrator import tickets_cache
from codeit.orchestrator.budget import CLAUDE_ROLES, Budget
from codeit.orchestrator.bus import EventBus
from codeit.orchestrator.leases import LeaseStore
from codeit.orchestrator.merge_watcher import MergeWatcher
from codeit.orchestrator.reaper import Reaper, mark_runs
from codeit.sandbox.containers import Sandbox

log = get_logger(__name__)

ROLES: tuple[Role, ...] = ("reviewer", "coder")
JQL: Mapping[str, str] = {
    "reviewer": 'project = {p} AND status = "Agent Review" ORDER BY updated ASC',
    "coder": 'project = {p} AND status = "Ready for Dev" ORDER BY priority DESC, created ASC',
}
STATUS_FOR = {"coder": "Ready for Dev", "reviewer": "Agent Review"}
CANDIDATES_PER_TICK = 20
ERROR_COOLDOWN = timedelta(minutes=10)
SHUTDOWN_GRACE_S = 60.0
MAX_SLOTS = 10
SLOT_ROLES = ("coder", "reviewer", "rebase", "docs", "learning", "planner")

Echo = Callable[[str], None]


class RunRefused(Exception):
    """A requested run cannot start now; the message says why."""


class AgentRunner(Protocol):
    async def __call__(
        self,
        cfg: Config,
        secrets: Secrets,
        ids: JiraIds,
        key: str,
        *,
        instance: str,
        run_id: str,
        sandbox: Sandbox,
        echo: Echo,
    ) -> Any: ...


def default_runners() -> dict[str, AgentRunner]:
    from codeit.agents.coder_run import run_coder
    from codeit.agents.reviewer.run import run_reviewer

    return {"coder": run_coder, "reviewer": run_reviewer}


@dataclass
class Job:
    role: str
    key: str
    instance: str
    run_id: str
    started: datetime
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class Orchestrator:
    def __init__(
        self,
        cfg: Config,
        secrets: Secrets,
        ids: JiraIds,
        *,
        engine: Engine,
        jira: JiraClient,
        budget: Budget,
        merge_watcher: MergeWatcher,
        sandbox: Sandbox,
        runners: Mapping[str, AgentRunner] | None = None,
        config_path: Path | None = None,
        echo: Echo = print,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        bus: EventBus | None = None,
    ) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.ids = ids
        self.engine = engine
        self.jira = jira
        self.budget = budget
        self.merge_watcher = merge_watcher
        self.sandbox = sandbox
        self.runners = dict(runners) if runners is not None else default_runners()
        self.config_path = config_path
        self.echo = echo
        self.clock = clock
        self.bus = bus or EventBus()
        self.overrides_path = cfg.data_dir / "slots.json"
        self.slot_overrides = self._load_overrides()
        self.slots: dict[str, int] = {
            **{str(k): v for k, v in cfg.slots.items()},
            **self.slot_overrides,
        }
        self._published_states: list[dict[str, Any]] = []
        self._published_budget: dict[str, Any] | None = None
        self.jobs: dict[str, Job] = {}
        self.cooldown: dict[str, datetime] = {}
        self.waiting: dict[str, str] = {}  # role -> why it cannot start runs now
        self.leases = LeaseStore(engine)
        self.reaper = Reaper(
            engine,
            jira,
            ids,
            cfg.project.jira_project_key,
            remove_containers=sandbox.remove_run,
        )

    # the loop ----------------------------------------------------------------------------

    async def run_forever(self, stop: asyncio.Event) -> None:
        await self.startup()
        while not stop.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.cfg.orchestrator.poll_seconds)
        await self.shutdown()

    async def startup(self) -> None:
        for step in (self.reaper.recover_orphans, self.reaper.reap):
            try:
                for r in await step():
                    self.echo(f"{r.key}: recovered dead {r.role} run {r.run_id} -> {r.action}")
            except Exception as e:
                log.exception("orchestrator.recovery_failed")
                self.echo(f"recovery step failed: {e}")

    async def tick(self) -> None:
        self._reload_slots()
        await self._step("reaper", self._reap)
        for role in ROLES:
            await self._step(f"schedule {role}", functools.partial(self._schedule, role))
        await self._step("merge watcher", self._merge)
        await self._step("tickets", self._refresh_tickets)
        self._write_instances()
        self._publish_budget()

    async def _step(self, name: str, fn: Callable[[], Any]) -> None:
        """One part of a tick; its failure is logged and does not stop the others."""
        try:
            await fn()
        except Exception as e:
            log.exception("orchestrator.step_failed", step=name)
            self.echo(f"{name} failed: {e}")

    async def _reap(self) -> None:
        live = {j.run_id for j in self.jobs.values()}
        for r in await self.reaper.reap(live):
            self.echo(f"{r.key}: dead {r.role} run {r.run_id} -> {r.action}")

    async def _merge(self) -> None:
        for a in await self.merge_watcher.tick():
            self.echo(f"{a.key}: {a.action} ({a.detail})")

    async def _refresh_tickets(self) -> None:
        for ticket in await tickets_cache.refresh(
            self.engine, self.jira, self.ids, self.cfg.project.jira_project_key
        ):
            self.bus.publish("ticket_update", ticket)

    def _publish_budget(self) -> None:
        status = jsonable(self.budget.status())
        if status != self._published_budget:
            self._published_budget = status
            self.bus.publish("budget_update", status)

    # scheduling -----------------------------------------------------------------------------

    def _load_overrides(self) -> dict[str, int]:
        try:
            raw = json.loads(self.overrides_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {
            str(k): int(v)
            for k, v in raw.items()
            if k in SLOT_ROLES and isinstance(v, int) and 0 <= v <= MAX_SLOTS
        }

    def _reload_slots(self) -> None:
        base = {str(k): v for k, v in self.cfg.slots.items()}
        if self.config_path is not None:
            try:
                base = {str(k): v for k, v in load_config(self.config_path).slots.items()}
            except ConfigError as e:
                log.warning("orchestrator.config_reload_failed", error=str(e))
                return
        slots = {**base, **self.slot_overrides}
        if slots != self.slots:
            self.echo(f"slots changed: {slots}")
            self.slots = slots

    def set_slots(self, changes: Mapping[str, int]) -> dict[str, int]:
        """Slot changes from the dashboard (PATCH /api/agents/slots). They apply to the next
        claim, and are kept in `data/slots.json` across restarts."""
        for role, count in changes.items():
            if role not in SLOT_ROLES:
                raise ValueError(f"unknown role {role!r}")
            if not 0 <= count <= MAX_SLOTS:
                raise ValueError(f"{role}: slots must be 0 to {MAX_SLOTS}")
        self.slot_overrides.update(changes)
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        self.overrides_path.write_text(json.dumps(self.slot_overrides, indent=2), encoding="utf-8")
        self.slots.update(changes)
        self.echo(f"slots changed from the dashboard: {dict(changes)}")
        self._write_instances()
        return dict(self.slots)

    async def request_run(self, role: str, key: str) -> Job:
        """Start `role` on ticket `key` now (POST /api/runs/{role}), within the same limits
        as the loop: the ticket in the role's status, a free slot, the budget, and no other
        run on the ticket."""
        if role not in self.runners:
            raise RunRefused(f"no {role} agent in this version")
        expected = STATUS_FOR.get(role)
        if expected is not None:
            try:
                status = (await get_ticket(self.jira, key, self.ids.fields)).status
            except JiraNotFound as e:
                raise RunRefused(f"{key} does not exist") from e
            if status != expected:
                raise RunRefused(f"{key} is in {status}; the {role} takes tickets in {expected}")
        if key in self.jobs:
            raise RunRefused(f"{key} is already being worked on by {self.jobs[key].instance}")
        if self.leases.holder(key) is not None:
            raise RunRefused(f"{key} is leased by another run")
        if len(self.busy(role)) >= self.slots.get(role, 0):
            raise RunRefused(f"no free {role} slot")
        decision = self.budget.can_run(role, claude_running=self._claude_running())  # type: ignore[arg-type]
        if not decision.ok:
            raise RunRefused(decision.reason)
        return self._spawn(role, key)

    def busy(self, role: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.role == role]

    def _claude_running(self) -> int:
        """Claude runs going anywhere: this process's jobs, plus live leases held by other
        processes (a manual `codeit run`, or a crashed run whose container still works)."""
        keys = {j.key for j in self.jobs.values() if j.role in CLAUDE_ROLES}
        now = self.clock()
        for lease in self.leases.all():
            expires = lease.expires_at
            expires = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
            if lease.role in CLAUDE_ROLES and expires > now:
                keys.add(lease.ticket_key)
        return len(keys)

    async def _schedule(self, role: Role) -> None:
        free = self.slots.get(role, 0) - len(self.busy(role))
        if free <= 0 or role not in self.runners:
            return
        decision = self.budget.can_run(role, claude_running=self._claude_running())
        if not decision.ok:
            if self.waiting.get(role) != decision.reason:
                self.echo(f"{role}: waiting ({decision.reason})")
            self.waiting[role] = decision.reason
            return
        self.waiting.pop(role, None)
        now = self.clock()
        jql = JQL[role].format(p=self.cfg.project.jira_project_key)
        async for ticket in search_tickets(
            self.jira, jql, self.ids.fields, max_results=CANDIDATES_PER_TICK
        ):
            if free <= 0:
                break
            key = ticket.key
            if key in self.jobs or self.cooldown.get(key, now) > now:
                continue
            if self.leases.holder(key) is not None:
                continue  # a manual `codeit run`, or another process
            if role == "coder" and not await self._unblocked(ticket.blocked_by):
                continue
            if not self.budget.can_run(role, claude_running=self._claude_running()).ok:
                break
            self._spawn(role, key)
            free -= 1

    async def _unblocked(self, blockers: list[str]) -> bool:
        for b in blockers:
            if (await get_ticket(self.jira, b, self.ids.fields)).status != "Done":
                return False
        return True

    def _spawn(self, role: str, key: str) -> Job:
        used = {j.instance for j in self.busy(role)}
        instance = next(f"{role}-{i}" for i in range(1, 100) if f"{role}-{i}" not in used)
        job = Job(role, key, instance, str(ULID()), self.clock())
        self.jobs[key] = job
        job.task = asyncio.create_task(self._run(job), name=f"{instance}:{key}")
        self.echo(f"{key}: {instance} starting run {job.run_id}")
        self.bus.publish("run_event", {**job_dict(job), "event": "started"})
        self._write_instances()
        return job

    async def _run(self, job: Job) -> None:
        runner = self.runners[job.role]
        outcome = "finished"
        try:
            await runner(
                self.cfg,
                self.secrets,
                self.ids,
                job.key,
                instance=job.instance,
                run_id=job.run_id,
                sandbox=self.sandbox,
                echo=lambda line: self.echo(f"[{job.instance}] {line}"),
            )
        except Exception as e:
            outcome = "failed"
            self.cooldown[job.key] = self.clock() + ERROR_COOLDOWN
            log.warning("orchestrator.job_failed", key=job.key, role=job.role, error=str(e))
            self.echo(f"{job.key}: {job.instance} failed: {e} (retry after cooldown)")
            with contextlib.suppress(Exception):
                record_event(
                    self.engine,
                    "error",
                    {"key": job.key, "role": job.role, "error": str(e)[:1000]},
                    job.run_id,
                )
        finally:
            self.jobs.pop(job.key, None)
            self.bus.publish("run_event", {**job_dict(job), "event": outcome})
            with contextlib.suppress(Exception):
                self._write_instances()

    # shutdown and instance state ---------------------------------------------------------

    async def shutdown(self, grace_s: float = SHUTDOWN_GRACE_S) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task is not None]
        if tasks:
            self.echo(f"waiting up to {grace_s:.0f}s for {len(tasks)} running job(s)")
            _, pending = await asyncio.wait(tasks, timeout=grace_s)
            stopped = [j.run_id for j in self.jobs.values() if j.task in pending]
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            mark_runs(self.engine, stopped, "interrupted", "orchestrator shut down")
            if stopped:
                self.echo(f"interrupted {len(stopped)} run(s); they resume on the next start")
        self._write_instances(stopping=True)

    def instance_states(self, stopping: bool = False) -> list[AgentInstance]:
        now = self.clock()
        rows = []
        for role in ROLES:
            jobs = {j.instance: j for j in self.busy(role)}
            highest = max((int(n.rsplit("-", 1)[1]) for n in jobs), default=0)
            count = max(self.slots.get(role, 0), highest)
            for i in range(1, max(count, 1) + 1):
                name = f"{role}-{i}"
                job = jobs.get(name)
                if job is not None and not stopping:
                    state = "busy"
                elif i > self.slots.get(role, 0):
                    state = "disabled"
                elif role in self.waiting and not stopping:
                    state = "parked"
                else:
                    state = "idle"
                rows.append(
                    AgentInstance(
                        name=name,
                        role=role,
                        state=state,
                        current_run_id=job.run_id if job and not stopping else None,
                        updated_at=now,
                    )
                )
        return rows

    def agent_states(self) -> list[dict[str, Any]]:
        """Instances as the dashboard shows them (GET /api/agents)."""
        jobs = {j.run_id: j for j in self.jobs.values()}
        out = []
        for r in self.instance_states():
            job = jobs.get(r.current_run_id or "")
            out.append(
                {
                    "name": r.name,
                    "role": r.role,
                    "state": r.state,
                    "run_id": r.current_run_id,
                    "ticket_key": job.key if job else None,
                    "since": job.started.isoformat() if job else None,
                    "reason": self.waiting.get(r.role) if r.state == "parked" else None,
                }
            )
        return out

    def _write_instances(self, stopping: bool = False) -> None:
        rows = self.instance_states(stopping)
        states = self.agent_states() if not stopping else []
        if states != self._published_states:
            self._published_states = states
            self.bus.publish("agent_state", {"agents": states, "slots": dict(self.slots)})
        with Session(self.engine) as s, s.begin():
            for r in rows:
                values = {
                    "name": r.name,
                    "role": r.role,
                    "state": r.state,
                    "current_run_id": r.current_run_id,
                    "updated_at": r.updated_at,
                }
                stmt = insert(AgentInstance).values(**values)
                s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[AgentInstance.name],
                        set_={k: v for k, v in values.items() if k != "name"},
                    )
                )


def job_dict(job: Job) -> dict[str, Any]:
    return {
        "run_id": job.run_id,
        "role": job.role,
        "ticket_key": job.key,
        "instance": job.instance,
        "started": job.started.isoformat(),
    }


def jsonable(value: Any) -> Any:
    """Datetimes to ISO strings, tuples to lists, for JSON payloads."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
