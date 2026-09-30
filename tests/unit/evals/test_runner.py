from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from codeit.agents.reviewer.checks import CheckResult, Phase1
from codeit.agents.reviewer.verdict import Verdict
from codeit.backends.base import AgenticResult
from codeit.config import Config, Secrets, load_config
from codeit.evals import runner
from codeit.evals.hidden import HiddenResult
from codeit.evals.report import compare, latest_by_config, run_report
from codeit.evals.runner import (
    EvalContext,
    EvalStopped,
    results_of,
    run_coder_eval,
    run_review_eval,
    verify,
)
from codeit.evals.suite import EvalConfig, load_suite
from codeit.orchestrator.leases import LeaseStore
from codeit.sandbox.clone import Prepared
from tests.conftest import REPO_ROOT

SECRETS = Secrets(_env_file=None, openrouter_api_key=SecretStr("k"))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return load_config(REPO_ROOT / "config" / "config.yaml").model_copy(
        update={"data_dir": tmp_path / "data"}
    )


class FakeWorkspaces:
    def __init__(self, cfg: Config, *a: Any) -> None:
        self.root = cfg.data_dir / "evals" / "work"
        self.applied: list[str] = []
        self.removed: list[Path] = []

    async def prepare(self, name: str, base: str, branch: str) -> Prepared:
        path = self.root / name
        (path / ".claude").mkdir(parents=True, exist_ok=True)
        (path / "CLAUDE.md").write_text("steer")
        return Prepared(path, branch, created=True, head=base)

    async def apply(self, path: Path, patch: Path, message: str) -> str:
        self.applied.append(patch.name if patch.parent.name == "mutants" else "reference")
        return "HEAD"

    async def diff_lines(self, path: Path, base: str) -> int:
        return 42

    def remove(self, path: Path) -> None:
        self.removed.append(path)


class FakeGit:
    async def run(self, *args: str, **kw: Any) -> str:
        return ""


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"coder": [], "scores": [], "reviews": []}

    @contextlib.asynccontextmanager
    async def container(ctx: Any, run_id: str, workspace: Path) -> AsyncIterator[Any]:
        yield None

    async def run_coder_in(
        cfg: Config, secrets: Secrets, prepared: Prepared, key: str, ticket_md: str, **kw: Any
    ) -> Any:
        state["coder"].append((key, kw["instance"]))
        agent = AgenticResult("completed", "RESULT: {}", "t.jsonl", turns=7, cost_usd=0.25)
        status = state.get("agent_status")
        if status:
            agent = AgenticResult(status, "limit", "t.jsonl")
        outcome = type("O", (), {"status": "committed"})()
        return type("R", (), {"agent": agent, "outcome": outcome})()

    async def score(suite: Any, task: Any, path: Path, run: Any, **kw: Any) -> HiddenResult:
        state["scores"].append(task.id)
        passed = state.get("pass", {}).get(task.id, True)
        return HiddenResult(4 if passed else 2, 4, [] if passed else ["x"], [])

    async def workspace_diff(git: Any, path: Path, base: str) -> tuple[list[str], str]:
        return ["src/a.ts"], "diff"

    async def run_phase1(*a: Any, **kw: Any) -> Phase1:
        return Phase1(checks=[CheckResult("unit", "pass")])

    async def judge(backends: Any, engine: Any, *, key: str, **kw: Any) -> tuple[Verdict, None]:
        verdict = state.get("verdicts", {}).get(key, "pass")
        state["reviews"].append(key)
        return Verdict(verdict=verdict, summary_md="s"), None

    for name, fn in {
        "container": container,
        "run_coder_in": run_coder_in,
        "score": score,
        "workspace_diff": workspace_diff,
        "run_phase1": run_phase1,
        "judge": judge,
        "Workspaces": FakeWorkspaces,
        "Git": lambda *a: FakeGit(),
    }.items():
        monkeypatch.setattr(runner, name, fn)
    return state


def context(cfg: Config) -> EvalContext:
    return EvalContext(cfg, SECRETS, load_suite("golden"), sandbox=object(), echo=lambda _: None)  # type: ignore[arg-type]


async def test_coder_eval_scores_and_stores(cfg: Config, fakes: dict[str, Any]) -> None:
    ctx = context(cfg)
    fakes["pass"] = {"T002": False}
    tasks = ctx.suite.select(["T001", "T002"])
    eval_id = await run_coder_eval(
        ctx, EvalConfig(name="default"), tasks, repeats=2, any_time=True, review=True
    )
    assert fakes["coder"] == [("T001", "eval")] * 2 + [("T002", "eval")] * 2
    results = results_of(ctx.engine, eval_id)
    assert [(r.task_id, r.repeat_idx, r.passed) for r in results] == [
        ("T001", 0, True),
        ("T001", 1, True),
        ("T002", 0, False),
        ("T002", 1, False),
    ]
    assert (
        results[0].diff_lines == 42
        and results[0].turns == 7
        and results[0].reviewer_verdict == "pass"
    )
    run = runner.latest_runs(ctx.engine)[0]
    assert run.summary_json is not None
    assert (run.summary_json["pass@1"], run.summary_json["pass^2"]) == (0.5, 0.5)
    assert run.steering_sha and len(run.steering_sha) == 12
    assert LeaseStore(ctx.engine).all() == []  # the eval's Claude lease is released
    assert len(ctx.workspaces.removed) == 4  # type: ignore[attr-defined]
    text = run_report(run, results)
    assert "pass^2" in text and "| T002 | 2      | NO" in text


async def test_coder_eval_respects_the_window(cfg: Config, fakes: dict[str, Any]) -> None:
    ctx = context(cfg)
    night = cfg.model_copy(
        update={"claude": cfg.claude.model_copy(update={"run_window": ["00:00-00:01"]})}
    )
    ctx.cfg = night
    with pytest.raises(EvalStopped, match="run window"):
        await run_coder_eval(ctx, EvalConfig(name="default"), ctx.suite.select(["T001"]), repeats=1)
    assert fakes["coder"] == []


async def test_coder_eval_stops_on_usage_limit(cfg: Config, fakes: dict[str, Any]) -> None:
    ctx = context(cfg)
    fakes["agent_status"] = "usage_limited"
    eval_id = await run_coder_eval(
        ctx,
        EvalConfig(name="default"),
        ctx.suite.select(["T001", "T002"]),
        repeats=1,
        any_time=True,
    )
    run = runner.latest_runs(ctx.engine)[0]
    assert run.id == eval_id and run.summary_json is not None
    assert "usage limit" in run.summary_json["stopped"] and len(fakes["coder"]) == 1


async def test_coder_eval_waits_for_another_claude_run(
    cfg: Config, fakes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = context(cfg)
    leases = LeaseStore(ctx.engine)
    leases.acquire("CODEIT-1", "coder", "coder-1", "R", timedelta(minutes=5))

    async def released(_: float) -> None:
        leases.release("CODEIT-1", "R")

    monkeypatch.setattr(runner.asyncio, "sleep", released)
    await run_coder_eval(
        ctx, EvalConfig(name="default"), ctx.suite.select(["T001"]), repeats=1, any_time=True
    )
    assert fakes["coder"] == [("T001", "eval")]


async def test_review_eval_counts_catches(cfg: Config, fakes: dict[str, Any]) -> None:
    ctx = context(cfg)
    fakes["verdicts"] = {"T001": "fail_critical"}  # judged by task id: every T001 variant fails
    eval_id = await run_review_eval(ctx, ctx.suite.select(["T001", "T002"]))
    results = {r.task_id: r for r in results_of(ctx.engine, eval_id)}
    assert set(results) == {"T001/clean", "T001/M1", "T001/M2", "T002/clean", "T002/M1", "T002/M2"}
    assert not results["T001/clean"].passed  # a false fail
    assert results["T001/M1"].passed and not results["T002/M1"].passed
    run = runner.latest_runs(ctx.engine)[0]
    assert run.suite == "golden-review" and run.model == "deepseek/deepseek-v4.1-flash"
    assert run.summary_json is not None
    assert (run.summary_json["critical_catch_rate"], run.summary_json["false_fail_rate"]) == (
        0.5,
        0.5,
    )
    assert "T002/M1" in run_report(run, list(results.values()))


async def test_verify(cfg: Config, fakes: dict[str, Any]) -> None:
    ctx = context(cfg)
    calls = iter([HiddenResult(1, 4), HiddenResult(4, 4), HiddenResult(4, 4), HiddenResult(4, 4)])

    async def score(*a: Any, **kw: Any) -> HiddenResult:
        return next(calls)

    runner.score = score  # type: ignore[assignment]
    results = await verify(ctx, ctx.suite.select(["T001", "T002"]))
    assert [r.ok for r in results] == [True, False]  # T002's base already passes everything


def test_compare_saves_a_table(cfg: Config, tmp_path: Path) -> None:
    from codeit import db
    from codeit.evals.runner import _finish_run, _start_run

    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    for run_id, config, rate in (("A1", "default", 0.5), ("B1", "opus", 0.75)):
        _start_run(engine, run_id, "golden", config, "abc123def456", None)
        _finish_run(engine, run_id, {"pass@1": rate, "pass^3": rate, "repeats": 3})
    runs = latest_by_config(engine, ["default", "opus"])
    text, path = compare(runs, tmp_path / "reports")
    assert "| pass@1 " in text and "0.500" in text and "0.750" in text
    assert path.exists() and "default-vs-opus" in path.name
    with pytest.raises(ValueError, match="no finished eval run"):
        latest_by_config(engine, ["missing"])
