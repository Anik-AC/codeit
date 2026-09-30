"""The Docs agent: the file writers, and a whole run with real git against a local
"GitHub" (a bare repo), fake Jira and GitHub over respx, and a scripted model."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from codeit import db
from codeit.agents import docs
from codeit.agents.docs import (
    LABEL,
    STATUS_END,
    STATUS_START,
    DesignDecision,
    Entry,
    Shipped,
    changelog_section,
    complete_entries,
    next_adr_number,
    run_docs,
    slugify,
    status_block,
    update_changelog,
    update_readme,
    worklog,
)
from codeit.config import Config, Secrets, load_config
from codeit.runs import finish_run, record_run
from codeit.sandbox.clone import CloneManager
from tests.conftest import REPO_ROOT
from tests.unit.agents.conftest import FakeBackend
from tests.unit.jira.conftest import API, BASE
from tests.unit.orchestrator.fake_jira import IDS, FakeJira

SLUG = "Anik-AC/codeit-sandbox-app"
DAY = date(2026, 9, 30)
SECRETS = Secrets(
    _env_file=None,
    jira_base_url=BASE,
    jira_email="e",
    jira_api_token=SecretStr("j"),
    github_token_agent=SecretStr("gh"),
)


def shipped(key: str, pr: int | None = None, epic: str | None = "Tasks") -> Shipped:
    return Shipped(
        key=key,
        summary=f"Summary of {key}",
        description="",
        epic=epic,
        review_loop=1,
        human_returns=0,
        pr_number=pr,
        pr_url=f"https://github.com/{SLUG}/pull/{pr}" if pr else None,
        cost_usd=0.5,
        cost_by_role={"coder": 0.4, "reviewer": 0.1},
    )


# the writers --------------------------------------------------------------------------


def test_missing_entries_fall_back_to_the_summary() -> None:
    items = [shipped("CODEIT-1"), shipped("CODEIT-2")]
    got = complete_entries(items, [Entry(key="CODEIT-1", category="Added", text="Due dates")])
    assert got["CODEIT-1"].text == "Due dates"
    assert got["CODEIT-2"] == Entry(key="CODEIT-2", category="Changed", text="Summary of CODEIT-2.")


def test_changelog_groups_by_epic_with_references() -> None:
    items = [shipped("CODEIT-1", 7), shipped("CODEIT-2", epic=None), shipped("CODEIT-3", 9)]
    entries = complete_entries(
        items,
        [
            Entry(key="CODEIT-1", category="Fixed", text="Search ignores case."),
            Entry(key="CODEIT-3", category="Added", text="Due dates on tasks"),
        ],
    )
    section = changelog_section(DAY, items, entries)
    assert section.index("### Tasks") < section.index("### Other changes")
    assert "- **Added:** Due dates on tasks. (CODEIT-3, #9)" in section
    assert "- **Fixed:** Search ignores case. (CODEIT-1, #7)" in section
    assert section.index("Added") < section.index("Fixed")
    assert "(CODEIT-2)" in section


def test_changelog_new_existing_and_same_day() -> None:
    first = update_changelog(None, DAY, "## 2026-09-30\n\n### A\n\n- one\n")
    assert first.startswith("# Changelog") and first.rstrip().endswith("- one")
    older = first.replace("2026-09-30", "2026-09-29")
    newer = update_changelog(older, DAY, "## 2026-09-30\n\n### A\n\n- two\n")
    assert newer.index("## 2026-09-30") < newer.index("## 2026-09-29")
    same = update_changelog(first, DAY, "## 2026-09-30\n\n### B\n\n- three\n")
    assert same.count("## 2026-09-30") == 1 and "- one" in same and "- three" in same


def test_readme_status_block_inserted_then_replaced() -> None:
    block = status_block({"shipped": 4, "median_loops": 1.0, "first_pass": 0.75}, DAY)
    assert "| Tickets shipped | 4 |" in block and "| Reviewer first-pass rate | 75% |" in block
    assert "eval" not in block  # no eval run yet
    evals = {"pass@1": 1.0, "pass^k": 0.5, "k": "3", "eval_tasks": 2, "catch_rate": 0.96}
    block = status_block({**evals, "false_fails": 0.0}, DAY)
    assert "| Coder eval pass@1 (2 golden tasks) | 100% |" in block
    assert "| Coder eval pass^3 | 50% |" in block
    assert "| Reviewer eval: critical bugs caught | 96% |" in block
    text = update_readme("# App\n\nHello.\n", status_block({"shipped": 4}, DAY))
    assert text.startswith("# App\n\n" + STATUS_START) and text.endswith("Hello.\n")
    again = update_readme(text, status_block({"shipped": 5}, DAY))
    assert again.count(STATUS_START) == 1 and again.count(STATUS_END) == 1
    assert "| Tickets shipped | 5 |" in again


def test_adr_numbers_and_slugs() -> None:
    assert next_adr_number([]) == 1
    assert next_adr_number(["0001-a.md", "0007-b.md", "README.md"]) == 8
    assert slugify("Use SQLite, not Postgres!") == "use-sqlite-not-postgres"


def test_worklog_table_decisions_and_cost() -> None:
    items = [shipped("CODEIT-1", 7), shipped("CODEIT-2")]
    d = DesignDecision(key="CODEIT-1", title="T", context="c", decision="d", consequences="x")
    text = worklog(DAY, items, complete_entries(items, []), ["Fast"], [(d, "../adr/0001-t.md")])
    assert f"| CODEIT-1 | Summary of CODEIT-1 | [#7](https://github.com/{SLUG}/pull/7) |" in text
    assert "- Fast" in text and "[../adr/0001-t.md]" in text
    assert "- coder: $0.80" in text and "- Total: $1.00" in text


# a whole run --------------------------------------------------------------------------


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    sh("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "work"
    sh("clone", "-q", str(bare), str(work), cwd=tmp_path)
    sh("checkout", "-qb", "main", cwd=work)
    (work / "README.md").write_text("# Sandbox\n\nA task app.\n")
    (work / "docs" / "adr").mkdir(parents=True)
    (work / "docs" / "adr" / "0003-old.md").write_text("# 3. Old\n")
    sh("add", "-A", cwd=work)
    sh("commit", "-qm", "init", cwd=work)
    sh("push", "-q", "origin", "main", cwd=work)
    return bare


@pytest.fixture
def cfg(tmp_path: Path, bare: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    class LocalClones(CloneManager):
        def __init__(
            self, data_dir: Path, name: str, url: str, branch: str = "main", git: Any = None
        ) -> None:
            super().__init__(data_dir, name, str(bare), branch, git)

    monkeypatch.setattr(docs, "CloneManager", LocalClones)
    return load_config(REPO_ROOT / "config" / "config.yaml").model_copy(
        update={"data_dir": tmp_path / "data"}
    )


@pytest.fixture
def jira() -> Iterator[FakeJira]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        fake = FakeJira(router)
        fake.add("CODEIT-9", "Done", pr_url=f"https://github.com/{SLUG}/pull/5")
        fake.add("CODEIT-10", "Done", labels=[LABEL])  # written up by an earlier run
        fake.add("CODEIT-11", "In Dev")
        yield fake


@pytest.fixture
def github() -> Iterator[list[dict[str, Any]]]:
    created: list[dict[str, Any]] = []

    def pr(number: int, head: str, state: str = "closed") -> dict[str, Any]:
        return {
            "number": number,
            "html_url": f"https://github.com/{SLUG}/pull/{number}",
            "state": state,
            "merged": state == "closed",
            "title": "CODEIT-9: Search",
            "body": "Adds search.",
            "head": {"ref": head, "sha": "h"},
            "base": {"ref": "main", "sha": "b"},
            "merge_commit_sha": "m" if state == "closed" else None,
        }

    def create(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        created.append(body)
        return httpx.Response(201, json=pr(20, body["head"], "open"))

    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as router:
        router.get(f"/repos/{SLUG}/pulls/5").respond(json=pr(5, "CODEIT-9-search"))
        router.get(f"/repos/{SLUG}/pulls/5/reviews").respond(
            json=[
                {"body": "a human note"},
                {"body": "Verdict: pass. Tidy search.\n\n_(CodeIt reviewer, run R)_"},
            ]
        )
        router.get(f"/repos/{SLUG}/pulls").respond(json=[])
        router.post(f"/repos/{SLUG}/pulls").mock(side_effect=create)
        yield created


ANSWER = {
    "entries": [{"key": "CODEIT-9", "category": "Added", "text": "Search tasks by title"}],
    "decisions": [
        {
            "key": "CODEIT-9",
            "title": "Search in the database, not the client",
            "context": "Lists can be long.",
            "decision": "Use a LIKE query.",
            "consequences": "Needs an index later.",
        },
        {"key": "CODEIT-99", "title": "Made up", "context": "", "decision": "", "consequences": ""},
    ],
    "highlights": ["Search shipped in one review loop."],
}


async def test_run_writes_docs_opens_a_pr_and_labels(
    cfg: Config, bare: Path, jira: FakeJira, github: list[dict[str, Any]]
) -> None:
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    record_run(engine, "C1", "coder", ticket_key="CODEIT-9")
    finish_run(engine, "C1", "coder", {"status": "pr_opened", "cost_usd": 0.42})
    backend = FakeBackend("claude_code_chat", [ANSWER])
    lines: list[str] = []

    result = await run_docs(cfg, SECRETS, IDS, echo=lines.append, backends=[backend], today=DAY)

    assert result.shipped == ["CODEIT-9"]
    assert result.pr_url == f"https://github.com/{SLUG}/pull/20"
    assert github[0]["head"] == "docs/2026-09-30" and github[0]["base"] == "main"
    assert jira.issues["CODEIT-9"].labels[-1] == LABEL
    prompt = backend.requests[0].messages[-1].content
    assert "Tidy search." in prompt and "a human note" not in prompt

    def show(path: str) -> str:
        return sh("show", f"docs/2026-09-30:{path}", cwd=bare)

    changelog = show("CHANGELOG.md")
    assert "- **Added:** Search tasks by title. (CODEIT-9, #5)" in changelog
    assert "0004-search-in-the-database-not-the-client.md" in sh(
        "ls-tree", "--name-only", "docs/2026-09-30", "docs/adr/", cwd=bare
    )
    assert "Made up" not in changelog + show("docs/worklog/2026-09-30.md")  # unknown key
    log = show("docs/worklog/2026-09-30.md")
    assert "| CODEIT-9 |" in log and "$0.42" in log and "one review loop" in log
    readme = show("README.md")
    assert readme.startswith("# Sandbox\n\n" + STATUS_START) and "| Tickets shipped | 2 |" in readme

    # The next run has nothing new: CODEIT-9 is labelled now.
    again = await run_docs(cfg, SECRETS, IDS, echo=lines.append, backends=[backend], today=DAY)
    assert again.shipped == [] and again.pr_url is None
    assert len(github) == 1
