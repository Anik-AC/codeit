"""`codeit run coder`: one Coder run end to end (PRD 11.2).

1. Claim: lease, check `Ready for Dev`, move to `In Dev`, set Agent and Run ID, comment.
2. Context: the ticket, feedback since the previous run (Jira and PR), the existing PR.
3. Workspace: the ticket's clone on its branch, rebased onto main on rework.
4. Run: a worker container with a coder run token for jira-mcp; Claude Code inside.
5. Result: verify the PR on GitHub, then Agent Review, Human Review (+ needs-human) or
   back to Ready for Dev (usage limit), re-reading the ticket first (PRD 12.5).

Local mode (`--file`) skips Jira, GitHub and jira-mcp: the agent commits in the clone.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from ulid import ULID

from codeit import db
from codeit.agents.coder import (
    MCP_TOOLS,
    NEEDS_HUMAN,
    ROLE,
    TOOLS,
    Outcome,
    build_prompts,
    decide,
    feedback_items,
    orchestrator_comment,
    parse_result,
    ticket_markdown,
)
from codeit.backends.base import AgenticRequest, AgenticResult
from codeit.backends.claude_code_agent import ClaudeCodeAgent
from codeit.backends.state import BackendStateStore
from codeit.config import Config, Secrets
from codeit.git import Git
from codeit.github_client import GitHubClient, PullRequest, repo_slug
from codeit.github_client.prs import branch_head, find_pr, get_pr, pr_feedback
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.comments import add_comment, list_comments
from codeit.jira_client.issues import add_labels, add_remote_link, get_ticket, update_fields
from codeit.jira_client.transitions import TransitionCache, transition_to
from codeit.log import bind_run, clear_run, get_logger
from codeit.model_env import claude_model
from codeit.orchestrator.leases import LeaseStore
from codeit.prompts import prompt_hash
from codeit.run_tokens import RunTokenStore, token_ttl
from codeit.runs import finish_run, previous_run_start, record_run
from codeit.sandbox.clone import CloneManager, Prepared, branch_name
from codeit.sandbox.containers import ContainerSpec, Sandbox
from codeit.sandbox.runner import role_env, write_mcp_config
from mcp_servers.jira.app import running_http_server
from mcp_servers.jira.server import JiraContext

log = get_logger(__name__)

READY, IN_DEV = "Ready for Dev", "In Dev"
AGENT_REVIEW, HUMAN_REVIEW = "Agent Review", "Human Review"
HEARTBEAT_S = 60

Echo = Callable[[str], None]


class CoderError(Exception):
    pass


def outcome_fields(outcome: Outcome) -> dict[str, Any]:
    return {
        "status": outcome.status,
        "result_json": {
            "action": outcome.action,
            "pr_url": outcome.pr_url,
            "comment": outcome.comment,
        },
    }


@dataclass(frozen=True)
class CoderRun:
    run_id: str
    key: str
    branch: str
    workspace: Path
    agent: AgenticResult | None
    outcome: Outcome


# pieces -----------------------------------------------------------------------------------------


def _pr_number(url: str) -> int | None:
    m = re.search(r"/pull/(\d+)", url)
    return int(m.group(1)) if m else None


async def _existing_pr(
    gh: GitHubClient, slug: str, pr_url: str | None, branch: str
) -> PullRequest | None:
    number = _pr_number(pr_url) if pr_url else None
    if number is not None:
        return await get_pr(gh, slug, number)
    return await find_pr(gh, slug, branch)


@contextlib.asynccontextmanager
async def _heartbeat(
    leases: LeaseStore, key: str, run_id: str, ttl: timedelta
) -> AsyncIterator[None]:
    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            leases.heartbeat(key, run_id, ttl)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _run_agent(
    cfg: Config,
    secrets: Secrets,
    sandbox: Sandbox,
    engine: Engine,
    *,
    run_id: str,
    prepared: Prepared,
    system: str,
    task: str,
    mcp_config: str | None,
) -> AgenticResult:
    run_dir = cfg.data_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = ContainerSpec.for_role(
        cfg,
        run_id=run_id,
        role=ROLE,
        workspace=prepared.path,
        run_dir=run_dir,
        env=role_env(ROLE, secrets),
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
                prompt=task,
                run_id=run_id,
                container_id=str(container.id),
                system_append=system,
                max_turns=cfg.claude.max_turns.get(ROLE, 80),
                tools=TOOLS,
                allowed_tools=MCP_TOOLS if mcp_config else [],
                mcp_config_path=mcp_config,
                timeout_s=cfg.sandbox.timeouts_minutes.get(ROLE, 60) * 60,
                model=claude_model(),
            )
        )
    finally:
        sandbox.stop(container, cfg.data_dir / "logs" / "containers" / f"{run_id}.log")


async def apply_outcome(
    jira: JiraClient, ids: JiraIds, key: str, outcome: Outcome, cache: TransitionCache
) -> bool:
    """Apply the result to Jira. Returns False, changing nothing, if a human moved the
    ticket out of In Dev during the run (PRD 12.5)."""
    current = (await get_ticket(jira, key, ids.fields)).status
    if current != IN_DEV:
        log.warning("coder.status_changed_during_run", key=key, status=current)
        return False
    if outcome.action == "agent_review" and outcome.pr_url:
        await update_fields(jira, key, ids.fields.ids(pr_url=outcome.pr_url))
        await add_remote_link(
            jira, key, outcome.pr_url, f"Pull request {outcome.pr_url.rsplit('/', 1)[-1]}"
        )
        await add_comment(jira, key, orchestrator_comment(outcome.comment))
        await transition_to(jira, key, AGENT_REVIEW, cache)
    elif outcome.action == "human_review":
        await add_comment(jira, key, orchestrator_comment(outcome.comment))
        await add_labels(jira, key, [NEEDS_HUMAN])
        await transition_to(jira, key, HUMAN_REVIEW, cache)
    elif outcome.action == "ready_for_dev":
        await add_comment(jira, key, orchestrator_comment(outcome.comment))
        await transition_to(jira, key, READY, cache)
    return True


# the run ----------------------------------------------------------------------------------------


async def run_coder(
    cfg: Config,
    secrets: Secrets,
    ids: JiraIds,
    key: str,
    *,
    instance: str = "coder-1",
    sandbox: Sandbox | None = None,
    echo: Echo = print,
) -> CoderRun:
    if secrets.github_token_agent is None:
        raise CoderError("GITHUB_TOKEN_AGENT is not set; the Coder cannot push or open PRs")
    agent_token = secrets.github_token_agent.get_secret_value()
    read_token = (
        secrets.github_token_readonly.get_secret_value()
        if secrets.github_token_readonly
        else agent_token
    )
    repo = cfg.project.target_repo
    slug = repo_slug(repo.url)
    run_id = str(ULID())
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    leases = LeaseStore(engine)
    lease_ttl = timedelta(minutes=cfg.orchestrator.lease_ttl_minutes.get(ROLE, 90))
    if not leases.acquire(key, ROLE, instance, run_id, lease_ttl):
        holder = leases.holder(key)
        raise CoderError(f"{key} is leased by {holder.instance if holder else 'another run'}")
    bind_run(run_id, key)
    tokens = RunTokenStore(engine)
    claimed = False
    outcome: Outcome | None = None
    agent: AgenticResult | None = None
    try:
        async with JiraClient.from_secrets(secrets) as jira, GitHubClient(read_token) as gh:
            ticket = await get_ticket(jira, key, ids.fields)
            if ticket.status != READY:
                raise CoderError(f"{key} is in {ticket.status!r}, not {READY!r}")
            record_run(
                engine,
                run_id,
                ROLE,
                instance=instance,
                ticket_key=key,
                prompt_hash=prompt_hash(ROLE),
            )
            previous = previous_run_start(engine, ROLE, key, run_id)
            cache = TransitionCache(ids.transitions)
            await transition_to(jira, key, IN_DEV, cache)
            claimed = True
            await update_fields(jira, key, ids.fields.ids(agent=instance, run_id=run_id))
            await add_comment(
                jira, key, orchestrator_comment(f"Picked up by {instance} (run {run_id}).")
            )
            echo(f"{key}: claimed by {instance}, run {run_id}")

            pr = await _existing_pr(gh, slug, ticket.pr_url, branch_name(key, ticket.summary))
            branch = pr.head_ref if pr else branch_name(key, ticket.summary)
            jira_comments = await list_comments(jira, key, since=previous) if previous or pr else []
            pr_comments = await pr_feedback(gh, slug, pr.number, since=previous) if pr else []
            feedback = feedback_items(jira_comments, pr_comments)
            head_before = pr.head_sha if pr else await branch_head(gh, slug, branch)

            clones = CloneManager(
                cfg.data_dir, repo.name, repo.url, repo.default_branch, Git(agent_token)
            )
            prepared = await clones.prepare(key, branch)
            echo(f"{key}: workspace {prepared.path} on {branch}" + (" (rework)" if pr else ""))
            if prepared.conflict:
                outcome = Outcome(
                    "human_review",
                    "blocked",
                    f"Rebasing `{branch}` onto main conflicts. Resolve it, or wait for the "
                    "rebase agent (M10). Labelled `needs-human`.",
                )
            else:
                system, task = build_prompts(
                    key=key,
                    ticket_md=ticket_markdown(ticket),
                    branch=branch,
                    pr_url=pr.url if pr else None,
                    feedback=feedback,
                )
                token = tokens.mint(ROLE, run_id, key, token_ttl(cfg, ROLE))
                mcp_config = write_mcp_config(cfg.data_dir / "runs" / run_id, cfg, token)
                jira_ctx = JiraContext(jira, ids, cfg.project.jira_project_key)
                async with (
                    running_http_server(jira_ctx, cfg, tokens),
                    _heartbeat(leases, key, run_id, lease_ttl),
                ):
                    limit = cfg.sandbox.timeouts_minutes.get(ROLE, 60)
                    echo(f"{key}: agent running (limit {limit} min)")
                    agent = await _run_agent(
                        cfg,
                        secrets,
                        sandbox or Sandbox(),
                        engine,
                        run_id=run_id,
                        prepared=prepared,
                        system=system,
                        task=task,
                        mcp_config=mcp_config,
                    )
                result = parse_result(agent.final_message)
                pr_after = await find_pr(gh, slug, branch)
                verified = (
                    pr_after is not None
                    and pr_after.state == "open"
                    and pr_after.head_sha != head_before
                )
                outcome = decide(
                    agent,
                    result,
                    pr_url=pr_after.url if pr_after else None,
                    pr_verified=verified,
                    transcript_tail=agent.final_message[-1500:],
                )
            applied = await apply_outcome(jira, ids, key, outcome, cache)
            echo(
                f"{key}: {outcome.status}"
                + ("" if applied else " (ticket moved by a human; left as is)")
            )
            return CoderRun(run_id, key, branch, prepared.path, agent, outcome)
    except Exception as e:
        if outcome is None:
            outcome = Outcome("human_review", "failed", f"CodeIt error during the run: {e}")
            if claimed:
                with contextlib.suppress(Exception):
                    async with JiraClient.from_secrets(secrets) as jira:
                        await apply_outcome(
                            jira, ids, key, outcome, TransitionCache(ids.transitions)
                        )
        raise
    finally:
        tokens.revoke_run(run_id)
        leases.release(key, run_id)
        if outcome is not None:
            finish_run(engine, run_id, ROLE, outcome_fields(outcome), agent)
        clear_run()


async def run_coder_local(
    cfg: Config,
    secrets: Secrets,
    ticket_file: Path,
    key: str,
    *,
    sandbox: Sandbox | None = None,
    echo: Echo = print,
) -> CoderRun:
    """Local mode (PRD 25.1): a ticket from a markdown file; the agent commits in the clone,
    nothing is pushed and Jira is not touched."""
    repo = cfg.project.target_repo
    run_id = str(ULID())
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    ticket_md = await asyncio.to_thread(ticket_file.read_text, encoding="utf-8")
    title = next((ln.lstrip("# ").strip() for ln in ticket_md.splitlines() if ln.strip()), key)
    branch = branch_name(key, title)
    token = secrets.github_token_readonly or secrets.github_token_agent
    clones = CloneManager(
        cfg.data_dir,
        repo.name,
        repo.url,
        repo.default_branch,
        Git(token.get_secret_value() if token else None),
    )
    prepared = await clones.prepare(key, branch)
    record_run(
        engine, run_id, ROLE, instance="coder-local", ticket_key=key, prompt_hash=prompt_hash(ROLE)
    )
    system, task = build_prompts(
        key=key, ticket_md=ticket_md, branch=branch, pr_url=None, feedback=[], local=True
    )
    echo(f"{key}: local run {run_id} in {prepared.path}")
    agent = await _run_agent(
        cfg,
        secrets,
        sandbox or Sandbox(),
        engine,
        run_id=run_id,
        prepared=prepared,
        system=system,
        task=task,
        mcp_config=None,
    )
    outcome = decide(
        agent,
        parse_result(agent.final_message),
        pr_url=None,
        pr_verified=False,
        transcript_tail=agent.final_message[-1500:],
    )
    finish_run(engine, run_id, ROLE, outcome_fields(outcome), agent)
    return CoderRun(run_id, key, branch, prepared.path, agent, outcome)
