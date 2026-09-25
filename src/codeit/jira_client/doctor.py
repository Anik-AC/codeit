"""`codeit jira doctor`: ordered setup checks with remediation hints (PRD 7.3).

Checks run in order and stop early when a later check cannot mean anything
(no auth -> nothing else; no project -> no statuses or fields).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codeit.jira_client.client import API, JiraAuthError, JiraClient, JiraError, JiraNotFound
from codeit.jira_client.discover import get_project, get_statuses, newest_jql, resolve_fields
from codeit.jira_client.issues import IssueSpec, create_issue, delete_issue
from codeit.jira_client.models import REQUIRED_ISSUE_TYPES, REQUIRED_STATUSES
from codeit.jira_client.search import search
from codeit.jira_client.transitions import list_transitions

DOCTOR_LABEL = "codeit-doctor"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""


async def run_doctor(
    client: JiraClient, project_key: str, *, write_test: bool = False
) -> list[Check]:
    checks: list[Check] = []

    # 1. auth
    try:
        me = await client.get_json(f"{API}/myself")
        checks.append(Check("auth", True, f"authenticated as {me.get('displayName', '?')}"))
    except JiraAuthError as e:
        checks.append(
            Check(
                "auth",
                False,
                str(e),
                "Check JIRA_EMAIL and JIRA_API_TOKEN in .env. The token must belong to that "
                "email; create one at https://id.atlassian.com/manage-profile/security/api-tokens",
            )
        )
        return checks
    except JiraError as e:
        checks.append(Check("auth", False, str(e), "Check JIRA_BASE_URL in .env"))
        return checks

    # 2. project
    try:
        project = await get_project(client, project_key)
    except JiraNotFound:
        checks.append(
            Check(
                "project",
                False,
                f"project {project_key} not found or not visible",
                f"Create a team-managed project with key {project_key}, or give this "
                "account access to it (PRD 7.1)",
            )
        )
        return checks
    missing_types = [t for t in REQUIRED_ISSUE_TYPES if t not in project.issue_types]
    checks.append(
        Check(
            "project",
            not missing_types,
            f"{project.key} ({project.name}, {project.style or 'unknown style'})"
            + (f"; missing work types: {', '.join(missing_types)}" if missing_types else ""),
            "Add the missing work types in Project settings > Work types" if missing_types else "",
        )
    )

    # 3. statuses
    statuses = await get_statuses(client, project_key)
    missing = [s for s in REQUIRED_STATUSES if s not in statuses]
    checks.append(
        Check(
            "statuses",
            not missing,
            f"missing: {', '.join(missing)}"
            if missing
            else f"all {len(REQUIRED_STATUSES)} present",
            "Add them to the Story workflow: Project settings > Work types > Story > "
            "Edit workflow (PRD 7.1 step 3)"
            if missing
            else "",
        )
    )

    # 4. custom fields exist, and are on the Story work type
    all_fields = await client.get_json(f"{API}/field")
    field_ids, problems = resolve_fields(all_fields, project.id)
    story_id = project.issue_types.get("Story")
    if not problems and story_id:
        on_story = await _create_meta_field_ids(client, project_key, story_id)
        problems += [
            f"field {attr} ({fid}) is not on the Story work type"
            for attr, fid in field_ids.items()
            if fid not in on_story
        ]
    checks.append(
        Check(
            "fields",
            not problems,
            "; ".join(problems) if problems else f"all {len(field_ids)} present on Story",
            "Add the fields in Project settings > Work types > Story (PRD 7.1 step 5). Story "
            "points need estimation enabled in board settings (step 4)"
            if problems
            else "",
        )
    )

    # 5. transitions (needs an issue to sample) and 6. write test
    sample_key: str | None = None
    created_key: str | None = None
    if write_test and story_id:
        try:
            created_key = await create_issue(
                client,
                IssueSpec(
                    project_key=project_key,
                    issue_type_id=story_id,
                    summary="codeit jira doctor write test (safe to delete)",
                    labels=[DOCTOR_LABEL],
                ),
            )
        except JiraError as e:
            checks.append(
                Check("write", False, f"create failed: {e}", "Grant the account Create work items")
            )
    sample_key = created_key or await _newest_issue(client, project_key)
    checks.append(await _check_transitions(client, sample_key, statuses))

    if created_key:
        try:
            await delete_issue(client, created_key)
            checks.append(Check("write", True, f"created and deleted {created_key}"))
        except JiraError as e:
            checks.append(
                Check(
                    "write",
                    False,
                    f"created {created_key} but delete failed: {e}",
                    f"Grant the account Delete work items, or delete {created_key} by hand",
                )
            )
    return checks


async def _newest_issue(client: JiraClient, project_key: str) -> str | None:
    async for issue in search(client, newest_jql(project_key), ["status"], max_results=1):
        return str(issue["key"])
    return None


async def _check_transitions(
    client: JiraClient, key: str | None, statuses: dict[str, str]
) -> Check:
    if key is None:
        return Check(
            "transitions",
            True,
            "skipped: the project has no Stories to sample (run with --write-test)",
        )
    issue = await client.get_json(f"{API}/issue/{key}", params={"fields": "status"})
    current = str(issue["fields"]["status"]["name"])
    reachable = {t.to_status for t in await list_transitions(client, key)}
    wanted = [s for s in REQUIRED_STATUSES if s in statuses and s != current]
    missing = [s for s in wanted if s not in reachable]
    return Check(
        "transitions",
        not missing,
        f"from {current} on {key}, cannot reach: {', '.join(missing)}"
        if missing
        else f"every status reachable from {current} on {key}",
        "In the workflow editor, tick 'Allow all statuses to transition to this one' on "
        "each status (PRD 7.1 step 3)"
        if missing
        else "",
    )


async def _create_meta_field_ids(
    client: JiraClient, project_key: str, issue_type_id: str
) -> set[str]:
    ids: set[str] = set()
    start = 0
    for _ in range(20):
        data: dict[str, Any] = await client.get_json(
            f"{API}/issue/createmeta/{project_key}/issuetypes/{issue_type_id}",
            params={"startAt": start, "maxResults": 50},
        )
        page = data.get("fields") or data.get("values") or []
        ids |= {str(f.get("fieldId") or f.get("key")) for f in page}
        start += len(page)
        if not page or start >= int(data.get("total", 0)):
            break
    return ids
