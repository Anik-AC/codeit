"""Merge watcher (PRD 11.4), no model involved. Every poll:

- a ticket in `Human Review` whose PR is merged moves to `Done`, with a comment naming
  the merge commit, and its clones are removed (PRD 10.2)
- a ticket that reached `Done` recently while its PR is not merged gets `state-mismatch`
  (PRD 6.2 rule 4), and nothing else happens
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import Engine

from codeit.agents.coder import orchestrator_comment
from codeit.events import record_event
from codeit.github_client import GitHubClient, GitHubError, PullRequest
from codeit.github_client.prs import get_pr
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.comments import add_comment
from codeit.jira_client.issues import add_labels, get_ticket
from codeit.jira_client.search import search_tickets
from codeit.jira_client.transitions import TransitionCache, transition_to
from codeit.log import get_logger
from codeit.sandbox.clone import CloneManager

log = get_logger(__name__)

HUMAN_REVIEW, DONE = "Human Review", "Done"
STATE_MISMATCH = "state-mismatch"
# Done tickets are checked for a while after they change, not forever.
DONE_LOOKBACK = "-2d"


@dataclass(frozen=True)
class MergeAction:
    key: str
    action: str  # done | state_mismatch
    detail: str = ""


def _pr_number(url: str | None) -> int | None:
    m = re.search(r"/pull/(\d+)", url or "")
    return int(m.group(1)) if m else None


class MergeWatcher:
    def __init__(
        self,
        engine: Engine,
        jira: JiraClient,
        gh: GitHubClient,
        ids: JiraIds,
        project_key: str,
        repo_slug: str,
        clones: CloneManager | None = None,
    ) -> None:
        self.engine = engine
        self.jira = jira
        self.gh = gh
        self.ids = ids
        self.project_key = project_key
        self.slug = repo_slug
        self.clones = clones
        self._cache = TransitionCache(ids.transitions)

    async def tick(self) -> list[MergeAction]:
        actions = []
        p = self.project_key
        async for t in search_tickets(
            self.jira, f'project = {p} AND status = "{HUMAN_REVIEW}"', self.ids.fields
        ):
            number = _pr_number(t.pr_url)
            if number is None:
                continue
            pr = await self._pr(number)
            if pr is None or not pr.merged:
                continue
            # Re-read: a human may have moved it since the search (PRD 12.5).
            if (await get_ticket(self.jira, t.key, self.ids.fields)).status != HUMAN_REVIEW:
                continue
            sha = pr.merge_commit_sha or pr.head_sha
            await add_comment(
                self.jira, t.key, orchestrator_comment(f"PR {pr.url} merged as `{sha[:12]}`.")
            )
            await transition_to(self.jira, t.key, DONE, self._cache)
            self._remove_clones(t.key)
            record_event(self.engine, "transition", {"key": t.key, "to": DONE, "merge": sha})
            actions.append(MergeAction(t.key, "done", sha))

        jql = (
            f"project = {p} AND status = {DONE} AND updated >= {DONE_LOOKBACK}"
            f' AND (labels IS EMPTY OR labels != "{STATE_MISMATCH}")'
        )
        async for t in search_tickets(self.jira, jql, self.ids.fields):
            self._remove_clones(t.key)
            number = _pr_number(t.pr_url)
            if number is None:
                continue  # no PR at all: nothing to compare
            pr = await self._pr(number)
            if pr is None or pr.merged:
                continue
            await add_labels(self.jira, t.key, [STATE_MISMATCH])
            await add_comment(
                self.jira,
                t.key,
                orchestrator_comment(
                    f"This ticket is Done but PR {pr.url} is {pr.state} and not merged. "
                    f"Labelled `{STATE_MISMATCH}`; CodeIt takes no other action."
                ),
            )
            record_event(self.engine, "error", {"key": t.key, "label": STATE_MISMATCH})
            actions.append(MergeAction(t.key, "state_mismatch", pr.state))
        return actions

    async def _pr(self, number: int) -> PullRequest | None:
        try:
            return await get_pr(self.gh, self.slug, number)
        except GitHubError as e:
            log.warning("merge_watcher.pr_unreadable", pr=number, error=str(e))
            return None

    def _remove_clones(self, key: str) -> None:
        if self.clones is not None:
            self.clones.remove(key)
            self.clones.remove(f"{key}-review")
            self.clones.remove(f"{key}-rebase")
