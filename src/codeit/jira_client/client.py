"""Async Jira Cloud session: auth, retries and error mapping (PRD 7.2).

Retry policy:
- 429 and 503 are retried for every method, honoring `Retry-After` when present.
- 500, 502, 504 and dropped connections are retried only for idempotent requests, so a
  POST that may have reached Jira is never sent twice. Callers mark read-only POSTs
  (such as search) idempotent explicitly.
- Connection failures before the request was sent are always retried.
- Backoff is exponential with full jitter. At most `max_attempts` attempts.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self

import httpx

from codeit.config import Secrets
from codeit.log import get_logger

log = get_logger(__name__)

API = "/rest/api/3"
DEFAULT_TIMEOUT_S = 30.0

_RETRY_ALWAYS = frozenset({429, 503})
_RETRY_IDEMPOTENT = frozenset({500, 502, 504})
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class JiraError(Exception):
    """Base error for Jira calls. `messages` holds Jira's own error strings."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        messages: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.status = status
        self.messages = messages


class JiraConfigError(JiraError):
    """Credentials or base URL are missing from `.env`."""


class JiraAuthError(JiraError):
    """401 or 403: bad credentials, or the account lacks permission."""


class JiraNotFound(JiraError):
    """404: the resource does not exist or the account cannot see it."""


class JiraConflict(JiraError):
    """409, or a requested state change is not possible (e.g. unreachable transition)."""


class JiraBadRequest(JiraError):
    """400: Jira rejected the payload."""


class JiraRateLimited(JiraError):
    """429 after all retries, or a `Retry-After` longer than the client will wait."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


Sleep = Callable[[float], Awaitable[None]]


class JiraClient:
    """Thin async wrapper over `httpx.AsyncClient` for the Jira REST API v3."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_attempts: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        max_retry_after: float = 120.0,
        sleep: Sleep = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            auth=httpx.BasicAuth(email, api_token),
            timeout=timeout,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    @classmethod
    def from_secrets(cls, secrets: Secrets, **kwargs: Any) -> Self:
        """Build a client from `.env`. Raises `JiraConfigError` naming missing variables."""
        missing = [
            name
            for name, value in (
                ("JIRA_BASE_URL", secrets.jira_base_url),
                ("JIRA_EMAIL", secrets.jira_email),
                ("JIRA_API_TOKEN", secrets.jira_api_token),
            )
            if not value
        ]
        if missing:
            raise JiraConfigError(f"missing in .env: {', '.join(missing)}")
        assert secrets.jira_base_url and secrets.jira_email and secrets.jira_api_token
        if not secrets.jira_base_url.startswith("https://"):
            raise JiraConfigError("JIRA_BASE_URL must start with https://")
        return cls(
            secrets.jira_base_url,
            secrets.jira_email,
            secrets.jira_api_token.get_secret_value(),
            **kwargs,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # requests ---------------------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        """Send a request with retries. Raises a `JiraError` subclass on failure."""
        method = method.upper()
        idem = method in _IDEMPOTENT_METHODS if idempotent is None else idempotent
        for attempt in range(1, self.max_attempts + 1):
            last = attempt == self.max_attempts
            try:
                resp = await self._http.request(method, path, params=params, json=json)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # The request never reached Jira, so retrying is always safe.
                if last:
                    raise JiraError(f"{method} {path}: cannot connect: {e}") from e
                await self._backoff_sleep(attempt, method, path, reason=type(e).__name__)
                continue
            except httpx.TransportError as e:
                if last or not idem:
                    raise JiraError(f"{method} {path}: {type(e).__name__}: {e}") from e
                await self._backoff_sleep(attempt, method, path, reason=type(e).__name__)
                continue

            status = resp.status_code
            if status < 400:
                return resp
            retryable = status in _RETRY_ALWAYS or (idem and status in _RETRY_IDEMPOTENT)
            if not retryable or last:
                raise _error_for(resp, method, path)
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if retry_after is not None and retry_after > self.max_retry_after:
                raise _error_for(resp, method, path)
            if retry_after is not None:
                log.info("jira.retry", method=method, path=path, status=status, delay=retry_after)
                await self._sleep(retry_after)
            else:
                await self._backoff_sleep(attempt, method, path, reason=str(status))
        raise AssertionError("unreachable")

    async def _backoff_sleep(self, attempt: int, method: str, path: str, reason: str) -> None:
        cap = min(self.backoff_cap, self.backoff_base * 2 ** (attempt - 1))
        delay = random.uniform(0, cap)  # noqa: S311 - jitter, not crypto
        log.info("jira.retry", method=method, path=path, reason=reason, delay=round(delay, 2))
        await self._sleep(delay)

    async def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return (await self.request("GET", path, params=params)).json()

    async def post_json(self, path: str, json: Any, *, idempotent: bool | None = None) -> Any:
        resp = await self.request("POST", path, json=json, idempotent=idempotent)
        return resp.json() if resp.content else None

    async def put(self, path: str, json: Any) -> None:
        await self.request("PUT", path, json=json)

    async def delete(self, path: str, params: Mapping[str, Any] | None = None) -> None:
        await self.request("DELETE", path, params=params)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _jira_messages(resp: httpx.Response) -> tuple[str, ...]:
    try:
        body = resp.json()
    except ValueError:
        return ()
    if not isinstance(body, dict):
        return ()
    msgs = [str(m) for m in body.get("errorMessages") or []]
    errors = body.get("errors") or {}
    if isinstance(errors, dict):
        msgs += [f"{k}: {v}" for k, v in errors.items()]
    return tuple(msgs)


def _error_for(resp: httpx.Response, method: str, path: str) -> JiraError:
    status = resp.status_code
    messages = _jira_messages(resp)
    detail = "; ".join(messages) or resp.reason_phrase
    text = f"{method} {path} -> {status}: {detail}"
    if status in (401, 403):
        return JiraAuthError(text, status=status, messages=messages)
    if status == 404:
        return JiraNotFound(text, status=status, messages=messages)
    if status == 409:
        return JiraConflict(text, status=status, messages=messages)
    if status == 400:
        return JiraBadRequest(text, status=status, messages=messages)
    if status == 429:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        return JiraRateLimited(text, retry_after=retry_after, status=status, messages=messages)
    return JiraError(text, status=status, messages=messages)
