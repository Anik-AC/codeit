"""The Learning agent: signals, lessons, steering edits, the eval gate, and a whole run with
real git against local "GitHub" repos, fake Jira and GitHub over respx, and a scripted
model."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from codeit import db
from codeit.agents.learning import gate as gates
from codeit.agents.learning import run as learning_run
from codeit.agents.learning import signals as sig
from codeit.agents.learning.lessons import classify, open_lessons, qualifying
from codeit.agents.learning.run import pr_body, report_gate, run_learning, with_gate
from codeit.agents.learning.steering import SECTION, add_rules, clean_rule
from codeit.backends.base import BackendError
from codeit.config import Config, Secrets, load_config
from codeit.db.models import EvalResult, EvalRun, LearningProposal, Signal
from codeit.jira_client import JiraClient
from codeit.runs import finish_run, record_run
from codeit.sandbox.clone import CloneManager
from tests.conftest import REPO_ROOT
from tests.unit.agents.conftest import FakeBackend
from tests.unit.jira.conftest import API, BASE, FIELDS, issue_json
from tests.unit.orchestrator.fake_jira import IDS

SLUG = "Anik-AC/codeit-sandbox-app"
FIXTURE = REPO_ROOT / "evals" / "suites" / "learning" / "signals.yaml"
SECRETS = Secrets(
    _env_file=None,
    jira_base_url=BASE,
    jira_email="e",
    jira_api_token=SecretStr("j"),
    github_token_agent=SecretStr("gh"),
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return load_config(REPO_ROOT / "config" / "config.yaml").model_copy(
        update={"data_dir": tmp_path / "data"}
    )


@pytest.fixture
def engine(cfg: Config) -> Engine:
    db.upgrade(cfg.db_path)
    return db.make_engine(cfg.db_path)


def classified(ids: list[int], answers: dict[int, tuple[str, str, bool]]) -> dict[str, Any]:
    """A classify answer: id -> (target, lesson_key, actionable)."""
    return {
        "signals": [
            {
                "id": i,
                "actionable": answers[i][2],
                "target": answers[i][0],
                "theme": "testing",
                "lesson_key": answers[i][1],
                "lesson": f"Lesson {answers[i][1]}.",
            }
            for i in ids
            if i in answers
        ]
    }


# steering files -----------------------------------------------------------------------------


def test_rules_go_under_one_managed_heading() -> None:
    text, added = add_rules("# Repo\n\n- Use npm.\n", ["Test the 400 case.", "- Use npm."])
    assert added == ["Test the 400 case."]  # already there, in other words
    assert text.startswith("# Repo\n\n- Use npm.\n\n" + SECTION)
    assert text.endswith("- Test the 400 case.\n")
    more = text + "\n## Other\n\nKeep me.\n"
    again, added = add_rules(more, ["Name the empty state."])
    assert added == ["Name the empty state."]
    assert again.count(SECTION) == 1
    assert "- Test the 400 case.\n- Name the empty state.\n\n## Other\n\nKeep me.\n" in again


def test_rules_that_cannot_go_into_a_prompt_are_dropped() -> None:
    assert clean_rule("  - Use   {{ secret }} here") is None
    assert clean_rule("{% if x %}") is None
    assert clean_rule("# A heading") is None
    assert clean_rule("* Keep\nit   on one line") == "Keep it on one line"
    assert add_rules("x\n", ["{{ bad }}"]) == ("x\n", [])


# signals and lessons ------------------------------------------------------------------------


def test_fixture_store_is_deduplicated(engine: Engine) -> None:
    raws = sig.load_fixture(FIXTURE)
    assert len(raws) == 12 and raws[0].external_id == "fixture:signals:1"
    assert sig.store(engine, raws) == 12
    assert sig.store(engine, raws) == 0
    assert sig.count_new_human(engine) == 12


async def test_classify_then_qualify(engine: Engine) -> None:
    sig.store(engine, sig.load_fixture(FIXTURE)[:5])
    answer = classified(
        [1, 2, 3, 4, 5],
        {
            1: ("coder", "api-error-tests", True),
            2: ("coder", "API error tests", True),  # slugified to the same key
            3: ("planner", "empty-state", True),
            4: ("reviewer", "noise", False),
            # 5 left out: stays new
        },
    )
    backend = FakeBackend("claude_code_chat", [answer])
    count, calls = await classify(engine, [backend])
    assert count == 5 and calls[0].backend == "claude_code_chat"
    with Session(engine) as s:
        status = {r.id: r.status for r in s.scalars(select(Signal))}
    assert status == {1: "classified", 2: "classified", 3: "classified", 4: "ignored", 5: "new"}
    lessons = open_lessons(engine)
    assert set(lessons) == {"coder:api-error-tests", "planner:empty-state"}
    assert [lesson.key for lesson in qualifying(lessons)] == ["api-error-tests"]
    with Session(engine) as s, s.begin():
        row = s.get(Signal, 3)
        assert row is not None
        row.learn = True  # a human wrote #learn
    assert {lesson.key for lesson in qualifying(open_lessons(engine))} == {
        "api-error-tests",
        "empty-state",
    }


def test_eval_signals_use_the_latest_run_only(engine: Engine) -> None:
    now = datetime.now(UTC)

    def add(run_id: str, suite: str, config: str, ago: int, failed: list[str]) -> None:
        with Session(engine) as s, s.begin():
            s.add(
                EvalRun(
                    id=run_id, suite=suite, config_name=config,
                    started_at=now - timedelta(hours=ago), ended_at=now,
                )
            )  # fmt: skip
            s.flush()
            for t in failed:
                notes = json.dumps({"kind": "off_by_one"})
                s.add(
                    EvalResult(
                        eval_run_id=run_id, task_id=t, repeat_idx=0, passed=False, notes=notes
                    )
                )

    add("OLD", "golden-review", "reviewer", 5, ["T001/M1", "T002/M1"])
    add("NEW", "golden-review", "reviewer", 1, ["T003/M2"])
    add("GATE", "golden-review", "gate-after-abc", 0, ["T004/M1"])
    got = sig.eval_signals(engine, now - timedelta(days=1))
    assert [s.external_id for s in got] == ["eval:NEW:T003/M2:0"]
    assert "missed a planted off_by_one bug" in got[0].text and not got[0].human


def test_reviewer_findings_skip_failed_checks(engine: Engine) -> None:
    record_run(engine, "R1", "reviewer", ticket_key="CODEIT-5")
    verdict = {
        "findings": [
            {"severity": "critical", "issue": "Check `unit` failed: exit 1", "suggestion": ""},
            {"severity": "critical", "issue": "No test for 404.", "suggestion": "Add one."},
            {"severity": "minor", "issue": "Naming.", "suggestion": ""},
        ]
    }
    finish_run(
        engine, "R1", "reviewer", {"status": "fail_critical", "result_json": {"verdict": verdict}}
    )
    got = sig.reviewer_signals(engine, datetime.now(UTC) - timedelta(days=1))
    assert [s.text for s in got] == ["[CODEIT-5] No test for 404. Suggestion: Add one."]


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


async def test_jira_signals_are_human_comments_only() -> None:
    comments = [
        ("1", "Picked up by coder-1 (run R).\n\n*(CodeIt orchestrator)*"),
        ("2", "Done.\n\n*Posted by coder (run R)*"),
        ("3", "**Review: pass**\n\n*(CodeIt reviewer, run R)*"),
        ("4", "Please test the empty list too. #learn"),
    ]
    with respx.mock(base_url=API) as router:
        router.post("/search/jql").respond(
            json={"issues": [issue_json("CODEIT-7", status={"name": "Rejected"})]}
        )
        router.get("/issue/CODEIT-7/comment").respond(
            json={
                "comments": [
                    {
                        "id": cid,
                        "author": {"displayName": "Onix"},
                        "body": _adf(text),
                        "created": "2026-09-29T10:00:00.000+0000",
                    }
                    for cid, text in comments
                ],
                "total": len(comments),
            }
        )
        async with JiraClient(BASE, "e", "t") as jira:
            got = await sig.jira_signals(
                jira, IDS, "CODEIT", BASE, datetime(2026, 9, 1, tzinfo=UTC)
            )
    assert [s.external_id for s in got] == ["jira-comment:4"]
    assert got[0].learn and got[0].source == "jira_rejection"
    assert got[0].url == f"{BASE}/browse/CODEIT-7?focusedCommentId=4"
    assert FIELDS  # the fake ticket carries the usual custom fields


# the gate -----------------------------------------------------------------------------------


def test_compare_marks_regressions_beyond_tolerance() -> None:
    rows = gates.compare(
        "reviewer",
        {"critical_catch_rate": 0.90, "false_fail_rate": 0.0},
        {"critical_catch_rate": 0.86, "false_fail_rate": 0.10},
        0.05,
    )
    assert [(r.label, r.worse) for r in rows] == [
        ("Reviewer: planted bugs caught", False),  # -0.04 is within tolerance
        ("Reviewer: correct patches failed", True),  # lower is better
    ]
    text = gates.table(rows, 0.05)
    assert (
        "| Reviewer: correct patches failed (lower is better) | 0.00 | 0.10 | +0.10 **worse** |"
        in text
    )


def _proposal(engine: Engine, targets: list[str], pr_url: str | None = None) -> str:
    with Session(engine) as s, s.begin():
        s.add(
            LearningProposal(
                id="P" * 26, run_id="R" * 26, repo="target", branch="learning/x",
                pr_url=pr_url, changes_json={"files": {
                    t: {"path": f"{t}.md", "before": "a", "after": "b"} for t in targets
                }},
                gate_status="pending", created_at=datetime.now(UTC),
            )
        )  # fmt: skip
    return "P" * 26


async def test_gate_waits_for_a_part_then_finishes(
    engine: Engine, cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _proposal(engine, ["coder", "planner"])
    summaries = {
        "cb": {"pass@1": 1.0, "hidden_pass_ratio": 1.0},
        "ca": {"pass@1": 0.5, "hidden_pass_ratio": 0.9},
        "pb": {"schema_valid": 1.0, "invest": 0.8, "coverage": 1.0},
        "pa": {"schema_valid": 1.0, "invest": 0.85, "coverage": 1.0},
    }
    for eid, summary in summaries.items():
        with Session(engine) as s, s.begin():
            s.add(
                EvalRun(
                    id=eid,
                    suite="x",
                    config_name="gate-x",
                    started_at=datetime.now(UTC),
                    summary_json=summary,
                )
            )
    claude_ok = False

    planner_calls: list[int] = []

    async def part(deps: Any, proposal_id: str, target: str, change: Any, workdir: Path) -> Any:
        if target == "coder":
            return ("cb", "ca") if claude_ok else None
        planner_calls.append(1)
        if len(planner_calls) == 1:
            raise BackendError("claude_code_chat: You've hit your session limit")
        return ("pb", "pa")

    monkeypatch.setattr(gates, "run_part", part)
    deps = gates.GateDeps(cfg, SECRETS, engine, sandbox=None, echo=lambda _: None)  # type: ignore[arg-type]
    p = await gates.run_gate(deps, pid, tmp_path)  # usage limit: nothing done, no crash
    assert p.gate_status == "pending" and not (p.gate_json or {})["parts"]
    p = await gates.run_gate(deps, pid, tmp_path)
    assert p.gate_status == "pending" and set((p.gate_json or {})["parts"]) == {"planner"}
    claude_ok = True
    p = await gates.run_gate(deps, pid, tmp_path)
    assert p.gate_status == "regression" and p.gated_at is not None
    assert "| Coder pass@1 | 1.00 | 0.50 | -0.50 **worse** |" in (p.gate_json or {})["table"]


# the PR text --------------------------------------------------------------------------------


def test_pr_body_and_gate_section() -> None:
    change = learning_run.FileChange(
        "coder", "CLAUDE.md", "a", "b",
        rules=[{
            "rule": "Test the 400 case.", "lesson": "api-error-tests", "theme": "testing",
            "count": 2, "effect": "Fewer send-backs.",
            "evidence": [{"text": "Only happy path tested.", "url": "https://x/1"}],
        }],
        skipped=[{"lesson": "naming", "reason": "already in the file"}],
        addressed=[1, 2], skipped_signals=[3],
    )  # fmt: skip
    body = pr_body("RUN", [change], "_Pending._")
    assert "### `CLAUDE.md` (Coder)" in body and "- **Test the 400 case.**" in body
    assert "[source](https://x/1): Only happy path tested." in body
    assert "- `naming`: already in the file" in body
    assert body.rstrip().endswith("## Eval gate\n\n_Pending._")
    done = with_gate(body, "| table |")
    assert done.count("## Eval gate") == 1 and done.rstrip().endswith("| table |")
    assert "_Pending._" not in done


async def test_report_gate_updates_the_pr_and_labels_a_regression(
    engine: Engine, cfg: Config
) -> None:
    pid = _proposal(engine, ["coder"], pr_url=f"https://github.com/{SLUG}/pull/40")
    with Session(engine, expire_on_commit=False) as s, s.begin():
        p = s.get(LearningProposal, pid)
        assert p is not None
        p.gate_status, p.gate_json = "regression", {"table": "| t |"}
    pr = {
        "number": 40, "html_url": f"https://github.com/{SLUG}/pull/40", "state": "open",
        "merged": False, "head": {"ref": "learning/x", "sha": "h"},
        "base": {"ref": "main", "sha": "b"}, "body": "Hi\n\n## Eval gate\n\n_Pending._\n",
    }  # fmt: skip
    with respx.mock(base_url="https://api.github.com") as router:
        router.get(f"/repos/{SLUG}/pulls/40").respond(json=pr)
        patch = router.patch(f"/repos/{SLUG}/pulls/40").respond(json=pr)
        labels = router.post(f"/repos/{SLUG}/issues/40/labels").respond(json=[])
        await report_gate(cfg, SECRETS, p, lambda _: None)
    body = json.loads(patch.calls[0].request.content)["body"]
    assert body.startswith("Hi\n\n## Eval gate\n\n**Regression:**") and "| t |" in body
    assert json.loads(labels.calls[0].request.content) == {"labels": ["regression"]}


# a whole run --------------------------------------------------------------------------------


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def _repo(tmp_path: Path, name: str, files: dict[str, str]) -> tuple[Path, Path]:
    bare = tmp_path / f"{name}.git"
    sh("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / name
    sh("clone", "-q", str(bare), str(work), cwd=tmp_path)
    sh("checkout", "-qb", "main", cwd=work)
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    sh("add", "-A", cwd=work)
    sh("commit", "-qm", "init", cwd=work)
    sh("push", "-q", "origin", "main", cwd=work)
    return bare, work


@pytest.fixture
def repos(
    tmp_path: Path, cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path]]:
    """(the target's bare repo, a stand-in CodeIt checkout)."""
    bare, _ = _repo(tmp_path, "target", {"CLAUDE.md": "# Sandbox\n\n- Use npm.\n"})
    _, codeit = _repo(
        tmp_path,
        "codeit",
        {
            "prompts/reviewer/checklist.md": "# Reviewer checklist\n\n- Tests exist.\n",
            "prompts/planner/system.md": "You are the Planner.\n",
        },
    )

    class LocalClones(CloneManager):
        def __init__(
            self, data_dir: Path, name: str, url: str, branch: str = "main", git: Any = None
        ) -> None:
            super().__init__(data_dir, name, str(bare), branch, git)

    monkeypatch.setattr(learning_run, "CloneManager", LocalClones)
    monkeypatch.setattr(learning_run, "REPO_ROOT", codeit)
    yield bare, codeit


def _propose(key: str, rule: str) -> dict[str, Any]:
    return {
        "rules": [{"lesson_key": key, "rule": rule, "expected_effect": "Better."}],
        "skipped": [],
    }


async def test_run_on_the_fixture_opens_a_pr_and_a_patch(
    cfg: Config, engine: Engine, repos: tuple[Path, Path]
) -> None:
    bare, _ = repos
    answers = {i: ("coder", "api-error-tests", True) for i in (1, 2, 3, 4)}
    answers |= {i: ("reviewer", "in-flight-guard", True) for i in (5, 6, 7)}
    answers |= {i: ("planner", "empty-state", True) for i in (8, 9, 10)}
    answers |= {11: ("coder", "thanks", False), 12: ("coder", "ci-outage", False)}
    backend = FakeBackend(
        "claude_code_chat",
        [
            classified(list(range(1, 13)), answers),
            _propose("api-error-tests", "Test the 400 and 404 responses of every endpoint."),
            _propose("empty-state", "Define the empty state of every list in the criteria."),
            _propose("in-flight-guard", "Buttons are disabled while their request runs."),
        ],
    )
    created: list[dict[str, Any]] = []

    def create(request: httpx.Request) -> httpx.Response:
        created.append(json.loads(request.content))
        return httpx.Response(201, json={
            "number": 41, "html_url": f"https://github.com/{SLUG}/pull/41", "state": "open",
            "merged": False, "head": {"ref": created[-1]["head"], "sha": "h"},
            "base": {"ref": "main", "sha": "b"},
        })  # fmt: skip

    lines: list[str] = []
    with respx.mock(base_url="https://api.github.com") as router:
        router.get(f"/repos/{SLUG}/pulls").respond(json=[])
        router.post(f"/repos/{SLUG}/pulls").mock(side_effect=create)
        result = await run_learning(
            cfg, SECRETS, IDS, echo=lines.append, backends=[backend],
            signals_file=FIXTURE, gate=False, today=date(2026, 9, 30),
        )  # fmt: skip

    assert result.new_signals == 12 and result.classified == 12
    assert sorted(result.lessons) == [
        "coder:api-error-tests", "planner:empty-state", "reviewer:in-flight-guard",
    ]  # fmt: skip
    branch = created[0]["head"]
    assert branch.startswith("learning/2026-09-30-")
    claude_md = sh("show", f"{branch}:CLAUDE.md", cwd=bare)
    assert claude_md.endswith("- Test the 400 and 404 responses of every endpoint.")
    assert "Lesson `api-error-tests` (testing), from 4 signal(s)" in created[0]["body"]
    assert "**Test run:** the signals come from the synthetic fixture" in created[0]["body"]
    patch_path = next(p for p in result.proposals if p.endswith("codeit.patch"))
    patch = Path(patch_path).read_text()
    assert "+- Buttons are disabled while their request runs." in patch
    assert "+- Define the empty state of every list in the criteria." in patch
    assert "prompts/planner/system.md" in Path(patch_path).with_name("pr.md").read_text()
    with Session(engine) as s:
        status = {r.external_id: r.status for r in s.scalars(select(Signal))}
        proposals = s.scalars(select(LearningProposal)).all()
    assert list(status.values()).count("addressed") == 10
    assert list(status.values()).count("ignored") == 2
    assert {p.repo: p.gate_status for p in proposals} == {"target": "pending", "codeit": "pending"}
    files = {p.repo: set(p.changes_json["files"]) for p in proposals}
    assert files == {"target": {"coder"}, "codeit": {"planner", "reviewer"}}
