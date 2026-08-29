"""Add structured personas, strategy guides and listening buffers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_companion_reply_integrity"
down_revision: str | None = "0007_companion_safety_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("personas", sa.Column("card_json", sa.Text(), nullable=False, server_default="{}"))
    op.add_column(
        "personas", sa.Column("status", sa.String(length=30), nullable=False, server_default="active")
    )
    op.add_column("personas", sa.Column("card_version", sa.Integer(), nullable=False, server_default="1"))
    op.execute(
        "UPDATE personas SET status = 'needs_migration', card_version = 0 "
        "WHERE (card_json IS NULL OR card_json = '{}') AND trim(raw_prompt) <> ''"
    )

    op.add_column(
        "companion_preferences",
        sa.Column("listening_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "companion_preferences",
        sa.Column("listening_silence_seconds", sa.Integer(), nullable=False, server_default="30"),
    )

    op.create_table(
        "strategy_guides",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("strategy", sa.String(length=30), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy"),
    )
    op.create_index("ix_strategy_guides_strategy", "strategy_guides", ["strategy"])

    op.create_table(
        "strategy_guide_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("strategy", sa.String(length=30), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="edit"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy", "version", name="uq_strategy_guide_revision"),
    )
    op.create_index(
        "ix_strategy_guide_revisions_strategy", "strategy_guide_revisions", ["strategy"]
    )

    op.create_table(
        "companion_listening_buffers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("scope_id", sa.String(length=120), nullable=False),
        sa.Column("fragments", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("conversation_id"),
    )
    op.create_index(
        "ix_companion_listening_buffers_conversation_id",
        "companion_listening_buffers",
        ["conversation_id"],
    )
    op.create_index(
        "ix_companion_listening_buffers_scope_id", "companion_listening_buffers", ["scope_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_companion_listening_buffers_scope_id", table_name="companion_listening_buffers")
    op.drop_index(
        "ix_companion_listening_buffers_conversation_id", table_name="companion_listening_buffers"
    )
    op.drop_table("companion_listening_buffers")
    op.drop_index("ix_strategy_guide_revisions_strategy", table_name="strategy_guide_revisions")
    op.drop_table("strategy_guide_revisions")
    op.drop_index("ix_strategy_guides_strategy", table_name="strategy_guides")
    op.drop_table("strategy_guides")
    with op.batch_alter_table("companion_preferences") as batch_op:
        batch_op.drop_column("listening_silence_seconds")
        batch_op.drop_column("listening_enabled")
    with op.batch_alter_table("personas") as batch_op:
        batch_op.drop_column("card_version")
        batch_op.drop_column("status")
        batch_op.drop_column("card_json")
