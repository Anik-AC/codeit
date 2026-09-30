from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from codeit.config import Config, Secrets
from codeit.db.models import AgentInstance, Run
from codeit.jira_client import JiraClient
from codeit.orchestrator.budget import Budget
from codeit.orchestrator.leases import LeaseStore
from codeit.orchestrator.scheduler import Orchestrator
from codeit.runs import record_run
from tests.conftest import REPO_ROOT
from tests.unit.orchestrator.fake_jira import IDS, FakeJira


class FakeRunner:
    """Records calls; moves the ticket like the real agent would, or fails, or hangs."""

    def __init__(
        self, jira: FakeJira, to: str | None, *, fail: bool = False, hang: bool = False
    ) -> None:
        self.jira = jira
        self.to = to
        self.fail = fail
        self.hang = hang
        self.calls: list[tuple[str, str]] = []
        self.engine: Engine | None = None  # set by `orchestrator()`

    async def __call__(self, cfg: Config, secrets: Secrets, ids: Any, key: str, **kw: Any) -> None:
        self.calls.append((key, kw["instance"]))
        assert self.engine is not None
        record_run(self.engine, kw["run_id"], "x", ticket_key=key)
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise RuntimeError("docker is down")
        if self.to:
            self.jira.issues[key].status = self.to


def orchestrator(
    cfg: Config,
    engine: Engine,
    jira: JiraClient,
    runners: dict[str, FakeRunner],
    *,
    any_time: bool = True,
    config_path: Path | None = None,
    key: bool = True,
) -> Orchestrator:
    for r in runners.values():
        r.engine = engine
    watcher = MagicMock()
    watcher.tick = AsyncMock(return_value=[])
    return Orchestrator(
        cfg,
        Secrets(_env_file=None),
        IDS,
        engine=engine,
        jira=jira,
        budget=Budget(cfg, engine, has_openrouter_key=key, any_time=any_time),
        merge_watcher=watcher,
        sandbox=MagicMock(),
        runners=runners,  # type: ignore[arg-type]
        config_path=config_path,
        echo=lambda _: None,
    )


async def tick(o: Orchestrator) -> None:
    await o.tick()
    await asyncio.sleep(0)  # let the jobs it started begin


async def settle(o: Orchestrator) -> None:
    tasks = [j.task for j in o.jobs.values() if j.task]
    if tasks:
        await asyncio.wait(tasks, timeout=5)


def states(engine: Engine) -> dict[str, str]:
    with Session(engine) as s:
        return {i.name: i.state for i in s.scalars(select(AgentInstance))}


async def test_reviewer_then_coder_within_slots(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-1", "Ready for Dev")
    fake_jira.add("CODEIT-2", "Ready for Dev")
    fake_jira.add("CODEIT-3", "Agent Review")
    fake_jira.add("CODEIT-4", "Agent Review")
    fake_jira.add("CODEIT-5", "Agent Review")
    coder = FakeRunner(fake_jira, "Agent Review")
    reviewer = FakeRunner(fake_jira, "Human Review")
    o = orchestrator(cfg, engine, jira, {"coder": coder, "reviewer": reviewer})

    await tick(o)
    # 2 reviewer slots, 1 coder slot; reviewers are scheduled first
    assert reviewer.calls == [("CODEIT-3", "reviewer-1"), ("CODEIT-4", "reviewer-2")]
    assert coder.calls == [("CODEIT-1", "coder-1")]
    assert 'status = "Agent Review"' in fake_jira.jql[0]
    assert states(engine) == {"reviewer-1": "busy", "reviewer-2": "busy", "coder-1": "busy"}
    await settle(o)

    await tick(o)  # CODEIT-1 is in Agent Review now
    assert {c[0] for c in reviewer.calls[2:]} == {"CODEIT-5", "CODEIT-1"}
    assert coder.calls[1:] == [("CODEIT-2", "coder-1")]
    await settle(o)
    o._write_instances()
    assert set(states(engine).values()) == {"idle"}


async def test_blocked_leased_and_cooling_tickets_are_skipped(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-1", "In Dev")
    fake_jira.add("CODEIT-2", "Ready for Dev", blocked_by=["CODEIT-1"])
    fake_jira.add("CODEIT-3", "Ready for Dev")  # leased by a manual run
    fake_jira.add("CODEIT-4", "Ready for Dev")  # its runs fail
    LeaseStore(engine).acquire("CODEIT-3", "coder", "coder-9", "M1", timedelta(minutes=5))
    coder = FakeRunner(fake_jira, None, fail=True)
    two = cfg.model_copy(
        update={"claude": cfg.claude.model_copy(update={"max_concurrent_runs": 2})}
    )  # the manual run takes one Claude slot
    o = orchestrator(two, engine, jira, {"coder": coder})

    await tick(o)
    await settle(o)
    assert coder.calls == [("CODEIT-4", "coder-1")]
    assert o.cooldown["CODEIT-4"] > datetime.now(UTC)

    await tick(o)  # cooling down: not retried yet
    assert len(coder.calls) == 1

    fake_jira.issues["CODEIT-1"].status = "Done"  # the blocker is done
    o.cooldown.clear()
    await tick(o)
    await settle(o)
    assert [c[0] for c in coder.calls[1:]] == ["CODEIT-2"]


async def test_budget_refusal_parks_the_role(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-1", "Agent Review")
    reviewer = FakeRunner(fake_jira, "Human Review")
    o = orchestrator(cfg, engine, jira, {"reviewer": reviewer}, key=False)
    await tick(o)
    assert reviewer.calls == [] and "OpenRouter key" in o.waiting["reviewer"]
    assert states(engine)["reviewer-1"] == "parked"


async def test_run_window_holds_the_coder(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-1", "Ready for Dev")
    coder = FakeRunner(fake_jira, "Agent Review")
    night_only = cfg.model_copy(
        update={"claude": cfg.claude.model_copy(update={"run_window": ["00:00-00:01"]})}
    )
    o = orchestrator(night_only, engine, jira, {"coder": coder}, any_time=False)
    await tick(o)
    assert coder.calls == [] and "run window" in o.waiting["coder"]


async def test_slots_reload_without_restart(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira, tmp_path: Path
) -> None:
    raw = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text())
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    fake_jira.add("CODEIT-1", "Agent Review")
    reviewer = FakeRunner(fake_jira, "Human Review")
    o = orchestrator(cfg, engine, jira, {"reviewer": reviewer}, config_path=path)

    raw["slots"]["reviewer"] = 0
    path.write_text(yaml.safe_dump(raw))
    await tick(o)
    assert reviewer.calls == [] and o.slots["reviewer"] == 0
    assert states(engine) == {"reviewer-1": "disabled", "coder-1": "idle"}

    raw["slots"]["reviewer"] = 1
    path.write_text(yaml.safe_dump(raw))
    await tick(o)
    assert reviewer.calls == [("CODEIT-1", "reviewer-1")]
    await settle(o)


async def test_shutdown_interrupts_what_does_not_finish(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-1", "Ready for Dev")
    coder = FakeRunner(fake_jira, None, hang=True)
    o = orchestrator(cfg, engine, jira, {"coder": coder})
    await tick(o)
    run_id = o.jobs["CODEIT-1"].run_id
    await o.shutdown(grace_s=0.05)
    assert o.jobs == {}
    with Session(engine) as s:
        run = s.get(Run, run_id)
        assert run is not None and run.status == "interrupted"


async def test_one_failing_step_does_not_stop_the_tick(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-1", "Agent Review")
    reviewer = FakeRunner(fake_jira, "Human Review")
    o = orchestrator(cfg, engine, jira, {"reviewer": reviewer})
    o.reaper.reap = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    await tick(o)
    assert reviewer.calls == [("CODEIT-1", "reviewer-1")]
    o.merge_watcher.tick.assert_awaited()  # type: ignore[attr-defined]
    await settle(o)


@pytest.mark.parametrize("leftover", ["running", "interrupted"])
async def test_startup_recovers_orphans(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira, leftover: str
) -> None:
    fake_jira.add("CODEIT-1", "In Dev")
    record_run(engine, "OLD", "coder", instance="coder-1", ticket_key="CODEIT-1", status=leftover)
    o = orchestrator(cfg, engine, jira, {})
    await o.startup()
    assert fake_jira.issues["CODEIT-1"].status == "Ready for Dev"


async def test_claude_runs_elsewhere_count_toward_the_limit(
    cfg: Config, engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-1", "In Dev")  # a crashed run's container is still working on it
    fake_jira.add("CODEIT-2", "Ready for Dev")
    LeaseStore(engine).acquire("CODEIT-1", "coder", "coder-1", "OLD", timedelta(minutes=90))
    coder = FakeRunner(fake_jira, "Agent Review")
    o = orchestrator(cfg, engine, jira, {"coder": coder})
    await tick(o)
    assert coder.calls == [] and "concurrency" in o.waiting["coder"]
