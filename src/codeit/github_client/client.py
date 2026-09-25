"""Async GitHub REST session: auth, a few retries, Link-header paging."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Any, Self

import httpx

API_URL = "https://api.github.com"
MAX_PAGES = 10
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')

Sleep = Callable[[float], Awaitable[None]]


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubNotFound(GitHubError):
    pass


def repo_slug(url: str) -> str:
    """`owner/name` from an HTTPS or SSH GitHub URL."""
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise ValueError(f"not a GitHub repository URL: {url}")
    return f"{m.group(1)}/{m.group(2)}"


class GitHubClient:
    def __init__(
        self,
        token: str | None,
        *,
        base_url: str = API_URL,
        max_attempts: int = 3,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30)
        self._max_attempts = max_attempts
        self._sleep = sleep

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = await self._http.get(url, params=params)
            except httpx.TransportError as e:
                if attempt == self._max_attempts:
                    raise GitHubError(f"GET {url}: {e}") from e
                await self._sleep(2**attempt)
                continue
            if resp.status_code < 400:
                return resp
            retryable = resp.status_code in (429, 500, 502, 503, 504)
            if retryable and attempt < self._max_attempts:
                await self._sleep(float(resp.headers.get("Retry-After", 2**attempt)))
                continue
            message = f"GET {url} -> {resp.status_code}: {_message(resp)}"
            if resp.status_code == 404:
                raise GitHubNotFound(message, 404)
            raise GitHubError(message, resp.status_code)
        raise AssertionError("unreachable")

    async def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        return (await self._get(url, params)).json()

    async def get_all(self, url: str, params: Mapping[str, Any] | None = None) -> list[Any]:
        """Every item of a paged list endpoint, following `Link: rel="next"`."""
        items: list[Any] = []
        next_url: str | None = url
        query: Mapping[str, Any] | None = {"per_page": 100, **(params or {})}
        for _ in range(MAX_PAGES):
            if next_url is None:
                break
            resp = await self._get(next_url, query)
            items += resp.json()
            m = _NEXT.search(resp.headers.get("Link", ""))
            next_url, query = (m.group(1), None) if m else (None, None)
        return items


def _message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", resp.reason_phrase))
    except ValueError:
        return resp.reason_phrase
