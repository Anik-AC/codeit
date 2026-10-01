from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from codeit import db
from codeit.db.models import EvalResult, EvalRun
from codeit.evals.planner import load_planner_suite, planner_summary, run_planner_eval
from tests.unit.agents.conftest import FakeBackend, valid_plan


def _scores(refs: list[str], value: int) -> dict[str, object]:
    crit = ("independent", "negotiable", "valuable", "estimable", "small", "testable")
    return {
        "stories": [{"ref": r, **dict.fromkeys(crit, value), "note": ""} for r in refs],
        "coverage": 2,
        "missing": [],
    }


async def test_planner_eval_scores_valid_and_invalid_drafts(tmp_path: Path) -> None:
    db.upgrade(tmp_path / "e.db")
    engine = db.make_engine(tmp_path / "e.db")
    suite = load_planner_suite()
    assert [c.id for c in suite.cases] == [
        "P1-task-basics",
        "P2-tags-and-search",
        "P3-stats-endpoint",
    ]
    bad = {"epic": {"summary": "x"}, "stories": []}
    planner = FakeBackend("claude_code_chat", [valid_plan(), bad, bad])
    judge = FakeBackend("claude_code_chat", [_scores(["S1", "S2", "S3"], 2)])
    eval_id = await run_planner_eval(
        engine, suite, [planner], [judge], cases=suite.cases[:2], echo=lambda _: None
    )
    with Session(engine) as s:
        run = s.get(EvalRun, eval_id)
        results = s.query(EvalResult).filter_by(eval_run_id=eval_id).all()
    assert run is not None and run.suite == "planner"
    summary = run.summary_json or {}
    assert summary["schema_valid"] == 0.5 and summary["first_try_valid"] == 0.5
    assert summary["invest"] == 1.0 and summary["coverage"] == 1.0
    assert [(r.task_id, r.passed) for r in results] == [
        ("P1-task-basics", True),
        ("P2-tags-and-search", False),
    ]
    user = judge.requests[0].messages[-1].content
    assert "Unit tests:" in user and "<plan>" in user


def test_planner_summary_empty() -> None:
    assert planner_summary([])["schema_valid"] == 0.0


async def test_eval_steering_is_committed_as_the_new_base(tmp_path: Path) -> None:
    import subprocess
    from types import SimpleNamespace

    from codeit.evals.runner import _apply_steering
    from codeit.git import Git

    def sh(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    sh("init", "-q", "-b", "main")
    sh("config", "user.name", "t")
    sh("config", "user.email", "t@t")
    (tmp_path / "CLAUDE.md").write_text("old\n")
    sh("add", "-A")
    sh("commit", "-qm", "base")
    base = sh("rev-parse", "HEAD")
    ctx = SimpleNamespace(git=Git(), steering={"CLAUDE.md": "new rules\n"})
    head = await _apply_steering(ctx, tmp_path)  # type: ignore[arg-type]
    assert head != base and (tmp_path / "CLAUDE.md").read_text() == "new rules\n"
    assert sh("status", "--porcelain") == ""
    assert (
        await _apply_steering(ctx, tmp_path) == head
    )  # nothing new to commit  # type: ignore[arg-type]
