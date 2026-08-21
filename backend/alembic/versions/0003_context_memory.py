"""Add incremental conversation summary boundary and message lookup index."""

import sqlalchemy as sa
from alembic import op

revision = "0003_context_memory"
down_revision = "0002_v020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "summary_up_to_message_id" not in conversation_columns:
        op.add_column(
            "conversations",
            sa.Column("summary_up_to_message_id", sa.String(36), nullable=True),
        )

    indexes = {index["name"] for index in inspector.get_indexes("messages")}
    if "ix_messages_conversation_created_at" not in indexes:
        op.create_index(
            "ix_messages_conversation_created_at",
            "messages",
            ["conversation_id", "created_at"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("messages")}
    if "ix_messages_conversation_created_at" in indexes:
        op.drop_index("ix_messages_conversation_created_at", table_name="messages")
    conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "summary_up_to_message_id" in conversation_columns:
        op.drop_column("conversations", "summary_up_to_message_id")
