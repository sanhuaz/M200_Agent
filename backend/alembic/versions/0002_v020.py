"""PersonalAgent v0.2 extension, persona, admin, artifact and memory scope schema."""

import sqlalchemy as sa
from alembic import op

revision = "0002_v020"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "scope_type" not in {column["name"] for column in inspector.get_columns("memories")}:
        op.rename_table("memories", "memories_v1")
        op.create_table(
            "memories",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("scope_type", sa.String(20), nullable=False, server_default="user"),
            sa.Column("user_id", sa.String(120), nullable=True),
            sa.Column("fact_key", sa.String(200), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("source_message_id", sa.String(36), nullable=True),
            sa.Column("extraction_model", sa.String(120), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        )
        op.execute(
            """
            INSERT INTO memories
                (id, scope_type, user_id, fact_key, content, status,
                 source_message_id, extraction_model, created_at, last_seen_at)
            SELECT id, 'user', user_id, fact_key, content, status,
                   source_message_id, extraction_model, created_at, last_seen_at
            FROM memories_v1
            """
        )
        op.drop_table("memories_v1")
        op.create_index("ix_memories_scope_type", "memories", ["scope_type"])
        op.create_index("ix_memories_user_id", "memories", ["user_id"])
        op.create_index("ix_memories_fact_key", "memories", ["fact_key"])

    if "extension_packages" not in existing_tables:
        op.create_table(
            "extension_packages",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("version", sa.String(80), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("source_type", sa.String(20), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("source_ref", sa.String(120), nullable=True),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("install_path", sa.Text(), nullable=False),
            sa.Column("manifest", sa.Text(), nullable=False),
            sa.Column("permissions", sa.Text(), nullable=False),
            sa.Column("access_policy", sa.String(30), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("builtin", sa.Boolean(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("kind", "name", name="uq_extension_kind_name"),
        )
    if "personas" not in existing_tables:
        op.create_table(
            "personas",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("raw_prompt", sa.Text(), nullable=False),
            sa.Column("compiled_style", sa.Text(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("name", name="uq_personas_name"),
        )
    if "persona_assignments" not in existing_tables:
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
        op.create_index("ix_persona_assignments_user_id", "persona_assignments", ["user_id"])
    if "admin_identities" not in existing_tables:
        op.create_table(
            "admin_identities",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("platform", sa.String(20), nullable=False),
            sa.Column("external_id", sa.String(120), nullable=False),
            sa.Column("display_name", sa.String(160), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("created_by", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("external_id", name="uq_admin_identities_external_id"),
        )
    if "artifacts" not in existing_tables:
        op.create_table(
            "artifacts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("owner_id", sa.String(120), nullable=False),
            sa.Column("conversation_id", sa.String(36), nullable=True),
            sa.Column("filename", sa.String(260), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("content_type", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_artifacts_owner_id", "artifacts", ["owner_id"])
    if "app_settings" not in existing_tables:
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(120), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    for table in (
        "app_settings",
        "artifacts",
        "admin_identities",
        "persona_assignments",
        "personas",
        "extension_packages",
    ):
        op.drop_table(table)
