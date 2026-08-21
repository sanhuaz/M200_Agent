"""Add the conversation-to-persona foreign key constraint."""

import sqlalchemy as sa
from alembic import op

revision = "0005_conversation_persona_fk"
down_revision = "0004_conversation_persona_prompt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "persona_id" not in {column["name"] for column in inspector.get_columns("conversations")}:  # noqa: E501
        return
    foreign_keys = inspector.get_foreign_keys("conversations")
    if any(
        fk.get("referred_table") == "personas"
        and fk.get("constrained_columns") == ["persona_id"]
        for fk in foreign_keys
    ):
        return
    with op.batch_alter_table("conversations", recreate="always") as batch:
        batch.create_foreign_key(
            "fk_conversations_persona_id_personas",
            "personas",
            ["persona_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "conversations" not in inspector.get_table_names():
        return
    foreign_keys = inspector.get_foreign_keys("conversations")
    if not any(
        fk.get("referred_table") == "personas"
        and fk.get("constrained_columns") == ["persona_id"]
        for fk in foreign_keys
    ):
        return
    with op.batch_alter_table("conversations", recreate="always") as batch:
        batch.drop_constraint("fk_conversations_persona_id_personas", type_="foreignkey")
