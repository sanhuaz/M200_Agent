"""Replace compiled persona assignments with per-conversation raw prompts."""

import sqlalchemy as sa
from alembic import op

revision = "0004_conversation_persona_prompt"
down_revision = "0003_context_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "persona_id" not in conversation_columns:
        with op.batch_alter_table("conversations", recreate="always") as batch:
            batch.add_column(
                sa.Column(
                    "persona_id",
                    sa.String(36),
                    nullable=True,
                )
            )

    tables = set(inspector.get_table_names())
    if "persona_assignments" in tables:
        op.drop_table("persona_assignments")

    persona_columns = {column["name"] for column in inspector.get_columns("personas")}
    legacy_columns = {"compiled_style", "status", "error"}.intersection(persona_columns)
    if legacy_columns:
        with op.batch_alter_table("personas") as batch:
            for column in sorted(legacy_columns):
                batch.drop_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    persona_columns = {column["name"] for column in inspector.get_columns("personas")}
    with op.batch_alter_table("personas") as batch:
        if "compiled_style" not in persona_columns:
            batch.add_column(sa.Column("compiled_style", sa.Text(), nullable=True))
        if "status" not in persona_columns:
            batch.add_column(sa.Column("status", sa.String(20), nullable=False, server_default="draft"))
        if "error" not in persona_columns:
            batch.add_column(sa.Column("error", sa.Text(), nullable=True))

    if "persona_assignments" not in inspector.get_table_names():
        op.create_table(
            "persona_assignments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("scope_type", sa.String(20), nullable=False),
            sa.Column("user_id", sa.String(120), nullable=True),
            sa.Column(
                "persona_id",
                sa.String(36),
                sa.ForeignKey("personas.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("scope_type", "user_id", name="uq_persona_assignment_scope"),
        )

    conversation_columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "persona_id" in conversation_columns:
        with op.batch_alter_table("conversations") as batch:
            batch.drop_column("persona_id")
