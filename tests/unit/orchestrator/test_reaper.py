from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from codeit.db.models import Run
from codeit.jira_client import JiraClient
from codeit.orchestrator.leases import LeaseStore
from codeit.orchestrator.reaper import Reaper
from codeit.run_tokens import RunTokenStore
from codeit.runs import record_run
from tests.unit.orchestrator.fake_jira import IDS, FakeJira

TTL = timedelta(minutes=90)


def reaper(engine: Engine, jira: JiraClient) -> tuple[Reaper, MagicMock]:
    remove = MagicMock()
    return Reaper(engine, jira, IDS, "CODEIT", remove_containers=remove), remove


def dead_lease(engine: Engine, key: str, role: str, run_id: str) -> None:
    """A lease whose holder stopped heartbeating 10 minutes ago."""
    past = datetime.now(UTC) - timedelta(minutes=10)
    LeaseStore(engine, clock=lambda: past).acquire(key, role, f"{role}-1", run_id, TTL)
    record_run(engine, run_id, role, instance=f"{role}-1", ticket_key=key)


def status(engine: Engine, run_id: str) -> str:
    with Session(engine) as s:
        run = s.get(Run, run_id)
        assert run is not None
        return run.status


async def test_dead_coder_run_retries_then_escalates(
    engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-1", "In Dev")
    token = RunTokenStore(engine).mint("coder", "R1", "CODEIT-1", TTL)
    dead_lease(engine, "CODEIT-1", "coder", "R1")
    r, remove = reaper(engine, jira)

    [first] = await r.reap()
    assert (first.key, first.action) == ("CODEIT-1", "ready_for_dev")
    assert status(engine, "R1") == "abandoned"
    remove.assert_called_once_with("R1")
    assert LeaseStore(engine).all() == []
    assert RunTokenStore(engine).verify(token) is None
    assert fake_jira.issues["CODEIT-1"].status == "Ready for Dev"

    fake_jira.issues["CODEIT-1"].status = "In Dev"  # claimed again, and died again
    dead_lease(engine, "CODEIT-1", "coder", "R2")
    [second] = await r.reap()
    assert second.action == "human_review"
    issue = fake_jira.issues["CODEIT-1"]
    assert (issue.status, issue.labels) == ("Human Review", ["needs-human"])
    assert "2 abandoned coder runs" in issue.comments[-1]


async def test_dead_review_is_retried_in_place(
    engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-2", "Agent Review")
    dead_lease(engine, "CODEIT-2", "reviewer", "R1")
    [done] = await reaper(engine, jira)[0].reap()
    assert done.action == "retry_review" and fake_jira.moves == []
    assert LeaseStore(engine).holder("CODEIT-2") is None


async def test_live_and_moved_tickets_are_left_alone(
    engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-3", "In Dev")
    dead_lease(engine, "CODEIT-3", "coder", "R1")
    r, _ = reaper(engine, jira)
    assert await r.reap(live_runs={"R1"}) == []  # this process runs it
    fake_jira.issues["CODEIT-3"].status = "Human Review"  # a human moved it
    [done] = await r.reap()
    assert done.action == "none" and fake_jira.moves == []


async def test_orphans_after_a_stop(engine: Engine, jira: JiraClient, fake_jira: FakeJira) -> None:
    fake_jira.add("CODEIT-4", "In Dev")  # killed: last run still `running`
    fake_jira.add("CODEIT-5", "In Dev")  # clean shutdown: `interrupted`
    fake_jira.add("CODEIT-6", "In Dev")  # moved to In Dev by a human; no unfinished run
    record_run(engine, "R4", "coder", instance="coder-1", ticket_key="CODEIT-4")
    record_run(engine, "R5", "coder", instance="coder-1", ticket_key="CODEIT-5")
    record_run(engine, "R5", "coder", status="interrupted")
    done = await reaper(engine, jira)[0].recover_orphans()
    assert {(d.key, d.action) for d in done} == {
        ("CODEIT-4", "ready_for_dev"),
        ("CODEIT-5", "ready_for_dev"),
    }
    assert (status(engine, "R4"), status(engine, "R5")) == ("abandoned", "interrupted")
    assert fake_jira.issues["CODEIT-6"].status == "In Dev"
