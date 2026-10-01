"""learning signals and proposals

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30 06:48:52.958071
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "learning_proposals",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("repo", sa.String(length=16), nullable=False),
        sa.Column("branch", sa.String(length=128), nullable=False),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("patch_path", sa.Text(), nullable=True),
        sa.Column("changes_json", sa.JSON(), nullable=False),
        sa.Column("gate_status", sa.String(length=16), nullable=False),
        sa.Column("gate_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("learning_proposals", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_learning_proposals_gate_status"), ["gate_status"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_learning_proposals_run_id"), ["run_id"], unique=False)

    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.add_column(sa.Column("external_id", sa.String(length=128), nullable=True))
        batch_op.add_column(
            sa.Column("human", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column("learn", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("status", sa.String(length=16), nullable=False, server_default="new")
        )
        batch_op.add_column(sa.Column("target", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("lesson_key", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("proposal_id", sa.String(length=26), nullable=True))
        batch_op.create_index(batch_op.f("ix_signals_external_id"), ["external_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_signals_lesson_key"), ["lesson_key"], unique=False)
        batch_op.create_index(batch_op.f("ix_signals_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_signals_status"))
        batch_op.drop_index(batch_op.f("ix_signals_lesson_key"))
        batch_op.drop_index(batch_op.f("ix_signals_external_id"))
        for column in (
            "proposal_id", "lesson_key", "target", "status", "created_at", "url", "learn",
            "human", "external_id",
        ):  # fmt: skip
            batch_op.drop_column(column)

    with op.batch_alter_table("learning_proposals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_learning_proposals_run_id"))
        batch_op.drop_index(batch_op.f("ix_learning_proposals_gate_status"))

    op.drop_table("learning_proposals")
