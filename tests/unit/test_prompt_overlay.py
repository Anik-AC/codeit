from __future__ import annotations

from pathlib import Path

from codeit.prompts import PROMPTS_DIR, overlay, prompt_hash, prompt_path, render


def test_overlay_wins_inside_the_block_only(tmp_path: Path) -> None:
    original = render("reviewer/retry.md", error="x")
    (tmp_path / "reviewer").mkdir()
    (tmp_path / "reviewer" / "retry.md").write_text("Overlaid: {{ error }}\n")
    (tmp_path / "reviewer" / "checklist.md").write_text("- One rule.\n")
    before = prompt_hash("reviewer")
    with overlay(tmp_path):
        assert render("reviewer/retry.md", error="x") == "Overlaid: x\n"
        assert prompt_path("reviewer/checklist.md") == tmp_path / "reviewer" / "checklist.md"
        assert prompt_path("planner/system.md") == PROMPTS_DIR / "planner" / "system.md"
        assert prompt_hash("reviewer") != before
    assert render("reviewer/retry.md", error="x") == original
    assert prompt_hash("reviewer") == before
