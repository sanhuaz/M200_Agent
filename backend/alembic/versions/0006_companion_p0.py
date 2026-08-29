"""Add the P0 companion data model."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_companion_p0"
down_revision: str | None = "0005_conversation_persona_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companion_preferences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "scope_type", sa.String(length=20), nullable=False, server_default="qq_user"
        ),
        sa.Column("scope_id", sa.String(length=120), nullable=False),
        sa.Column(
            "companion_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("support_mode", sa.String(length=20), nullable=False, server_default="auto"),
        sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("analyzer_model_alias", sa.String(length=80), nullable=True),
        sa.Column("boundaries", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_type", "scope_id", name="uq_companion_preference_scope"),
    )
    op.create_index(
        "ix_companion_preferences_scope_id", "companion_preferences", ["scope_id"]
    )

    op.create_table(
        "relationship_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_id", sa.String(length=120), nullable=False),
        sa.Column("persona_key", sa.String(length=80), nullable=False, server_default="default"),
        sa.Column("persona_id", sa.String(length=36), nullable=True),
        sa.Column("nickname", sa.String(length=120), nullable=True),
        sa.Column("shared_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("boundaries", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_id", "persona_key", name="uq_relationship_scope_persona"),
    )
    op.create_index(
        "ix_relationship_profiles_scope_id", "relationship_profiles", ["scope_id"]
    )
    op.create_index(
        "ix_relationship_profiles_persona_id", "relationship_profiles", ["persona_id"]
    )

    op.create_table(
        "emotion_assessments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_message_id", sa.String(length=36), nullable=False),
        sa.Column("scope_id", sa.String(length=120), nullable=False),
        sa.Column("candidate_emotions", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("primary_emotion", sa.String(length=40), nullable=False, server_default="neutral"),
        sa.Column("intensity", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("support_need", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("risk_level", sa.String(length=20), nullable=False, server_default="low"),
        sa.Column("next_action", sa.String(length=30), nullable=False, server_default="clarify"),
        sa.Column("model_alias", sa.String(length=80), nullable=True),
        sa.Column(
            "prompt_version",
            sa.String(length=80),
            nullable=False,
            server_default="companion-analysis-v1",
        ),
        sa.Column(
            "classifier_version",
            sa.String(length=80),
            nullable=False,
            server_default="emotion-classifier-v1",
        ),
        sa.Column("schema_valid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("correction", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_message_id"),
    )
    op.create_index(
        "ix_emotion_assessments_user_message_id", "emotion_assessments", ["user_message_id"]
    )
    op.create_index("ix_emotion_assessments_scope_id", "emotion_assessments", ["scope_id"])

    op.create_table(
        "response_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("assistant_message_id", sa.String(length=36), nullable=False),
        sa.Column("scope_id", sa.String(length=120), nullable=False),
        sa.Column("feedback", sa.String(length=30), nullable=False),
        sa.Column("correction", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assistant_message_id"),
    )
    op.create_index(
        "ix_response_feedback_assistant_message_id", "response_feedback", ["assistant_message_id"]
    )
    op.create_index("ix_response_feedback_scope_id", "response_feedback", ["scope_id"])

    op.create_table(
        "safety_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("scope_id", sa.String(length=120), nullable=False),
        sa.Column("risk_level", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column(
            "detector_version",
            sa.String(length=80),
            nullable=False,
            server_default="companion-safety-v1",
        ),
        sa.Column("details", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_safety_events_message_id", "safety_events", ["message_id"])
    op.create_index("ix_safety_events_scope_id", "safety_events", ["scope_id"])


def downgrade() -> None:
    op.drop_index("ix_safety_events_scope_id", table_name="safety_events")
    op.drop_index("ix_safety_events_message_id", table_name="safety_events")
    op.drop_table("safety_events")
    op.drop_index("ix_response_feedback_scope_id", table_name="response_feedback")
    op.drop_index("ix_response_feedback_assistant_message_id", table_name="response_feedback")
    op.drop_table("response_feedback")
    op.drop_index("ix_emotion_assessments_scope_id", table_name="emotion_assessments")
    op.drop_index("ix_emotion_assessments_user_message_id", table_name="emotion_assessments")
    op.drop_table("emotion_assessments")
    op.drop_index("ix_relationship_profiles_persona_id", table_name="relationship_profiles")
    op.drop_index("ix_relationship_profiles_scope_id", table_name="relationship_profiles")
    op.drop_table("relationship_profiles")
    op.drop_index("ix_companion_preferences_scope_id", table_name="companion_preferences")
    op.drop_table("companion_preferences")
