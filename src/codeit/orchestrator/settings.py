"""Runtime switches the owner flips while CodeIt runs, kept in `data/settings.json` so the
dashboard, the CLI and a restarted orchestrator all see the same value (ADR-0016).

- `fast_lane`: skip the Reviewer agent. Tickets that reach Agent Review go straight to
  Human Review. CI and the human merge still gate `main`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

FILE = "settings.json"


class RuntimeSettings(BaseModel):
    fast_lane: bool = False
    changed_at: datetime | None = None
    changed_by: str | None = None


def load(data_dir: Path) -> RuntimeSettings:
    try:
        return RuntimeSettings.model_validate(
            json.loads((data_dir / FILE).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, ValidationError):
        return RuntimeSettings()


def save(data_dir: Path, *, by: str, **changes: object) -> RuntimeSettings:
    current = load(data_dir)
    updated = current.model_copy(
        update={**changes, "changed_at": datetime.now(UTC), "changed_by": by}
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / FILE).write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    return updated
