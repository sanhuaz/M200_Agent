"""Remove legacy persona payload columns now that persona files are authoritative."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_remove_legacy_persona_payload"
down_revision: str | None = "0008_companion_reply_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("personas") as batch_op:
        batch_op.drop_column("card_json")
        batch_op.drop_column("raw_prompt")


def downgrade() -> None:
    with op.batch_alter_table("personas") as batch_op:
        batch_op.add_column(
            sa.Column("raw_prompt", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("card_json", sa.Text(), nullable=False, server_default="{}")
        )
