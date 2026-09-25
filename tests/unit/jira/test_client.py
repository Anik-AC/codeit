from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx
from pydantic import SecretStr

from codeit.config import Secrets
from codeit.jira_client import (
    JiraAuthError,
    JiraBadRequest,
    JiraClient,
    JiraConfigError,
    JiraConflict,
    JiraError,
    JiraNotFound,
    JiraRateLimited,
)
from codeit.jira_client.client import _parse_retry_after
from tests.unit.jira.conftest import BASE, SleepRecorder


async def test_basic_auth_and_json(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.get("/myself").respond(json={"displayName": "Bot"})
    assert await client.get_json("/rest/api/3/myself") == {"displayName": "Bot"}
    req = route.calls.last.request
    assert req.headers["Authorization"].startswith("Basic ")
    assert req.headers["Accept"] == "application/json"


async def test_retry_after_is_honored(
    mock: respx.MockRouter, client: JiraClient, sleeps: SleepRecorder
) -> None:
    mock.get("/myself").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503, headers={"Retry-After": "2"}),
            httpx.Response(200, json={}),
        ]
    )
    await client.get_json("/rest/api/3/myself")
    assert sleeps.calls == [7.0, 2.0]


async def test_backoff_without_retry_after_grows_and_is_capped(
    mock: respx.MockRouter, sleeps: SleepRecorder
) -> None:
    mock.get("/myself").mock(return_value=httpx.Response(500))
    async with JiraClient(BASE, "e", "t", sleep=sleeps, backoff_base=1, backoff_cap=3) as c:
        with pytest.raises(JiraError) as err:
            await c.get_json("/rest/api/3/myself")
    assert err.value.status == 500
    assert len(sleeps.calls) == 4  # 5 attempts
    caps = [1, 2, 3, 3]
    assert all(0 <= s <= cap for s, cap in zip(sleeps.calls, caps, strict=True))


async def test_post_is_not_retried_on_500(
    mock: respx.MockRouter, client: JiraClient, sleeps: SleepRecorder
) -> None:
    route = mock.post("/issue").mock(return_value=httpx.Response(500))
    with pytest.raises(JiraError):
        await client.post_json("/rest/api/3/issue", {})
    assert route.call_count == 1
    assert sleeps.calls == []


async def test_post_is_retried_on_429(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.post("/issue").mock(
        side_effect=[httpx.Response(429), httpx.Response(201, json={"key": "CODEIT-1"})]
    )
    assert await client.post_json("/rest/api/3/issue", {}) == {"key": "CODEIT-1"}
    assert route.call_count == 2


async def test_idempotent_post_is_retried_on_500(
    mock: respx.MockRouter, client: JiraClient
) -> None:
    mock.post("/search/jql").mock(side_effect=[httpx.Response(502), httpx.Response(200, json={})])
    assert await client.post_json("/rest/api/3/search/jql", {}, idempotent=True) == {}


async def test_connect_error_is_retried_even_for_post(
    mock: respx.MockRouter, client: JiraClient
) -> None:
    mock.post("/issue").mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(201, json={"key": "K-1"})]
    )
    assert await client.post_json("/rest/api/3/issue", {}) == {"key": "K-1"}


async def test_read_error_on_post_is_not_retried(
    mock: respx.MockRouter, client: JiraClient
) -> None:
    route = mock.post("/issue").mock(side_effect=httpx.ReadError("reset"))
    with pytest.raises(JiraError, match="ReadError"):
        await client.post_json("/rest/api/3/issue", {})
    assert route.call_count == 1


async def test_read_error_on_get_is_retried(mock: respx.MockRouter, client: JiraClient) -> None:
    mock.get("/myself").mock(side_effect=[httpx.ReadError("reset"), httpx.Response(200, json={})])
    assert await client.get_json("/rest/api/3/myself") == {}


async def test_connect_error_gives_up(mock: respx.MockRouter, client: JiraClient) -> None:
    mock.get("/myself").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(JiraError, match="cannot connect"):
        await client.get_json("/rest/api/3/myself")


async def test_long_retry_after_raises_rate_limited(
    mock: respx.MockRouter, client: JiraClient, sleeps: SleepRecorder
) -> None:
    mock.get("/myself").respond(429, headers={"Retry-After": "3600"})
    with pytest.raises(JiraRateLimited) as err:
        await client.get_json("/rest/api/3/myself")
    assert err.value.retry_after == 3600
    assert sleeps.calls == []


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (400, JiraBadRequest),
        (401, JiraAuthError),
        (403, JiraAuthError),
        (404, JiraNotFound),
        (409, JiraConflict),
        (418, JiraError),
    ],
)
async def test_error_mapping(
    mock: respx.MockRouter, client: JiraClient, status: int, exc: type[JiraError]
) -> None:
    mock.get("/issue/X-1").respond(
        status, json={"errorMessages": ["nope"], "errors": {"summary": "required"}}
    )
    with pytest.raises(exc) as err:
        await client.get_json("/rest/api/3/issue/X-1")
    assert err.value.status == status
    assert err.value.messages == ("nope", "summary: required")
    assert "nope" in str(err.value)


async def test_error_with_non_json_body(mock: respx.MockRouter, client: JiraClient) -> None:
    mock.get("/x").respond(404, text="<html>")
    with pytest.raises(JiraNotFound) as err:
        await client.get_json("/rest/api/3/x")
    assert err.value.messages == ()


async def test_empty_post_response_and_put_delete(
    mock: respx.MockRouter, client: JiraClient
) -> None:
    mock.post("/issueLink").respond(201)
    put = mock.put("/issue/X-1").respond(204)
    delete = mock.delete("/issue/X-1").respond(204)
    assert await client.post_json("/rest/api/3/issueLink", {}) is None
    await client.put("/rest/api/3/issue/X-1", {"fields": {}})
    await client.delete("/rest/api/3/issue/X-1")
    assert put.called and delete.called


def test_parse_retry_after() -> None:
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("-3") == 0.0
    assert _parse_retry_after("garbage") is None
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    parsed = _parse_retry_after(future)
    assert parsed is not None and 25 <= parsed <= 31
    assert _parse_retry_after("Mon, 01 Jan 2001 00:00:00") == 0.0


def test_from_secrets_reports_missing() -> None:
    with pytest.raises(JiraConfigError, match="JIRA_EMAIL, JIRA_API_TOKEN"):
        JiraClient.from_secrets(Secrets(_env_file=None, jira_base_url="https://x.atlassian.net"))


def test_from_secrets_requires_https() -> None:
    secrets = Secrets(
        _env_file=None,
        jira_base_url="http://x.atlassian.net",
        jira_email="e",
        jira_api_token=SecretStr("t"),
    )
    with pytest.raises(JiraConfigError, match="https"):
        JiraClient.from_secrets(secrets)


async def test_from_secrets_builds_client() -> None:
    secrets = Secrets(
        _env_file=None,
        jira_base_url="https://x.atlassian.net/",
        jira_email="e",
        jira_api_token=SecretStr("t"),
    )
    async with JiraClient.from_secrets(secrets) as c:
        assert c.base_url == "https://x.atlassian.net"
