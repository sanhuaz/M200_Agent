"""Initial PersonalAgent schema."""

from alembic import op
from app.db.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())
    op.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
            chunk_id UNINDEXED,
            knowledge_base_id UNINDEXED,
            tokens,
            content
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chunk_fts")
    Base.metadata.drop_all(bind=op.get_bind())
