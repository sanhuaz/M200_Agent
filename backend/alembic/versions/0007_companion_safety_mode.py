"""Add the companion safety mode preference."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_companion_safety_mode"
down_revision: str | None = "0006_companion_p0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companion_preferences",
        sa.Column("safety_mode", sa.String(length=20), nullable=False, server_default="standard"),
    )


def downgrade() -> None:
    with op.batch_alter_table("companion_preferences") as batch_op:
        batch_op.drop_column("safety_mode")
