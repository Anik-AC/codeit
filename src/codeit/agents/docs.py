"""The Docs agent (PRD 11.6, ADR-0018): once a day, write up what shipped.

1. Collect Done tickets without the `docs-logged` label, with their PRs, the Reviewer's
   summary and CodeIt's run costs.
2. One model call (the Claude subscription, `routing.docs`) writes a user-facing changelog
   line per ticket, and picks out design decisions worth an ADR and highlights for the
   work log.
3. CodeIt writes the files itself, in the target repo, on branch `docs/{date}`:
   - `CHANGELOG.md` (Keep a Changelog, grouped by Epic, lines ending `({KEY}, #PR)`)
   - `docs/worklog/{date}.md` (tickets, review loops, human returns, decisions, cost)
   - `docs/adr/NNNN-{slug}.md` for each decision
   - the README status block between the `codeit:status` markers
4. It opens (or updates) one PR, then labels the tickets `docs-logged`.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from ulid import ULID

from codeit import db
from codeit.agents.coder import REVIEWER_MARK
from codeit.backends.base import BackendUnavailable, ChatBackend, ChatMessage
from codeit.backends.registry import chat_route
from codeit.backends.structured import ModelCall, structured
from codeit.config import Config, Secrets
from codeit.db.models import EvalRun, Run
from codeit.git import Git
from codeit.github_client import GitHubClient, repo_slug
from codeit.github_client.prs import PullRequest, create_pr, find_pr, reviewer_summary
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.issues import add_labels, get_ticket
from codeit.jira_client.models import Ticket
from codeit.jira_client.search import search_tickets
from codeit.log import get_logger
from codeit.orchestrator.budget import record_spend
from codeit.orchestrator.leases import LeaseStore
from codeit.prompts import prompt_hash, render
from codeit.runs import finish_run, record_run
from codeit.sandbox.clone import CloneManager

log = get_logger(__name__)

ROLE: Final = "docs"
LABEL: Final = "docs-logged"
LEASE_KEY: Final = "DOCS"
STATUS_START = "<!-- codeit:status:start -->"
STATUS_END = "<!-- codeit:status:end -->"
CHANGELOG_HEAD = (
    "# Changelog\n\nWhat changed for users, written by CodeIt's Docs agent from merged tickets.\n"
    "The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).\n"
)
MAX_TEXT = 2500

Echo = Callable[[str], None]
Category = Literal["Added", "Changed", "Fixed", "Removed", "Security"]


class DocsError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entry(_Strict):
    key: str
    category: Category
    text: str


class DesignDecision(_Strict):
    key: str
    title: str
    context: str
    decision: str
    consequences: str


class DocsAnswer(_Strict):
    entries: list[Entry]
    decisions: list[DesignDecision] = []
    highlights: list[str] = []


@dataclass
class Shipped:
    key: str
    summary: str
    description: str
    epic: str | None
    review_loop: int
    human_returns: int
    pr_number: int | None = None
    pr_url: str | None = None
    pr_title: str = ""
    pr_body: str = ""
    merge_sha: str | None = None
    review_summary: str | None = None
    cost_usd: float = 0.0
    cost_by_role: dict[str, float] = field(default_factory=dict)


@dataclass
class DocsRun:
    run_id: str
    shipped: list[str]
    pr_url: str | None
    files: list[str]
    reason: str = ""


# collecting ----------------------------------------------------------------------------


def done_jql(project_key: str) -> str:
    not_logged = f'(labels IS EMPTY OR labels NOT IN ("{LABEL}"))'
    return f"project = {project_key} AND status = Done AND {not_logged} ORDER BY updated ASC"


def _costs(engine: Engine, key: str) -> dict[str, float]:
    with Session(engine) as s:
        rows = s.execute(
            select(Run.role, func.coalesce(func.sum(Run.cost_usd), 0.0))
            .where(Run.ticket_key == key)
            .group_by(Run.role)
        ).all()
    return {str(role): round(float(total or 0.0), 4) for role, total in rows}


async def collect(
    jira: JiraClient, gh: GitHubClient, ids: JiraIds, slug: str, project_key: str, engine: Engine
) -> list[Shipped]:
    out = []
    epics: dict[str, str] = {}
    async for t in search_tickets(jira, done_jql(project_key), ids.fields):
        if LABEL in t.labels:
            continue
        if t.epic_key and t.epic_key not in epics:
            epics[t.epic_key] = (await get_ticket(jira, t.epic_key, ids.fields)).summary
        costs = _costs(engine, t.key)
        item = Shipped(
            key=t.key,
            summary=t.summary,
            description=t.description_md[:MAX_TEXT],
            epic=epics.get(t.epic_key) if t.epic_key else None,
            review_loop=t.review_loop,
            human_returns=t.human_returns,
            cost_usd=round(sum(costs.values()), 4),
            cost_by_role=costs,
        )
        number = re.search(r"/pull/(\d+)", t.pr_url or "")
        if number:
            raw = await gh.get_json(f"/repos/{slug}/pulls/{number.group(1)}")
            pr = PullRequest.from_api(raw)
            item.pr_number, item.pr_url = pr.number, pr.url
            item.pr_title = str(raw.get("title") or "")
            item.pr_body = str(raw.get("body") or "")[:MAX_TEXT]
            item.merge_sha = pr.merge_commit_sha
            summary = await reviewer_summary(gh, slug, pr.number, REVIEWER_MARK)
            item.review_summary = summary[:MAX_TEXT] if summary else None
        out.append(item)
    return out


# writing ------------------------------------------------------------------------------


def _ref(s: Shipped) -> str:
    return f"({s.key}, #{s.pr_number})" if s.pr_number else f"({s.key})"


def complete_entries(shipped: Sequence[Shipped], entries: Sequence[Entry]) -> dict[str, Entry]:
    """One entry per ticket: the model's, or one made from the ticket summary if it left
    a ticket out."""
    by_key = {e.key: e for e in entries}
    return {
        s.key: by_key.get(s.key)
        or Entry(key=s.key, category="Changed", text=s.summary.rstrip(".") + ".")
        for s in shipped
    }


def changelog_section(day: date, shipped: Sequence[Shipped], entries: dict[str, Entry]) -> str:
    groups: dict[str, list[Shipped]] = defaultdict(list)
    for s in shipped:
        groups[s.epic or "Other changes"].append(s)
    lines = [f"## {day.isoformat()}", ""]
    for epic in sorted(groups, key=lambda g: (g == "Other changes", g)):
        lines += [f"### {epic}", ""]
        for s in sorted(groups[epic], key=lambda s: (entries[s.key].category, s.key)):
            e = entries[s.key]
            lines.append(f"- **{e.category}:** {e.text.strip().rstrip('.')}. {_ref(s)}")
        lines.append("")
    return "\n".join(lines)


def update_changelog(existing: str | None, day: date, section: str) -> str:
    """Put today's section under the header, merging with an existing section for today."""
    text = existing or CHANGELOG_HEAD
    heading = f"## {day.isoformat()}"
    if heading in text:
        # Same day again: add the new groups after today's heading.
        _, _, new_body = section.partition(heading)
        return text.replace(heading, heading + new_body.rstrip("\n"), 1)
    m = re.search(r"^## ", text, flags=re.M)
    if m:
        return text[: m.start()] + section + "\n" + text[m.start() :]
    return text.rstrip("\n") + "\n\n" + section


def worklog(
    day: date,
    shipped: Sequence[Shipped],
    entries: dict[str, Entry],
    highlights: Sequence[str],
    decisions: Sequence[tuple[DesignDecision, str]],
) -> str:
    rows = [
        f"| {s.key} | {s.summary} | {f'[#{s.pr_number}]({s.pr_url})' if s.pr_number else '-'} "
        f"| {s.review_loop} | {s.human_returns} | ${s.cost_usd:.2f} |"
        for s in shipped
    ]
    by_role: dict[str, float] = defaultdict(float)
    for s in shipped:
        for role, usd in s.cost_by_role.items():
            by_role[role] += usd
    lines = [
        f"# Work log {day.isoformat()}",
        "",
        f"Written by CodeIt's Docs agent. {len(shipped)} ticket(s) shipped.",
        "",
        "## Tickets closed",
        "",
        "| Ticket | Summary | PR | Review loops | Human returns | Agent cost |",
        "|---|---|---|---|---|---|",
        *rows,
        "",
        "## Notable",
        "",
    ]
    lines += [f"- {h}" for h in highlights] or ["- Nothing unusual."]
    lines += ["", "## Decisions", ""]
    lines += [f"- {d.title} ({d.key}): [{path}]({path})" for d, path in decisions] or [
        "- None recorded."
    ]
    lines += ["", "## Agent cost", ""]
    if by_role:
        lines += [f"- {role}: ${usd:.2f}" for role, usd in sorted(by_role.items())]
        lines.append(f"- Total: ${sum(by_role.values()):.2f}")
    else:
        lines.append("- No recorded agent cost.")
    lines.append("")
    return "\n".join(lines)


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "decision"


def next_adr_number(names: Sequence[str]) -> int:
    numbers = [int(m.group(1)) for n in names if (m := re.match(r"^(\d{4})-", n))]
    return max(numbers, default=0) + 1


def adr_text(number: int, d: DesignDecision, s: Shipped | None, day: date) -> str:
    source = f"{d.key}" + (f", [PR #{s.pr_number}]({s.pr_url})" if s and s.pr_number else "")
    return (
        f"# {number:04d}. {d.title}\n\n"
        f"- **Status:** Accepted\n- **Date:** {day.isoformat()}\n- **Source:** {source}\n\n"
        f"## Context\n\n{d.context.strip()}\n\n"
        f"## Decision\n\n{d.decision.strip()}\n\n"
        f"## Consequences\n\n{d.consequences.strip()}\n\n"
        "_Recorded by CodeIt's Docs agent from the pull request and its review._\n"
    )


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{round(value * 100)}%"


def status_block(metrics: dict[str, Any], day: date) -> str:
    rows = [
        ("Tickets shipped", str(metrics.get("shipped", 0))),
        (
            "Median review loops",
            "-" if metrics.get("median_loops") is None else f"{metrics['median_loops']:g}",
        ),
        ("Reviewer first-pass rate", _pct(metrics.get("first_pass"))),
    ]
    tasks = metrics.get("eval_tasks")
    if metrics.get("pass@1") is not None:
        on = f" ({tasks} golden tasks)" if tasks else ""
        rows.append((f"Coder eval pass@1{on}", _pct(metrics["pass@1"])))
        rows.append((f"Coder eval pass^{metrics.get('k', 3)}", _pct(metrics.get("pass^k"))))
    if metrics.get("catch_rate") is not None:
        rows.append(("Reviewer eval: critical bugs caught", _pct(metrics["catch_rate"])))
        rows.append(("Reviewer eval: false fails", _pct(metrics.get("false_fails"))))
    table = "\n".join(f"| {name} | {value} |" for name, value in rows)
    return (
        f"{STATUS_START}\n| Metric | Value |\n|---|---|\n{table}\n\n"
        f"<sub>Updated {day.isoformat()} by CodeIt.</sub>\n{STATUS_END}"
    )


def update_readme(text: str, block: str) -> str:
    """Replace the status block, or put one under the first heading if there is none."""
    pattern = re.compile(re.escape(STATUS_START) + r".*?" + re.escape(STATUS_END), re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1)
    m = re.search(r"^# .*\n", text, flags=re.M)
    at = m.end() if m else 0
    return text[:at] + "\n" + block + "\n" + text[at:]


def metrics(engine: Engine, done: Sequence[Ticket]) -> dict[str, Any]:
    with Session(engine) as s:
        reviews = s.execute(
            select(Run.ticket_key, Run.status, Run.started_at)
            .where(Run.role == "reviewer", Run.ticket_key.is_not(None))
            .order_by(Run.started_at)
        ).all()
        latest, review = (
            s.scalars(
                select(EvalRun)
                .where(EvalRun.suite == suite, EvalRun.ended_at.is_not(None))
                .order_by(EvalRun.started_at.desc())
                .limit(1)
            ).first()
            for suite in ("golden", "golden-review")
        )
    first: dict[str, str] = {}
    for key, status, _ in reviews:
        if status in ("pass", "pass_with_notes", "fail_critical"):
            first.setdefault(str(key), status)
    summary = (latest.summary_json or {}) if latest else {}
    k_key = next((k for k in summary if k.startswith("pass^")), None)
    caught = (review.summary_json or {}) if review else {}
    return {
        "shipped": len(done),
        "median_loops": statistics.median(t.review_loop for t in done) if done else None,
        "first_pass": (sum(v != "fail_critical" for v in first.values()) / len(first))
        if first
        else None,
        "pass@1": summary.get("pass@1"),
        "pass^k": summary.get(k_key) if k_key else None,
        "k": k_key.removeprefix("pass^") if k_key else 3,
        "eval_tasks": summary.get("tasks"),
        "catch_rate": caught.get("critical_catch_rate"),
        "false_fails": caught.get("false_fail_rate"),
    }


# the run ------------------------------------------------------------------------------


async def ask_model(
    backends: Sequence[ChatBackend], shipped: Sequence[Shipped]
) -> tuple[DocsAnswer, ModelCall]:
    messages = [
        ChatMessage("system", render("docs/system.md")),
        ChatMessage("user", render("docs/task.md", tickets=shipped)),
    ]
    return await structured(backends, messages, DocsAnswer, "docs")


async def run_docs(
    cfg: Config,
    secrets: Secrets,
    ids: JiraIds,
    key: str = LEASE_KEY,
    *,
    instance: str = "docs-1",
    run_id: str | None = None,
    sandbox: object | None = None,
    echo: Echo = print,
    backends: Sequence[ChatBackend] | None = None,
    today: date | None = None,
) -> DocsRun:
    """`key` is ignored (the scheduler passes one); the run leases `DOCS` so only one
    docs run happens at a time."""
    if secrets.github_token_agent is None:
        raise DocsError("GITHUB_TOKEN_AGENT is not set; the Docs agent opens a PR with it")
    token = secrets.github_token_agent.get_secret_value()
    repo = cfg.project.target_repo
    slug = repo_slug(repo.url)
    day = today or datetime.now().astimezone().date()
    run_id = run_id or str(ULID())
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    leases = LeaseStore(engine)
    ttl = timedelta(minutes=30)
    if not leases.acquire(LEASE_KEY, ROLE, instance, run_id, ttl):
        raise DocsError("another docs run is going")
    record_run(
        engine, run_id, ROLE, instance=instance, ticket_key=None, prompt_hash=prompt_hash(ROLE)
    )
    try:
        async with (
            leases.keep_alive(LEASE_KEY, run_id, ttl),
            JiraClient.from_secrets(secrets) as jira,
            GitHubClient(token) as gh,
        ):
            project = cfg.project.jira_project_key
            shipped = await collect(jira, gh, ids, slug, project, engine)
            if not shipped:
                finish_run(engine, run_id, ROLE, {"status": "nothing_to_log"})
                echo("docs: nothing new since the last run")
                return DocsRun(run_id, [], None, [], "nothing new")
            echo(f"docs: {len(shipped)} ticket(s): {', '.join(s.key for s in shipped)}")
            try:
                answer, call = await ask_model(backends or chat_route(cfg, secrets, ROLE), shipped)
            except BackendUnavailable as e:
                raise DocsError(f"no model could write the docs: {e}") from e
            if call.backend != "claude_code_chat":
                record_spend(
                    engine,
                    backend=call.backend,
                    role=ROLE,
                    usd=call.cost_usd,
                    requests=call.calls,
                    note=f"docs {run_id}",
                )
            all_done = [
                t
                async for t in search_tickets(
                    jira, f"project = {project} AND status = Done", ids.fields
                )
            ]
            result = await _write_and_open(
                cfg, gh, slug, token, day, shipped, answer, metrics(engine, all_done), echo
            )
            for s in shipped:
                await add_labels(jira, s.key, [LABEL])
            finish_run(
                engine,
                run_id,
                ROLE,
                {
                    "status": "pr_opened",
                    "backend": call.backend,
                    "model": call.model,
                    "turns": call.calls,
                    "cost_usd": call.cost_usd,
                    "result_json": {
                        "pr_url": result.pr_url,
                        "tickets": result.shipped,
                        "files": result.files,
                    },
                },
            )
            echo(f"docs: {result.pr_url}")
            return DocsRun(run_id, result.shipped, result.pr_url, result.files)
    except Exception as e:
        finish_run(engine, run_id, ROLE, {"status": "error", "error": str(e)[:2000]})
        raise
    finally:
        leases.release(LEASE_KEY, run_id)


async def _write_and_open(
    cfg: Config,
    gh: GitHubClient,
    slug: str,
    token: str,
    day: date,
    shipped: Sequence[Shipped],
    answer: DocsAnswer,
    stats: dict[str, Any],
    echo: Echo,
) -> DocsRun:
    repo = cfg.project.target_repo
    git = Git(token)
    clones = CloneManager(cfg.data_dir, repo.name, repo.url, repo.default_branch, git)
    branch = f"docs/{day.isoformat()}"
    prepared = await clones.prepare(f"DOCS-{day.isoformat()}", branch)
    path = prepared.path
    entries = complete_entries(shipped, answer.entries)
    written: list[str] = []

    def write(rel: str, text: str) -> None:
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text, encoding="utf-8")
        written.append(rel)

    changelog = path / "CHANGELOG.md"
    write(
        "CHANGELOG.md",
        update_changelog(
            changelog.read_text(encoding="utf-8") if changelog.exists() else None,
            day,
            changelog_section(day, shipped, entries),
        ),
    )
    by_key = {s.key: s for s in shipped}
    adr_dir = path / "docs" / "adr"
    names = [p.name for p in adr_dir.glob("*.md")] if adr_dir.is_dir() else []
    recorded: list[tuple[DesignDecision, str]] = []
    for d in answer.decisions:
        if d.key not in by_key:
            continue
        number = next_adr_number(names)
        name = f"{number:04d}-{slugify(d.title)}.md"
        names.append(name)
        write(f"docs/adr/{name}", adr_text(number, d, by_key.get(d.key), day))
        recorded.append((d, f"../adr/{name}"))
    worklog_path = f"docs/worklog/{day.isoformat()}.md"
    body = worklog(day, shipped, entries, answer.highlights, recorded)
    existing = path / worklog_path
    if existing.exists():  # a second run on the same day adds to the day's log
        body = existing.read_text(encoding="utf-8").rstrip("\n") + "\n\n---\n\n" + body
    write(worklog_path, body)
    readme = path / "README.md"
    write(
        "README.md",
        update_readme(
            readme.read_text(encoding="utf-8") if readme.exists() else "", status_block(stats, day)
        ),
    )

    await git.run("add", "--", *written, cwd=path)
    keys = ", ".join(s.key for s in shipped)
    await git.run(
        "commit",
        "--quiet",
        "-m",
        f"docs: {day.isoformat()} changelog and work log ({keys})",
        cwd=path,
    )
    await git.run("push", "--quiet", "origin", f"HEAD:{branch}", cwd=path)
    pr = await find_pr(gh, slug, branch)
    if pr is None or pr.state != "open":
        pr_body = (
            f"Written by CodeIt's Docs agent for {day.isoformat()}.\n\n"
            + "\n".join(
                f"- {s.key}: {s.summary}" + (f" (#{s.pr_number})" if s.pr_number else "")
                for s in shipped
            )
            + "\n\nUpdates `CHANGELOG.md`, the work log and the README status block"
            + (f", and records {len(recorded)} decision(s) as ADRs." if recorded else ".")
        )
        pr = await create_pr(
            gh,
            slug,
            head=branch,
            base=repo.default_branch,
            title=f"Docs: {day.isoformat()} changelog and work log",
            body=pr_body,
        )
        echo(f"docs: opened {pr.url}")
    else:
        echo(f"docs: updated {pr.url}")
    return DocsRun("", [s.key for s in shipped], pr.url, written)
