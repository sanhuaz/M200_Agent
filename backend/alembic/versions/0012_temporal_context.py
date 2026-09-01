"""Add summary and memory temporal context fields."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_temporal_context"
down_revision: str | None = "0011_message_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("summary_format_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "memories",
        sa.Column("memory_kind", sa.String(length=10), nullable=False, server_default="fact"),
    )
    op.add_column("memories", sa.Column("event_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("memories", "event_date")
    op.drop_column("memories", "memory_kind")
    op.drop_column("conversations", "summary_format_version")
