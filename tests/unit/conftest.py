"""Unit tests never see real credentials: `.env` is not read and secret variables are
unset, so a command that would reach Jira or GitHub fails fast instead of acting on real
data (a CLI test once started a real run this way)."""

from __future__ import annotations

import pytest

from codeit.config import Secrets


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Secrets.model_config, "env_file", None)
    for name in Secrets.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
