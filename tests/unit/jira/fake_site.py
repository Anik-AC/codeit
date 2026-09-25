"""A respx-backed fake Jira site that passes every doctor check unless told otherwise."""

from __future__ import annotations

from typing import Any

import httpx
import respx

from codeit.jira_client.models import REQUIRED_STATUSES

PROJECT_ID = "10000"
STORY_ID = "10001"
EPIC_ID = "10002"

CUSTOM = {
    "Agent": "customfield_10042",
    "Review Loop": "customfield_10043",
    "Human Returns": "customfield_10044",
    "PR URL": "customfield_10045",
    "Run ID": "customfield_10046",
}


def scoped(fid: str, name: str, project_id: str = PROJECT_ID) -> dict[str, Any]:
    return {
        "id": fid,
        "name": name,
        "custom": True,
        "scope": {"type": "PROJECT", "project": {"id": project_id}},
    }


def default_fields() -> list[dict[str, Any]]:
    fields = [
        {"id": "summary", "name": "Summary", "custom": False},
        {"id": "customfield_10016", "name": "Story point estimate", "custom": True},
        # Same name in another team-managed project must be ignored.
        scoped("customfield_20042", "Agent", project_id="99999"),
    ]
    fields += [scoped(fid, name) for name, fid in CUSTOM.items()]
    return fields


class FakeSite:
    def __init__(self, mock: respx.MockRouter) -> None:
        self.mock = mock
        self.statuses = list(REQUIRED_STATUSES)
        self.fields = default_fields()
        self.story_fields = {f["id"] for f in self.fields}
        self.issues: list[str] = ["CODEIT-1"]
        self.current_status = "Agent Draft"
        self.reachable = [s for s in REQUIRED_STATUSES if s != "Agent Draft"]

    def install(self) -> FakeSite:
        m = self.mock
        m.get("/myself").respond(json={"displayName": "CodeIt Bot"})
        m.get("/project/CODEIT").respond(
            json={
                "id": PROJECT_ID,
                "key": "CODEIT",
                "name": "CodeIt",
                "style": "next-gen",
                "issueTypes": [{"id": STORY_ID, "name": "Story"}, {"id": EPIC_ID, "name": "Epic"}],
            }
        )
        m.get("/project/CODEIT/statuses").mock(side_effect=self._statuses)
        m.get("/field").mock(side_effect=lambda r: httpx.Response(200, json=self.fields))
        m.get(f"/issue/createmeta/CODEIT/issuetypes/{STORY_ID}").mock(side_effect=self._meta)
        m.post("/search/jql").mock(side_effect=self._search)
        m.get(path__regex=r"/issue/[A-Z]+-\d+/transitions").mock(side_effect=self._transitions)
        m.get(path__regex=r"/issue/[A-Z]+-\d+$").mock(
            side_effect=lambda r: httpx.Response(
                200, json={"fields": {"status": {"name": self.current_status}}}
            )
        )
        self.create_route = m.post("/issue").respond(201, json={"id": "1", "key": "CODEIT-99"})
        self.delete_route = m.delete("/issue/CODEIT-99").respond(204)
        return self

    def _statuses(self, request: httpx.Request) -> httpx.Response:
        statuses = [{"id": str(i), "name": n} for i, n in enumerate(self.statuses, 1)]
        return httpx.Response(200, json=[{"name": "Story", "statuses": statuses}])

    def _meta(self, request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startAt", 0))
        fields = [{"fieldId": f} for f in sorted(self.story_fields)]
        page = fields[start : start + 3]  # small pages to exercise paging
        return httpx.Response(200, json={"fields": page, "total": len(fields)})

    def _search(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issues": [{"key": k} for k in self.issues[:1]]})

    def _transitions(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "transitions": [
                    {"id": str(20 + i), "name": s, "to": {"name": s, "id": str(i)}}
                    for i, s in enumerate(self.reachable)
                ]
            },
        )
