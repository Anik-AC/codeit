"""A stateful fake Jira over respx for orchestrator tests: search by status, read tickets,
transition, label and comment. Enough JQL for the queries the orchestrator sends."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

from codeit.jira_client import JiraIds
from tests.unit.jira.conftest import FIELDS, issue_json

STATUSES = ["Ready for Dev", "In Dev", "Agent Review", "Human Review", "Done"]
IDS = JiraIds(
    fields=FIELDS,
    statuses={},
    issue_types={"Story": "1"},
    transitions={s: str(i) for i, s in enumerate(STATUSES, 1)},
)


@dataclass
class Issue:
    status: str
    pr_url: str | None = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)


class FakeJira:
    def __init__(self, router: respx.MockRouter) -> None:
        self.issues: dict[str, Issue] = {}
        self.moves: list[tuple[str, str]] = []
        self.jql: list[str] = []
        router.post("/search/jql").mock(side_effect=self._search)
        router.get(path__regex=r"/issue/[A-Z]+-\d+$").mock(side_effect=self._get)
        router.put(path__regex=r"/issue/[A-Z]+-\d+$").mock(side_effect=self._put)
        router.post(path__regex=r"/issue/[A-Z]+-\d+/transitions$").mock(
            side_effect=self._transition
        )
        router.post(path__regex=r"/issue/[A-Z]+-\d+/comment$").mock(side_effect=self._comment)

    def add(self, key: str, status: str, **kw: Any) -> Issue:
        self.issues[key] = Issue(status, **kw)
        return self.issues[key]

    @staticmethod
    def _key(request: httpx.Request) -> str:
        m = re.search(r"/issue/([A-Z]+-\d+)", request.url.path)
        assert m
        return m.group(1)

    def _json(self, key: str) -> dict[str, Any]:
        i = self.issues[key]
        links = [{"type": {"name": "Blocks"}, "inwardIssue": {"key": b}} for b in i.blocked_by]
        return issue_json(
            key,
            status={"name": i.status},
            labels=list(i.labels),
            issuelinks=links,
            **{FIELDS.pr_url: i.pr_url},
        )

    def _search(self, request: httpx.Request) -> httpx.Response:
        jql = json.loads(request.content)["jql"]
        self.jql.append(jql)
        m = re.search(r'status = "?([A-Za-z ]+?)"?(?: AND| ORDER|$)', jql)
        assert m, jql
        found = [k for k, i in self.issues.items() if i.status == m.group(1)]
        if 'labels != "state-mismatch"' in jql:
            found = [k for k in found if "state-mismatch" not in self.issues[k].labels]
        return httpx.Response(200, json={"issues": [self._json(k) for k in found]})

    def _get(self, request: httpx.Request) -> httpx.Response:
        key = self._key(request)
        if key not in self.issues:
            return httpx.Response(404, json={"errorMessages": ["no"]})
        return httpx.Response(200, json=self._json(key))

    def _put(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        for op in body.get("update", {}).get("labels", []):
            if "add" in op:
                self.issues[self._key(request)].labels.append(op["add"])
        return httpx.Response(204)

    def _transition(self, request: httpx.Request) -> httpx.Response:
        key = self._key(request)
        tid = json.loads(request.content)["transition"]["id"]
        to = STATUSES[int(tid) - 1]
        self.issues[key].status = to
        self.moves.append((key, to))
        return httpx.Response(204)

    def _comment(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)["body"]
        text = json.dumps(body)
        self.issues[self._key(request)].comments.append(text)
        return httpx.Response(201, json={"id": "1"})
