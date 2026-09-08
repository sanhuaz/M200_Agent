"""Associate MCP tool runs with the user turn that authorized them."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_tool_run_user_message"
down_revision: str | None = "0012_temporal_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tool_runs", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "user_message_id",
                sa.String(length=36),
                sa.ForeignKey(
                    "messages.id",
                    name="fk_tool_runs_user_message_id_messages",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
        batch.create_index(
            "ix_tool_runs_conversation_user_message",
            ["conversation_id", "user_message_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_runs", recreate="always") as batch:
        batch.drop_index("ix_tool_runs_conversation_user_message")
        batch.drop_column("user_message_id")
