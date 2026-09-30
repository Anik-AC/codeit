"""The Rebase agent (PRD 11.5, ADR-0017): keep agent PRs applying cleanly to main.

For one ticket's open PR:

1. Lease the ticket (role `rebase`), so no Coder or Reviewer claims it meanwhile.
2. Check out the PR branch in its own clone and `git rebase origin/<base>` on the host.
3. Clean rebase: no model is needed.
4. Conflicts:
   - Escalate if there are more than `rebase.max_files` of them, or any touches a
     `never_auto` path (config plus the target's `never_auto_rebase`).
   - Otherwise, if Claude may run now, the agent resolves them in a worker container. If it
     may not (run window, parking, concurrency), the PR waits for a later poll, untouched.
5. CodeIt itself runs install, typecheck, unit and e2e in a container; only green results
   are pushed, with `--force-with-lease` against the head it started from.
6. Jira gets comments only (and `needs-human` on escalation); the status never changes.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine
from ulid import ULID

from codeit import db
from codeit.agents.coder import NEEDS_HUMAN, orchestrator_comment, ticket_markdown
from codeit.agents.reviewer.checks import CheckResult, container_runner, run_commands
from codeit.backends.base import AgenticRequest, AgenticResult
from codeit.backends.claude_code_agent import ClaudeCodeAgent
from codeit.backends.state import BackendStateStore
from codeit.config import Config, Secrets
from codeit.git import Git, GitError
from codeit.github_client import GitHubClient, PullRequest, repo_slug
from codeit.github_client.prs import find_pr, get_pr, open_pr_numbers
from codeit.jira_client import JiraClient, JiraIds, JiraNotFound
from codeit.jira_client.comments import add_comment
from codeit.jira_client.issues import add_labels, get_ticket
from codeit.log import bind_run, clear_run, get_logger
from codeit.model_env import claude_model
from codeit.orchestrator.budget import CLAUDE_ROLES, Budget, Decision
from codeit.orchestrator.leases import LeaseStore
from codeit.prompts import prompt_hash, render
from codeit.runs import finish_run, record_run
from codeit.sandbox.clone import CloneManager, branch_name
from codeit.sandbox.containers import ContainerSpec, Sandbox
from codeit.sandbox.runner import role_env
from codeit.target import load_target_config

log = get_logger(__name__)

ROLE: Final = "rebase"
IN_DEV, HUMAN_REVIEW = "In Dev", "Human Review"
TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]
GATES = ("install", "typecheck", "unit", "e2e")
MAIN_CHANGES = 20
KEY_IN_BRANCH = re.compile(r"^([A-Z][A-Z0-9]+-\d+)-")

Echo = Callable[[str], None]
ClaudeCheck = Callable[[], Decision]
Action = Literal["rebased", "resolved", "up_to_date", "escalated", "waiting", "refused"]


class RebaseError(Exception):
    pass


class RebaseResult(BaseModel):
    status: Literal["resolved", "blocked", "failed"]
    notes: str = ""


@dataclass
class RebaseRun:
    run_id: str
    key: str
    action: Action
    reason: str = ""
    conflicts: list[str] = field(default_factory=list)
    new_head: str | None = None
    agent: AgenticResult | None = None


def key_of_branch(branch: str) -> str | None:
    m = KEY_IN_BRANCH.match(branch)
    return m.group(1) if m else None


def blocked_paths(conflicts: Sequence[str], never_auto: Sequence[str]) -> list[str]:
    return [f for f in conflicts if any(fnmatch.fnmatch(f, g) for g in never_auto)]


def escalation_reason(conflicts: Sequence[str], max_files: int, never_auto: Sequence[str]) -> str:
    """Why these conflicts must go to a human, or "" if the agent may try (PRD 11.5)."""
    if len(conflicts) > max_files:
        return f"{len(conflicts)} conflicted files, more than the limit of {max_files}"
    blocked = blocked_paths(conflicts, never_auto)
    if blocked:
        return "conflicts in paths CodeIt never resolves by itself: " + ", ".join(blocked)
    return ""


def parse_result(text: str) -> RebaseResult | None:
    for line in reversed(text.strip().splitlines()):
        m = re.search(r"RESULT:\s*(\{.*\})", line)
        if m:
            try:
                return RebaseResult.model_validate(json.loads(m.group(1)))
            except (json.JSONDecodeError, ValidationError):
                return None
    return None


def default_claude_check(cfg: Config, engine: Engine, own_key: str) -> ClaudeCheck:
    """May Claude run now? Same rules as the scheduler; this run's own lease not counted."""
    budget = Budget(cfg, engine, has_openrouter_key=True)
    leases = LeaseStore(engine)

    def check() -> Decision:
        others = sum(
            1
            for lease in leases.all()
            if lease.role in CLAUDE_ROLES and lease.ticket_key != own_key
        )
        return budget.can_run("rebase", claude_running=others)

    return check


async def _in_progress(git: Git, path: Path) -> bool:
    """A rebase is still going while git keeps its state directory (REBASE_HEAD can outlive
    a finished rebase, so it is not the signal)."""
    for name in ("rebase-merge", "rebase-apply"):
        state = await git.run("rev-parse", "--git-path", name, cwd=path)
        if (path / state).exists():
            return True
    return False


async def _conflicts(git: Git, path: Path) -> list[str]:
    out = await git.run("diff", "--name-only", "--diff-filter=U", cwd=path, check=False)
    return [f for f in out.splitlines() if f]


async def run_rebase(
    cfg: Config,
    secrets: Secrets,
    ids: JiraIds,
    key: str,
    *,
    instance: str = "rebase-1",
    run_id: str | None = None,
    sandbox: Sandbox | None = None,
    echo: Echo = print,
    claude_check: ClaudeCheck | None = None,
) -> RebaseRun:
    if secrets.github_token_agent is None:
        raise RebaseError("GITHUB_TOKEN_AGENT is not set; the Rebase agent pushes with it")
    token = secrets.github_token_agent.get_secret_value()
    repo = cfg.project.target_repo
    slug = repo_slug(repo.url)
    run_id = run_id or str(ULID())
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    leases = LeaseStore(engine)
    ttl = timedelta(minutes=cfg.orchestrator.lease_ttl_minutes.get(ROLE, 30))
    if not leases.acquire(key, ROLE, instance, run_id, ttl):
        holder = leases.holder(key)
        raise RebaseError(f"{key} is leased by {holder.instance if holder else 'another run'}")
    bind_run(run_id, key)
    started = False
    sandbox = sandbox or Sandbox()
    try:
        async with (
            leases.keep_alive(key, run_id, ttl),
            JiraClient.from_secrets(secrets) as jira,
            GitHubClient(token) as gh,
        ):
            ticket = await get_ticket(jira, key, ids.fields)
            if ticket.status == IN_DEV:
                raise RebaseError(f"{key} is In Dev; the Coder owns its branch now")
            number = re.search(r"/pull/(\d+)", ticket.pr_url or "")
            pr: PullRequest | None = (
                await get_pr(gh, slug, int(number.group(1)))
                if number
                else await find_pr(gh, slug, branch_name(key, ticket.summary))
            )
            if pr is None or pr.state != "open":
                raise RebaseError(f"{key} has no open pull request")
            record_run(
                engine,
                run_id,
                ROLE,
                instance=instance,
                ticket_key=key,
                prompt_hash=prompt_hash(ROLE),
            )
            started = True
            result = await _rebase(
                cfg, secrets, engine, jira, ids, sandbox, key, pr, run_id, token, echo,
                claude_check or default_claude_check(cfg, engine, key),
                ticket_md=ticket_markdown(ticket),
                human_review=ticket.status == HUMAN_REVIEW,
            )  # fmt: skip
            finish_run(
                engine,
                run_id,
                ROLE,
                {
                    "status": result.action,
                    "result_json": {
                        "action": result.action,
                        "reason": result.reason,
                        "conflicts": result.conflicts,
                        "new_head": result.new_head,
                        "pr": pr.url,
                    },
                },
                result.agent,
            )
            echo(f"{key}: {result.action}" + (f" ({result.reason})" if result.reason else ""))
            return result
    except Exception as e:
        if started:
            finish_run(engine, run_id, ROLE, {"status": "error", "error": str(e)[:2000]})
        raise
    finally:
        leases.release(key, run_id)
        clear_run()


async def _rebase(
    cfg: Config,
    secrets: Secrets,
    engine: Engine,
    jira: JiraClient,
    ids: JiraIds,
    sandbox: Sandbox,
    key: str,
    pr: PullRequest,
    run_id: str,
    token: str,
    echo: Echo,
    claude_check: ClaudeCheck,
    *,
    ticket_md: str,
    human_review: bool,
) -> RebaseRun:
    repo = cfg.project.target_repo
    git = Git(token)
    clones = CloneManager(cfg.data_dir, repo.name, repo.url, repo.default_branch, git)
    path = await clones.prepare_branch(key, pr.head_ref)
    start = await git.run("rev-parse", "HEAD", cwd=path)
    base = f"origin/{pr.base_ref}"
    base_sha = await git.run("rev-parse", base, cwd=path)
    if await git.ok("merge-base", "--is-ancestor", base, "HEAD", cwd=path):
        return RebaseRun(run_id, key, "up_to_date", "already on the latest main")
    echo(f"{key}: rebasing {pr.head_ref} ({start[:8]}) onto {base} ({base_sha[:8]})")

    clean = True
    try:
        await git.run("rebase", base, cwd=path)
    except GitError:
        clean = False
    conflicts: list[str] = []
    agent: AgenticResult | None = None
    if not clean:
        conflicts = await _conflicts(git, path)
        if not conflicts:
            await git.run("rebase", "--abort", cwd=path, check=False)
            raise RebaseError("git rebase failed without conflicts")
        target = load_target_config(path)
        never_auto = [*cfg.rebase.never_auto, *target.never_auto_rebase]
        why = escalation_reason(conflicts, cfg.rebase.max_files, never_auto)
        if why:
            await git.run("rebase", "--abort", cwd=path, check=False)
            await _escalate(jira, key, why, conflicts, pr)
            return RebaseRun(run_id, key, "escalated", why, conflicts)
        decision = claude_check()
        if not decision.ok:
            await git.run("rebase", "--abort", cwd=path, check=False)
            return RebaseRun(
                run_id, key, "waiting", f"conflicts need Claude: {decision.reason}", conflicts
            )
        echo(f"{key}: {len(conflicts)} conflicted file(s); the agent resolves them")
        main_changes = (
            await git.run(
                "log",
                f"--max-count={MAIN_CHANGES}",
                "--no-merges",
                "--format=%h %s",
                f"{start}..{base}",
                cwd=path,
            )
        ).splitlines()
        agent = await _resolve(
            cfg, secrets, engine, sandbox, path, run_id,
            prompt=render(
                "rebase/task.md", key=key, branch=pr.head_ref, base=base, conflicts=conflicts,
                ticket_md=ticket_md, main_changes=main_changes,
            ),
        )  # fmt: skip
        answer = parse_result(agent.final_message)
        problem = ""
        if await _in_progress(git, path):
            problem = "the agent did not finish the rebase"
        elif not await git.ok("merge-base", "--is-ancestor", base, "HEAD", cwd=path):
            problem = "the result is not based on the latest main"
        elif await git.ok("grep", "-q", "-E", "^(<<<<<<<|>>>>>>>) ", "HEAD", cwd=path):
            problem = "conflict markers are still in the code"
        elif answer is None or answer.status != "resolved":
            problem = f"the agent reported {answer.status if answer else 'no result'}" + (
                f": {answer.notes}" if answer and answer.notes else ""
            )
        if problem:
            await git.run("rebase", "--abort", cwd=path, check=False)
            await _escalate(jira, key, problem, conflicts, pr)
            return RebaseRun(run_id, key, "escalated", problem, conflicts, agent=agent)

    failed = await _test(cfg, sandbox, path, run_id)
    if failed:
        why = "tests fail after the rebase: " + ", ".join(c.name for c in failed)
        tails = "\n\n".join(f"`{c.name}`:\n```\n{c.log_tail[-1500:]}\n```" for c in failed)
        await _escalate(jira, key, why, conflicts, pr, details=tails)
        return RebaseRun(run_id, key, "escalated", why, conflicts, agent=agent)

    new_head = await git.run("rev-parse", "HEAD", cwd=path)
    await git.run(
        "push",
        f"--force-with-lease={pr.head_ref}:{start}",
        "origin",
        f"HEAD:{pr.head_ref}",
        cwd=path,
    )
    action: Action = "rebased" if clean else "resolved"
    how = (
        ""
        if clean
        else f" The agent resolved conflicts in {', '.join(f'`{f}`' for f in conflicts)}."
    )
    note = (
        " The diff changed since you last looked; please check it again before merging."
        if human_review
        else ""
    )
    await add_comment(
        jira,
        key,
        orchestrator_comment(
            f"Rebased {pr.url} onto main `{base_sha[:8]}`, tests green "
            f"({', '.join(GATES)}).{how}{note}"
        ),
    )
    return RebaseRun(run_id, key, action, "", conflicts, new_head, agent)


async def _escalate(
    jira: JiraClient,
    key: str,
    why: str,
    conflicts: Sequence[str],
    pr: PullRequest,
    details: str = "",
) -> None:
    files = "".join(f"\n- `{f}`" for f in conflicts)
    body = (
        f"Could not rebase {pr.url} onto main: {why}."
        + (f"\n\nConflicted files:{files}" if conflicts else "")
        + (f"\n\n{details}" if details else "")
        + "\n\nNothing was pushed. Labelled `needs-human`."
    )
    await add_comment(jira, key, orchestrator_comment(body))
    await add_labels(jira, key, [NEEDS_HUMAN])


async def _resolve(
    cfg: Config,
    secrets: Secrets,
    engine: Engine,
    sandbox: Sandbox,
    path: Path,
    run_id: str,
    *,
    prompt: str,
) -> AgenticResult:
    spec = ContainerSpec.for_role(
        cfg,
        run_id=run_id,
        role=ROLE,
        workspace=path,
        run_dir=cfg.data_dir / "runs" / run_id,
        env={**role_env(ROLE, secrets), "GIT_EDITOR": "true"},
    )
    agent = ClaudeCodeAgent(
        cfg.claude.usage_limit_patterns,
        sandbox,
        cfg.data_dir / "transcripts",
        state=BackendStateStore(engine),
    )
    container = sandbox.start(spec)
    try:
        return await agent.run_agentic(
            AgenticRequest(
                prompt=prompt,
                run_id=run_id,
                container_id=str(container.id),
                system_append=render("rebase/system.md"),
                max_turns=cfg.claude.max_turns.get(ROLE, 40),
                tools=TOOLS,
                timeout_s=cfg.sandbox.timeouts_minutes.get(ROLE, 20) * 60,
                model=claude_model(),
            )
        )
    finally:
        sandbox.stop(container, cfg.data_dir / "logs" / "containers" / f"{run_id}.log")


async def _test(cfg: Config, sandbox: Sandbox, path: Path, run_id: str) -> list[CheckResult]:
    """Install, typecheck, unit and e2e in a container without credentials."""
    spec = ContainerSpec.for_role(
        cfg,
        run_id=f"{run_id}-t",
        role=ROLE,
        workspace=path,
        run_dir=cfg.data_dir / "runs" / run_id,
        env={"CI": "1"},
    )
    per_check = cfg.sandbox.timeouts_minutes.get(ROLE, 20) * 60
    container = sandbox.start(spec)
    try:
        results = await run_commands(
            load_target_config(path), container_runner(sandbox, str(container.id)), per_check
        )
    finally:
        sandbox.stop(container, cfg.data_dir / "logs" / "containers" / f"{run_id}-t.log")
    return [c for c in results if c.name in GATES and c.status != "pass"]


async def rebase_candidates(
    gh: GitHubClient, slug: str, jira: JiraClient, ids: JiraIds
) -> list[str]:
    """Ticket keys whose open PR conflicts with main or is behind it (PRD 11.5), leaving out
    tickets In Dev (the Coder owns the branch) and those already waiting for a human."""
    keys = []
    for number, branch in await open_pr_numbers(gh, slug):
        key = key_of_branch(branch)
        if key is None:
            continue
        pr = await get_pr(gh, slug, number)
        if pr.mergeable_state not in ("dirty", "behind"):
            continue  # "unknown" means GitHub is still computing it: try on the next poll
        try:
            ticket = await get_ticket(jira, key, ids.fields)
        except JiraNotFound:
            continue
        if ticket.status == IN_DEV or NEEDS_HUMAN in ticket.labels:
            continue
        keys.append(key)
    return keys
