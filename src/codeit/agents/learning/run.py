"""The Learning agent (PRD 11.7, ADR-0019): `codeit run learning`, weekly in `codeit up`.

1. Collect signals (`signals.py`) and store the new ones.
2. Classify new signals into lessons with one model call per batch (`routing.learning`).
3. Pick lessons seen at least twice, or once with `#learn`.
4. For each agent with such lessons, ask the model for one-line rules for its steering
   file, and add them under a managed heading (`steering.py`):
   - Coder: the target repo's `CLAUDE.md`, as a PR on `learning/{date}`
   - Reviewer and Planner: CodeIt's own prompts, as a PR when `GITHUB_TOKEN_CODEIT` is
     set, else as a local branch and a patch under `data/learning/`
5. Run the eval gate (`gate.py`) on every proposal still pending, and put its table on
   the PR. A part that needs Claude outside its window waits for a later run.

It never merges anything.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session
from ulid import ULID

from codeit import db
from codeit.agents.learning import gate as gates
from codeit.agents.learning import lessons as lessons_mod
from codeit.agents.learning import signals as sig
from codeit.agents.learning.lessons import Lesson
from codeit.agents.learning.steering import TARGETS, ProposeAnswer, add_rules
from codeit.backends.base import BackendUnavailable, ChatBackend, ChatMessage
from codeit.backends.registry import chat_route
from codeit.backends.structured import ModelCall, structured
from codeit.config import Config, Secrets
from codeit.db.models import LearningProposal, Signal
from codeit.git import Git
from codeit.github_client import GitHubClient, repo_slug
from codeit.github_client.prs import (
    add_pr_labels,
    create_pr,
    find_pr,
    get_pr,
    update_pr_body,
)
from codeit.jira_client import JiraClient, JiraIds
from codeit.log import get_logger
from codeit.orchestrator.budget import record_spend
from codeit.orchestrator.leases import LeaseStore
from codeit.prompts import REPO_ROOT, prompt_hash, render
from codeit.runs import finish_run, record_run
from codeit.sandbox.clone import BOT_EMAIL, BOT_NAME, CloneManager
from codeit.sandbox.containers import Sandbox

log = get_logger(__name__)

ROLE: Final = "learning"
LEASE_KEY: Final = "LEARNING"
LEASE_TTL = timedelta(minutes=30)
REGRESSION_LABEL = "regression"
GATE_HEADING = "## Eval gate"
Echo = Callable[[str], None]


class LearningError(Exception):
    pass


@dataclass
class LearningRun:
    run_id: str
    new_signals: int = 0
    classified: int = 0
    lessons: list[str] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)  # PR urls or patch paths
    gates: dict[str, str] = field(default_factory=dict)  # proposal id -> gate status


# collecting --------------------------------------------------------------------------------


async def collect(
    cfg: Config, secrets: Secrets, ids: JiraIds, engine: Engine, jira: JiraClient
) -> int:
    """Collect every source and store what is new; returns the number of new signals."""
    since = datetime.now(UTC) - timedelta(days=cfg.learning.lookback_days)
    project = cfg.project.jira_project_key
    raws = await sig.jira_signals(jira, ids, project, secrets.jira_base_url or "", since)
    token = secrets.github_token_readonly or secrets.github_token_agent
    if token is not None:
        async with GitHubClient(token.get_secret_value()) as gh:
            slug = repo_slug(cfg.project.target_repo.url)
            raws += await sig.github_signals(gh, slug, project, since)
    raws += sig.reviewer_signals(engine, since)
    raws += sig.eval_signals(engine, since)
    return sig.store(engine, raws)


def _load_fixture(engine: Engine, path: Path) -> list[int]:
    """Store a fixture's signals afresh; returns their row ids (the run's scope)."""
    raws = sig.load_fixture(path)
    prefix = f"fixture:{path.stem}:"
    with Session(engine) as s, s.begin():
        s.execute(delete(Signal).where(Signal.external_id.startswith(prefix)))
    sig.store(engine, raws)
    with Session(engine) as s:
        return list(s.scalars(select(Signal.id).where(Signal.external_id.startswith(prefix))))


# proposing ---------------------------------------------------------------------------------


async def propose_rules(
    backends: Sequence[ChatBackend], target: str, current: str, lessons: Sequence[Lesson]
) -> tuple[ProposeAnswer, ModelCall]:
    spec = TARGETS[target]
    messages = [
        ChatMessage("system", render("learning/propose.md")),
        ChatMessage(
            "user",
            render(
                "learning/propose_task.md",
                path=spec.path,
                agent=spec.agent,
                current=current,
                lessons=lessons,
            ),
        ),
    ]
    return await structured(backends, messages, ProposeAnswer, "learning_propose")


@dataclass
class FileChange:
    target: str
    path: str
    before: str
    after: str
    rules: list[dict[str, Any]]  # {rule, lesson (key), theme, count, effect, evidence}
    skipped: list[dict[str, str]]
    addressed: list[int]  # signal ids
    skipped_signals: list[int]


async def _change_for(
    backends: Sequence[ChatBackend],
    target: str,
    current: str,
    lessons: Sequence[Lesson],
    calls: list[ModelCall],
) -> FileChange:
    answer, call = await propose_rules(backends, target, current, lessons)
    calls.append(call)
    by_key = {lesson.key: lesson for lesson in lessons}
    wanted = [r for r in answer.rules if r.lesson_key in by_key]
    after, added = add_rules(current, [r.rule for r in wanted])
    added_set = set(added)
    rules, addressed = [], []
    for r in wanted:
        line = " ".join(r.rule.split()).lstrip("-* ").strip()
        if not any(line.startswith(a[:40]) for a in added_set):
            continue
        lesson = by_key[r.lesson_key]
        addressed += [s.id for s in lesson.signals]
        rules.append(
            {
                "rule": line,
                "lesson": lesson.key,
                "theme": lesson.theme,
                "count": len(lesson.signals),
                "effect": r.expected_effect,
                "evidence": [{"text": s.text[:200], "url": s.url} for s in lesson.signals][:6],
            }
        )
    done = {r["lesson"] for r in rules}
    skipped = [{"lesson": s.lesson_key, "reason": s.reason} for s in answer.skipped]
    skipped += [
        {"lesson": key, "reason": "no rule was added (already in the file, or not usable)"}
        for key in by_key
        if key not in done and key not in {s["lesson"] for s in skipped}
    ]
    skipped_signals = [s.id for key in by_key if key not in done for s in by_key[key].signals]
    return FileChange(
        target, TARGETS[target].path, current, after, rules, skipped, addressed, skipped_signals
    )


def pr_body(
    run_id: str, changes: Sequence[FileChange], gate_note: str, fixture: str | None = None
) -> str:
    lines = [
        f"Proposed by CodeIt's Learning agent from review feedback (run `{run_id}`).",
        *(
            [
                f"**Test run:** the signals come from the synthetic fixture `{fixture}`, not "
                "from real feedback; the ticket keys in the evidence do not exist."
            ]
            if fixture
            else []
        ),
        "It adds short rules under the `Learned from feedback` heading. Review them like any "
        "other change; nothing is merged automatically.",
        "",
        "## Changes",
    ]
    for c in changes:
        lines += ["", f"### `{c.path}` ({TARGETS[c.target].agent})", ""]
        for r in c.rules:
            lines.append(f"- **{r['rule']}**")
            lines.append(
                f"  - Lesson `{r['lesson']}` ({r['theme']}), from {r['count']} signal(s). "
                f"Expected effect: {r['effect']}"
            )
            for e in r["evidence"]:
                quote = e["text"].replace("\n", " ")
                lines.append(f"  - {f'[source]({e["url"]}): ' if e.get('url') else ''}{quote}")
        if c.skipped:
            lines += ["", "Skipped:"]
            lines += [f"- `{s['lesson']}`: {s['reason']}" for s in c.skipped]
    lines += ["", GATE_HEADING, "", gate_note]
    return "\n".join(lines) + "\n"


def with_gate(body: str, text: str) -> str:
    """Replace the gate section at the end of a PR body."""
    head, _, _ = body.partition(GATE_HEADING)
    return f"{head.rstrip()}\n\n{GATE_HEADING}\n\n{text}\n"


def _gate_pending_note(changes: Sequence[FileChange], cfg: Config) -> str:
    suites = ", ".join(gates.SUITE_NAME[c.target] for c in changes)
    return (
        f"_Pending: CodeIt runs {suites} before and after this change and puts the table "
        f"here. Tolerance {cfg.eval.regression_tolerance:.2f}._"
    )


# the repos ---------------------------------------------------------------------------------


async def _open_target(
    cfg: Config,
    secrets: Secrets,
    branch: str,
    run_id: str,
    changes: Sequence[FileChange],
    echo: Echo,
    fixture: str | None = None,
) -> tuple[str | None, str | None]:
    """Commit and push to the target repo, open or update the PR. Returns (pr_url, None)."""
    if secrets.github_token_agent is None:
        raise LearningError("GITHUB_TOKEN_AGENT is not set; the target PR needs it")
    token = secrets.github_token_agent.get_secret_value()
    repo = cfg.project.target_repo
    git = Git(token)
    clones = CloneManager(cfg.data_dir, repo.name, repo.url, repo.default_branch, git)
    prepared = await clones.prepare(f"LEARNING-{branch.rsplit('/', 1)[-1]}", branch)
    for c in changes:
        (prepared.path / c.path).write_text(c.after, encoding="utf-8")
    await git.run("add", "--", *[c.path for c in changes], cwd=prepared.path)
    await git.run("commit", "--quiet", "-m", _commit_message(changes), cwd=prepared.path)
    await git.run("push", "--quiet", "origin", f"HEAD:{branch}", cwd=prepared.path)
    slug = repo_slug(repo.url)
    body = pr_body(run_id, changes, _gate_pending_note(changes, cfg), fixture)
    async with GitHubClient(token) as gh:
        pr = await find_pr(gh, slug, branch)
        if pr is None or pr.state != "open":
            pr = await create_pr(
                gh, slug, head=branch, base=repo.default_branch,
                title=f"Learning: {_title(changes)}", body=body,
            )  # fmt: skip
            echo(f"learning: opened {pr.url}")
        else:
            await update_pr_body(gh, slug, pr.number, body)
            echo(f"learning: updated {pr.url}")
    return pr.url, None


async def _open_codeit(
    cfg: Config,
    secrets: Secrets,
    branch: str,
    run_id: str,
    proposal_id: str,
    changes: Sequence[FileChange],
    echo: Echo,
    fixture: str | None = None,
) -> tuple[str | None, str | None]:
    """Commit CodeIt prompt changes in a separate clone of this repo. With
    GITHUB_TOKEN_CODEIT, push and open a PR; without it, leave a patch and a PR text."""
    token = secrets.github_token_codeit.get_secret_value() if secrets.github_token_codeit else None
    git = Git(token)
    path = cfg.data_dir / "repos" / "codeit-self"
    if not (path / ".git").is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
        await git.run("clone", "--quiet", str(REPO_ROOT), str(path))
    else:
        await git.run("fetch", "--quiet", "origin", cwd=path)
    await git.run("config", "user.name", BOT_NAME, cwd=path)
    await git.run("config", "user.email", BOT_EMAIL, cwd=path)
    await git.run("checkout", "--quiet", "-B", branch, "origin/main", cwd=path)
    for c in changes:
        (path / c.path).write_text(c.after, encoding="utf-8")
    await git.run("add", "--", *[c.path for c in changes], cwd=path)
    await git.run("commit", "--quiet", "-m", _commit_message(changes), cwd=path)
    body = pr_body(run_id, changes, _gate_pending_note(changes, cfg), fixture)
    if token is not None:
        origin = await Git().run("remote", "get-url", "origin", cwd=REPO_ROOT)
        slug = repo_slug(origin)
        await git.run(
            "push", "--quiet", f"https://github.com/{slug}.git", f"HEAD:{branch}", cwd=path
        )
        async with GitHubClient(token) as gh:
            pr = await find_pr(gh, slug, branch)
            if pr is None or pr.state != "open":
                pr = await create_pr(
                    gh, slug, head=branch, base="main",
                    title=f"Learning: {_title(changes)}", body=body,
                )  # fmt: skip
            else:
                await update_pr_body(gh, slug, pr.number, body)
        echo(f"learning: CodeIt PR {pr.url}")
        return pr.url, None
    out = cfg.data_dir / "learning" / proposal_id
    out.mkdir(parents=True, exist_ok=True)
    patch = await git.run("format-patch", "-1", "--stdout", cwd=path)
    (out / "codeit.patch").write_text(patch + "\n", encoding="utf-8")
    (out / "pr.md").write_text(body, encoding="utf-8")
    echo(
        f"learning: CodeIt change is on branch {branch} in {path} and in {out / 'codeit.patch'} "
        "(no GITHUB_TOKEN_CODEIT, so no PR)"
    )
    return None, str(out / "codeit.patch")


def _title(changes: Sequence[FileChange]) -> str:
    n = sum(len(c.rules) for c in changes)
    agents = " and ".join(TARGETS[c.target].agent for c in changes)
    return f"{n} rule(s) for the {agents} from review feedback"


def _commit_message(changes: Sequence[FileChange]) -> str:
    return f"learning: {_title(changes)}\n\n" + "\n".join(
        f"- {c.path}: {r['rule']}" for c in changes for r in c.rules
    )


def _record_proposal(
    engine: Engine,
    proposal_id: str,
    run_id: str,
    repo: str,
    branch: str,
    pr_url: str | None,
    patch_path: str | None,
    changes: Sequence[FileChange],
) -> None:
    with Session(engine) as s, s.begin():
        s.add(
            LearningProposal(
                id=proposal_id,
                run_id=run_id,
                repo=repo,
                branch=branch,
                pr_url=pr_url,
                patch_path=patch_path,
                changes_json={
                    "files": {
                        c.target: {"path": c.path, "before": c.before, "after": c.after}
                        for c in changes
                    },
                    "rules": {c.target: c.rules for c in changes},
                    "skipped": {c.target: c.skipped for c in changes},
                },
                gate_status="pending",
                created_at=datetime.now(UTC),
            )
        )
        for c in changes:
            if c.addressed:
                s.execute(
                    update(Signal)
                    .where(Signal.id.in_(c.addressed))
                    .values(
                        status="addressed", proposal_id=proposal_id, used_in_learning_run=run_id
                    )
                )
            if c.skipped_signals:
                s.execute(
                    update(Signal)
                    .where(Signal.id.in_(c.skipped_signals))
                    .values(status="skipped", used_in_learning_run=run_id)
                )


# the gate on the PR ------------------------------------------------------------------------


async def report_gate(cfg: Config, secrets: Secrets, p: LearningProposal, echo: Echo) -> None:
    """Put a finished gate's table on the proposal's PR (and label a regression)."""
    state = p.gate_json or {}
    text = state.get("table", "")
    if p.gate_status == "pending":
        waiting = [
            gates.SUITE_NAME[t] for t in p.changes_json.get("files", {}) if t not in state["parts"]
        ]
        text = (
            f"_Partial: {', '.join(waiting)} still to run (it needs Claude's run window)._\n\n"
            + gates.partial_table(state, cfg.eval.regression_tolerance)
        )
    elif p.gate_status == "regression":
        text = "**Regression:** at least one metric got worse beyond the tolerance.\n\n" + text
    else:
        text = "No metric got worse beyond the tolerance.\n\n" + text
    if p.pr_url:
        m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", p.pr_url)
        assert m
        slug, number = m.group(1), int(m.group(2))
        token = secrets.github_token_agent if p.repo == "target" else secrets.github_token_codeit
        if token is None:
            echo(f"learning: no token to update {p.pr_url}")
            return
        async with GitHubClient(token.get_secret_value()) as gh:
            pr = await get_pr(gh, slug, number)
            raw = await gh.get_json(f"/repos/{slug}/pulls/{pr.number}")
            await update_pr_body(gh, slug, number, with_gate(str(raw.get("body") or ""), text))
            if p.gate_status == "regression":
                await add_pr_labels(gh, slug, number, [REGRESSION_LABEL])
        echo(f"learning: gate {p.gate_status} on {p.pr_url}")
    elif p.patch_path:
        pr_md = Path(p.patch_path).with_name("pr.md")
        pr_md.write_text(with_gate(pr_md.read_text(encoding="utf-8"), text), encoding="utf-8")
        echo(f"learning: gate {p.gate_status}, written to {pr_md}")


def pending_proposals(engine: Engine) -> list[str]:
    with Session(engine) as s:
        return list(
            s.scalars(
                select(LearningProposal.id)
                .where(LearningProposal.gate_status == "pending")
                .order_by(LearningProposal.created_at)
            )
        )


async def run_pending_gates(deps: gates.GateDeps) -> dict[str, str]:
    """Run what is left of every pending gate, and report each on its proposal."""
    out = {}
    for pid in pending_proposals(deps.engine):
        p = await gates.run_gate(deps, pid, deps.cfg.data_dir / "learning" / pid)
        out[pid] = p.gate_status
        if p.gate_status in ("passed", "regression") or (p.gate_json or {}).get("parts"):
            await report_gate(deps.cfg, deps.secrets, p, deps.echo)
    return out


# the run -----------------------------------------------------------------------------------


async def run_learning(
    cfg: Config,
    secrets: Secrets,
    ids: JiraIds,
    key: str = LEASE_KEY,
    *,
    instance: str = "learning-1",
    run_id: str | None = None,
    sandbox: Sandbox | None = None,
    echo: Echo = print,
    backends: Sequence[ChatBackend] | None = None,
    signals_file: Path | None = None,
    gate: bool = True,
    any_time: bool = False,
    today: date | None = None,
    gate_deps: gates.GateDeps | None = None,
) -> LearningRun:
    """`key` is ignored (the scheduler passes one); the run leases `LEARNING`."""
    day = today or datetime.now().astimezone().date()
    run_id = run_id or str(ULID())
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    leases = LeaseStore(engine)
    if not leases.acquire(LEASE_KEY, ROLE, instance, run_id, LEASE_TTL):
        raise LearningError("another learning run is going")
    record_run(
        engine, run_id, ROLE, instance=instance, ticket_key=None, prompt_hash=prompt_hash(ROLE)
    )
    result = LearningRun(run_id)
    calls: list[ModelCall] = []
    try:
        async with leases.keep_alive(LEASE_KEY, run_id, LEASE_TTL):
            chat = list(backends or chat_route(cfg, secrets, ROLE))
            scope: list[int] | None = None
            if signals_file is not None:
                scope = _load_fixture(engine, signals_file)
                result.new_signals = len(scope)
                echo(f"learning: {len(scope)} signal(s) from {signals_file}")
            else:
                async with JiraClient.from_secrets(secrets) as jira:
                    result.new_signals = await collect(cfg, secrets, ids, engine, jira)
                echo(f"learning: {result.new_signals} new signal(s)")
            try:
                result.classified, more = await lessons_mod.classify(engine, chat, scope)
                calls += more
                chosen = lessons_mod.qualifying(lessons_mod.open_lessons(engine, scope))
            except BackendUnavailable as e:
                raise LearningError(f"no model could classify the signals: {e}") from e
            result.lessons = [f"{lesson.target}:{lesson.key}" for lesson in chosen]
            echo(
                f"learning: {result.classified} classified; lessons that qualify: "
                + (", ".join(result.lessons) or "none")
            )
            if chosen:
                result.proposals = await _propose(
                    cfg, secrets, engine, chat, chosen, run_id, day, calls, echo,
                    fixture=str(signals_file) if signals_file else None,
                )  # fmt: skip
            if scope is not None:  # a fixture's leftovers do not linger into real runs
                with Session(engine) as s, s.begin():
                    s.execute(
                        update(Signal)
                        .where(Signal.id.in_(scope), Signal.status.in_(("new", "classified")))
                        .values(status="ignored")
                    )
            if gate:
                deps = gate_deps or gates.GateDeps(
                    cfg, secrets, engine, sandbox or Sandbox(), echo, any_time=any_time
                )
                result.gates = await run_pending_gates(deps)
        for call in calls:
            if call.backend != "claude_code_chat":
                record_spend(
                    engine, backend=call.backend, role=ROLE, usd=call.cost_usd,
                    requests=call.calls, note=f"learning {run_id}",
                )  # fmt: skip
        finish_run(
            engine,
            run_id,
            ROLE,
            {
                "status": "proposed" if result.proposals else "no_change",
                "backend": calls[0].backend if calls else None,
                "model": calls[0].model if calls else None,
                "turns": sum(c.calls for c in calls),
                "cost_usd": round(sum(c.cost_usd or 0.0 for c in calls), 4),
                "result_json": result.__dict__,
            },
        )
        return result
    except Exception as e:
        finish_run(engine, run_id, ROLE, {"status": "error", "error": str(e)[:2000]})
        raise
    finally:
        leases.release(LEASE_KEY, run_id)


async def _propose(
    cfg: Config,
    secrets: Secrets,
    engine: Engine,
    chat: Sequence[ChatBackend],
    chosen: Sequence[Lesson],
    run_id: str,
    day: date,
    calls: list[ModelCall],
    echo: Echo,
    fixture: str | None = None,
) -> list[str]:
    branch = f"learning/{day.isoformat()}-{run_id[-6:].lower()}"
    by_target = lessons_mod.group(chosen)
    target_changes: list[FileChange] = []
    codeit_changes: list[FileChange] = []
    for target, lessons in sorted(by_target.items()):
        spec = TARGETS[target]
        current = await _current_text(cfg, secrets, spec.repo, spec.path)
        change = await _change_for(chat, target, current, lessons, calls)
        if not change.rules:
            echo(f"learning: nothing to add for the {spec.agent}")
            _record_skips(engine, run_id, change)
            continue
        (target_changes if spec.repo == "target" else codeit_changes).append(change)
    out = []
    for repo, changes in (("target", target_changes), ("codeit", codeit_changes)):
        if not changes:
            continue
        proposal_id = str(ULID())
        if repo == "target":
            pr_url, patch = await _open_target(cfg, secrets, branch, run_id, changes, echo, fixture)
        else:
            pr_url, patch = await _open_codeit(
                cfg, secrets, branch, run_id, proposal_id, changes, echo, fixture
            )
        _record_proposal(engine, proposal_id, run_id, repo, branch, pr_url, patch, changes)
        out.append(pr_url or patch or "")
    return out


def _record_skips(engine: Engine, run_id: str, change: FileChange) -> None:
    if change.skipped_signals:
        with Session(engine) as s, s.begin():
            s.execute(
                update(Signal)
                .where(Signal.id.in_(change.skipped_signals))
                .values(status="skipped", used_in_learning_run=run_id)
            )


async def _current_text(cfg: Config, secrets: Secrets, repo: str, path: str) -> str:
    """The steering file as it is on the target's default branch, or in this checkout."""
    if repo == "codeit":
        return (REPO_ROOT / path).read_text(encoding="utf-8")
    token = secrets.github_token_readonly or secrets.github_token_agent
    target = cfg.project.target_repo
    git = Git(token.get_secret_value() if token else None)
    clones = CloneManager(cfg.data_dir, target.name, target.url, target.default_branch, git)
    mirror = await clones.refresh_mirror()
    return await git.run("show", f"origin/{target.default_branch}:{path}", cwd=mirror)
