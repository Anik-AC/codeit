"""Shared fixtures. Tests marked `live` are skipped unless LIVE=1."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import structlog

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config: pytest.Config) -> None:
    # Plain tracebacks: the default rich renderer walks every local variable, which can
    # take minutes when a test logs an exception with respx mocks in scope.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(
                colors=False, exception_formatter=structlog.dev.plain_traceback
            ),
        ]
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live test; set LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def repo_config_path() -> Path:
    return REPO_ROOT / "config" / "config.yaml"
