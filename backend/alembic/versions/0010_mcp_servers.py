"""Add MCP server configuration and per-directory grants."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_mcp_servers"
down_revision: str | None = "0009_remove_legacy_persona_payload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("transport", sa.String(length=30), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("secret_refs_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("access_policy", sa.String(length=30), nullable=False, server_default="owner_only"),
        sa.Column("private_users_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="disabled"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("server_info_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("catalog_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("last_connected_at", sa.DateTime(), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_mcp_servers_slug", "mcp_servers", ["slug"], unique=False)
    op.create_index("ix_mcp_servers_enabled", "mcp_servers", ["enabled"], unique=False)
    op.create_index("ix_mcp_servers_status", "mcp_servers", ["status"], unique=False)

    op.create_table(
        "mcp_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("item_key", sa.String(length=500), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "kind", "item_key", name="uq_mcp_grant_item"),
    )
    op.create_index("ix_mcp_grants_server_id", "mcp_grants", ["server_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_mcp_grants_server_id", table_name="mcp_grants")
    op.drop_table("mcp_grants")
    op.drop_index("ix_mcp_servers_status", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_enabled", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_slug", table_name="mcp_servers")
    op.drop_table("mcp_servers")
