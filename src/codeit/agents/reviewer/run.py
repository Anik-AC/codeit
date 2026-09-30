"""`codeit run reviewer`: one review end to end (PRD 11.3, 6.2).

1. Claim: lease; the ticket must be in `Agent Review` with an open PR.
2. Phase 1 in a worker container on the PR head: the target's commands, then
   new_tests_fail_on_base; then CI status from GitHub.
3. Phase 2: the model's verdict (non-Anthropic, `routing.reviewer`), then the phase 1 override.
4. Post a PR review (event COMMENT) and a Jira comment, update `Review Loop`, and route:
   back to `Ready for Dev`, or to `Human Review` (with `needs-human` at the loop cap).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from sqlalchemy import Engine
from ulid import ULID

from codeit import db
from codeit.agents.coder import (
    NEEDS_HUMAN,
    REVIEWER_MARK,
    feedback_items,
    is_own_comment,
    orchestrator_comment,
    ticket_markdown,
)
from codeit.agents.reviewer.checks import (
    Phase1,
    changed_test_files,
    ci_status,
    container_runner,
    new_tests_fail_on_base,
    run_commands,
)
from codeit.agents.reviewer.verdict import (
    Routing,
    Verdict,
    apply_override,
    as_dict,
    has_criteria,
    model_verdict,
    render_jira,
    render_review,
    route,
)
from codeit.backends.base import BackendUnavailable, ChatBackend
from codeit.backends.registry import chat_route
from codeit.backends.structured import ModelCall
from codeit.config import Config, Secrets
from codeit.git import Git
from codeit.github_client import GitHubClient, PullRequest, repo_slug
from codeit.github_client.prs import check_runs, find_pr, get_pr, post_review
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.comments import add_comment, list_comments
from codeit.jira_client.issues import add_labels, get_ticket, update_fields
from codeit.jira_client.transitions import TransitionCache, transition_to
from codeit.log import bind_run, clear_run, get_logger
from codeit.orchestrator.budget import record_spend
from codeit.orchestrator.leases import LeaseStore
from codeit.prompts import prompt_hash
from codeit.runs import finish_run, previous_run_start, record_run
from codeit.sandbox.clone import CloneManager, branch_name
from codeit.sandbox.containers import ContainerSpec, Sandbox
from codeit.sandbox.runner import role_env
from codeit.target import load_target_config

log = get_logger(__name__)

ROLE: Final = "reviewer"
AGENT_REVIEW, READY, HUMAN_REVIEW = "Agent Review", "Ready for Dev", "Human Review"
LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock")

Echo = Callable[[str], None]


class ReviewerError(Exception):
    pass


@dataclass(frozen=True)
class ReviewRun:
    run_id: str
    key: str
    verdict: Verdict | None
    phase1: Phase1
    routing: Routing
    review_url: str
    applied: bool


def _pr_number(url: str | None) -> int | None:
    m = re.search(r"/pull/(\d+)", url or "")
    return int(m.group(1)) if m else None


async def _escalate(jira: JiraClient, key: str, cache: TransitionCache, message: str) -> None:
    await add_comment(jira, key, orchestrator_comment(message))
    await add_labels(jira, key, [NEEDS_HUMAN])
    await transition_to(jira, key, HUMAN_REVIEW, cache)


async def run_reviewer(
    cfg: Config,
    secrets: Secrets,
    ids: JiraIds,
    key: str,
    *,
    instance: str = "reviewer-1",
    run_id: str | None = None,
    sandbox: Sandbox | None = None,
    backends: Sequence[ChatBackend] | None = None,
    ci_wait_s: float = 600,
    echo: Echo = print,
) -> ReviewRun:
    if secrets.github_token_agent is None:
        raise ReviewerError("GITHUB_TOKEN_AGENT is not set; the Reviewer posts PR reviews with it")
    write_token = secrets.github_token_agent.get_secret_value()
    read_token = (
        secrets.github_token_readonly.get_secret_value()
        if secrets.github_token_readonly
        else write_token
    )
    repo = cfg.project.target_repo
    slug = repo_slug(repo.url)
    run_id = run_id or str(ULID())
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    leases = LeaseStore(engine)
    ttl = timedelta(minutes=cfg.orchestrator.lease_ttl_minutes.get(ROLE, 40))
    if not leases.acquire(key, ROLE, instance, run_id, ttl):
        holder = leases.holder(key)
        raise ReviewerError(f"{key} is leased by {holder.instance if holder else 'another run'}")
    bind_run(run_id, key)
    started = False
    try:
        async with (
            leases.keep_alive(key, run_id, ttl),
            JiraClient.from_secrets(secrets) as jira,
            GitHubClient(read_token) as gh,
            GitHubClient(write_token) as gh_write,
        ):
            ticket = await get_ticket(jira, key, ids.fields)
            if ticket.status != AGENT_REVIEW:
                raise ReviewerError(f"{key} is in {ticket.status!r}, not {AGENT_REVIEW!r}")
            record_run(
                engine,
                run_id,
                ROLE,
                instance=instance,
                ticket_key=key,
                prompt_hash=prompt_hash(ROLE),
            )
            started = True
            cache = TransitionCache(ids.transitions)
            previous = previous_run_start(engine, ROLE, key, run_id)
            await update_fields(jira, key, ids.fields.ids(agent=instance, run_id=run_id))
            await add_comment(
                jira, key, orchestrator_comment(f"Picked up by {instance} (run {run_id}).")
            )
            echo(f"{key}: review {run_id} by {instance}")

            number = _pr_number(ticket.pr_url)
            pr: PullRequest | None
            if number:
                pr = await get_pr(gh, slug, number)
            else:
                # No PR URL on the ticket (e.g. a crashed Coder run): look by its branch.
                pr = await find_pr(gh, slug, branch_name(key, ticket.summary))
                if pr is not None:
                    await update_fields(jira, key, ids.fields.ids(pr_url=pr.url))
            if pr is not None and pr.merged:
                # A human merged it before the review: nothing to review; the merge
                # watcher moves it on to Done.
                routing = Routing("human_review", ticket.review_loop, False, "already merged")
                await add_comment(
                    jira, key, orchestrator_comment(f"{pr.url} is already merged; no review.")
                )
                await transition_to(jira, key, HUMAN_REVIEW, cache)
                finish_run(
                    engine, run_id, ROLE, {"status": "skipped", "result_json": {"merged": True}}
                )
                echo(f"{key}: PR already merged -> human_review")
                return ReviewRun(run_id, key, None, Phase1(), routing, "", True)
            if pr is None or pr.state != "open":
                why = "no pull request" if pr is None else f"the pull request is {pr.state}"
                routing = Routing("human_review", ticket.review_loop, True, why)
                await _escalate(jira, key, cache, f"Cannot review: {why}. Labelled `needs-human`.")
                echo(f"{key}: cannot review ({why}) -> human_review")
                finish_run(
                    engine, run_id, ROLE, {"status": "incomplete", "result_json": {"reason": why}}
                )
                return ReviewRun(run_id, key, None, Phase1(), routing, "", True)

            clones = CloneManager(
                cfg.data_dir, repo.name, repo.url, repo.default_branch, Git(read_token)
            )
            path = await clones.prepare_review(key, pr.head_sha)
            base = await clones.git.run("merge-base", "HEAD", f"origin/{pr.base_ref}", cwd=path)
            changed, diff = await workspace_diff(clones.git, path, base)
            echo(f"{key}: PR #{pr.number} at {pr.head_sha[:8]}, {len(changed)} files changed")
            phase1 = await run_phase1(
                cfg,
                secrets,
                sandbox or Sandbox(),
                run_id=run_id,
                path=path,
                head=pr.head_sha,
                base=base,
                changed=changed,
            )

            async def fetch_ci() -> list[tuple[str, str, str | None]]:
                return [
                    (r.name, r.status, r.conclusion)
                    for r in await check_runs(gh, slug, pr.head_sha)
                ]

            phase1.checks.append(await ci_status(fetch_ci, wait_s=ci_wait_s))
            echo(f"{key}: checks " + ", ".join(f"{c.name}={c.status}" for c in phase1.checks))

            jira_comments = await list_comments(jira, key, since=previous) if previous else []
            suggestions = [
                s
                for s in feedback_items(
                    [
                        c
                        for c in jira_comments
                        if REVIEWER_MARK not in c.body_md and not is_own_comment(c.body_md)
                    ],
                    [],
                )
            ]
            verdict, call = await judge(
                backends or chat_route(cfg, secrets, ROLE),
                engine,
                run_id=run_id,
                key=key,
                ticket_md=ticket_markdown(ticket),
                diff=diff,
                phase1=phase1,
                suggestions=suggestions,
                echo=echo,
            )
            routing = route(verdict, ticket.review_loop, cfg.orchestrator.max_review_loops)

            review_url = await post_review(
                gh_write, slug, pr.number, render_review(verdict, phase1, run_id=run_id, call=call)
            )
            await add_comment(
                jira, key, render_jira(verdict, phase1, routing, run_id=run_id, pr_url=pr.url)
            )
            applied = await _apply_routing(jira, ids, key, routing, ticket.review_loop, cache)
            name = verdict.verdict if verdict else "incomplete"
            echo(
                f"{key}: {name} -> {routing.route} ({routing.reason})"
                + ("" if applied else "; ticket moved by a human, left as is")
            )
            finish_run(
                engine,
                run_id,
                ROLE,
                {
                    "status": name,
                    "backend": call.backend if call else None,
                    "model": call.model if call else None,
                    "turns": call.calls if call else None,
                    "cost_usd": call.cost_usd if call else None,
                    "result_json": {
                        "verdict": as_dict(verdict),
                        "route": routing.route,
                        "review_loop": routing.review_loop,
                        "review_url": review_url,
                        "checks": {c.name: c.status for c in phase1.checks},
                    },
                },
            )
            return ReviewRun(run_id, key, verdict, phase1, routing, review_url, applied)
    except Exception as e:
        if started:
            finish_run(engine, run_id, ROLE, {"status": "error", "error": str(e)[:2000]})
        raise
    finally:
        leases.release(key, run_id)
        clear_run()


async def workspace_diff(git: Git, path: Path, base: str) -> tuple[list[str], str]:
    """Files added or modified since `base`, and the diff without lockfiles."""
    changed = (
        await git.run("diff", "--name-only", "--diff-filter=AM", f"{base}..HEAD", cwd=path)
    ).split()
    excludes = [f":(exclude){f}" for f in LOCKFILES]
    diff = await git.run("diff", f"{base}..HEAD", "--", ".", *excludes, cwd=path)
    return changed, diff


async def run_phase1(
    cfg: Config,
    secrets: Secrets,
    sandbox: Sandbox,
    *,
    run_id: str,
    path: Path,
    head: str,
    base: str,
    changed: Sequence[str],
) -> Phase1:
    """The deterministic checks in a worker container (PRD 11.3 phase 1), without CI."""
    target = load_target_config(path)
    phase1 = Phase1(test_files=changed_test_files(changed, target))
    env = {**role_env(ROLE, secrets), "CI": "1"}
    spec = ContainerSpec.for_role(
        cfg,
        run_id=run_id,
        role=ROLE,
        workspace=path,
        run_dir=cfg.data_dir / "runs" / run_id,
        env=env,
    )
    per_check = cfg.sandbox.timeouts_minutes.get(ROLE, 30) * 60
    container = sandbox.start(spec)
    try:
        runner = container_runner(sandbox, str(container.id))
        phase1.checks += await run_commands(target, runner, per_check)
        if phase1.checks[0].status == "pass":
            phase1.checks.append(
                await new_tests_fail_on_base(
                    target,
                    runner,
                    test_files=phase1.test_files,
                    head=head,
                    base=base,
                    timeout_s=per_check,
                )
            )
    finally:
        sandbox.stop(container, cfg.data_dir / "logs" / "containers" / f"{run_id}.log")
    return phase1


async def judge(
    backends: Sequence[ChatBackend],
    engine: Engine,
    *,
    run_id: str,
    key: str,
    ticket_md: str,
    diff: str,
    phase1: Phase1,
    suggestions: Sequence[str] = (),
    echo: Echo = print,
    spend_role: str = ROLE,
) -> tuple[Verdict | None, ModelCall | None]:
    """Phase 2: the model's verdict, its spend recorded, then the phase 1 override."""
    verdict: Verdict | None = None
    call: ModelCall | None = None
    try:
        verdict, call = await model_verdict(
            backends,
            key=key,
            ticket_md=ticket_md,
            diff=diff,
            phase1=phase1,
            suggestions=list(suggestions),
        )
    except BackendUnavailable as e:
        log.warning("reviewer.model_unavailable", reason=str(e))
        echo(f"{key}: model review unavailable ({e})")
    if call is not None:
        record_spend(
            engine,
            backend=call.backend,
            role=spend_role,
            usd=call.cost_usd,
            requests=call.calls,
            note=f"review {run_id}",
        )
    return apply_override(verdict, phase1, has_criteria=has_criteria(ticket_md)), call


async def _apply_routing(
    jira: JiraClient,
    ids: JiraIds,
    key: str,
    routing: Routing,
    old_loop: int,
    cache: TransitionCache,
) -> bool:
    """Returns False, changing nothing, if a human moved the ticket during the review."""
    current = (await get_ticket(jira, key, ids.fields)).status
    if current != AGENT_REVIEW:
        log.warning("reviewer.status_changed_during_run", key=key, status=current)
        return False
    if routing.review_loop != old_loop:
        await update_fields(jira, key, ids.fields.ids(review_loop=routing.review_loop))
    if routing.needs_human:
        await add_labels(jira, key, [NEEDS_HUMAN])
    await transition_to(
        jira, key, READY if routing.route == "ready_for_dev" else HUMAN_REVIEW, cache
    )
    return True
