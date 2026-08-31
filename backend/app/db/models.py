from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(20), default="web")
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    conversation_type: Mapped[str] = mapped_column(String(20), default="private")
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    model_alias: Mapped[str] = mapped_column(String(80), default="default")
    persona_id: Mapped[str | None] = mapped_column(
        ForeignKey("personas.id", ondelete="SET NULL"), nullable=True
    )
    owner_id: Mapped[str] = mapped_column(String(120), default="local-owner")
    summary: Mapped[str] = mapped_column(Text, default="")
    summary_up_to_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_conversation_platform_external"),)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    sender_id: Mapped[str] = mapped_column(String(120), default="local-owner")
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    platform_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    attachments: Mapped[list[MessageAttachment]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    __table_args__ = (Index("ix_messages_conversation_created_at", "conversation_id", "created_at"),)


class MessageAttachment(Base):
    """Private image belonging to a user message."""

    __tablename__ = "message_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(260))
    mime_type: Mapped[str] = mapped_column(String(120))
    byte_size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    relative_path: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="web")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    message: Mapped[Message] = relationship(back_populates="attachments")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    embedding_profile: Mapped[str] = mapped_column(String(80), default="local-bge")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(260))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("knowledge_base_id", "sha256", name="uq_document_kb_sha"),)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    heading_path: Mapped[str] = mapped_column(Text, default="[]")
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope_type: Mapped[str] = mapped_column(String(20), default="user", index=True)
    user_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    fact_key: Mapped[str] = mapped_column(String(200), index=True)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active")
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    extraction_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Confirmation(Base):
    __tablename__ = "confirmations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    requester_id: Mapped[str] = mapped_column(String(120))
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    requester_id: Mapped[str] = mapped_column(String(120), default="local-owner")
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[str] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ToolRun(Base):
    __tablename__ = "tool_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    arguments: Mapped[str] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExtensionPackage(Base):
    __tablename__ = "extension_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(80))
    version: Mapped[str] = mapped_column(String(80), default="0.0.0")
    description: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(20), default="local")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    install_path: Mapped[str] = mapped_column(Text)
    manifest: Mapped[str] = mapped_column(Text, default="{}")
    permissions: Mapped[str] = mapped_column(Text, default="[]")
    access_policy: Mapped[str] = mapped_column(String(30), default="owner_only")
    status: Mapped[str] = mapped_column(String(30), default="installed_disabled")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_extension_kind_name"),)


class McpServer(Base):
    """Persisted MCP connection configuration and the last safe snapshot."""

    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    transport: Mapped[str] = mapped_column(String(30))
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    secret_refs_json: Mapped[str] = mapped_column(Text, default="{}")
    access_policy: Mapped[str] = mapped_column(String(30), default="owner_only")
    private_users_json: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="disabled", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_info_json: Mapped[str] = mapped_column(Text, default="{}")
    catalog_json: Mapped[str] = mapped_column(Text, default="{}")
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    grants: Mapped[list[McpGrant]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )


class McpGrant(Base):
    """Per-directory-item grant; new discoveries are denied by default."""

    __tablename__ = "mcp_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    server_id: Mapped[str] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    item_key: Mapped[str] = mapped_column(String(500))
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    server: Mapped[McpServer] = relationship(back_populates="grants")

    __table_args__ = (
        UniqueConstraint("server_id", "kind", "item_key", name="uq_mcp_grant_item"),
    )


class Persona(Base):
    __tablename__ = "personas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    card_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AdminIdentity(Base):
    __tablename__ = "admin_identities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(20), default="qq")
    external_id: Mapped[str] = mapped_column(String(120), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(120), default="local-owner")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(120), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    filename: Mapped[str] = mapped_column(String(260))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    message_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CompanionPreference(Base):
    """Per QQ Owner companion settings.

    The scope is intentionally explicit even though the current product is a
    single-user application.  It prevents a future identity from inheriting
    another user's companion controls by accident.
    """

    __tablename__ = "companion_preferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope_type: Mapped[str] = mapped_column(String(20), default="qq_user")
    scope_id: Mapped[str] = mapped_column(String(120), index=True)
    companion_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    support_mode: Mapped[str] = mapped_column(String(20), default="auto")
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    safety_mode: Mapped[str] = mapped_column(String(20), default="standard")
    listening_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    listening_silence_seconds: Mapped[int] = mapped_column(Integer, default=30)
    analyzer_model_alias: Mapped[str | None] = mapped_column(String(80), nullable=True)
    boundaries: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", name="uq_companion_preference_scope"),
    )


class StrategyGuide(Base):
    __tablename__ = "strategy_guides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    prompt_text: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class StrategyGuideRevision(Base):
    __tablename__ = "strategy_guide_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy: Mapped[str] = mapped_column(String(30), index=True)
    prompt_text: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(30), default="edit")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("strategy", "version", name="uq_strategy_guide_revision"),)


class CompanionListeningBuffer(Base):
    __tablename__ = "companion_listening_buffers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), unique=True, index=True
    )
    scope_id: Mapped[str] = mapped_column(String(120), index=True)
    fragments: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RelationshipProfile(Base):
    __tablename__ = "relationship_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope_id: Mapped[str] = mapped_column(String(120), index=True)
    persona_key: Mapped[str] = mapped_column(String(80), default="default")
    persona_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    nickname: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shared_summary: Mapped[str] = mapped_column(Text, default="")
    boundaries: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("scope_id", "persona_key", name="uq_relationship_scope_persona"),
    )


class EmotionAssessment(Base):
    __tablename__ = "emotion_assessments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_message_id: Mapped[str] = mapped_column(String(36), index=True, unique=True)
    scope_id: Mapped[str] = mapped_column(String(120), index=True)
    candidate_emotions: Mapped[str] = mapped_column(Text, default="[]")
    primary_emotion: Mapped[str] = mapped_column(String(40), default="neutral")
    intensity: Mapped[str] = mapped_column(String(20), default="unknown")
    support_need: Mapped[str] = mapped_column(String(20), default="unknown")
    confidence: Mapped[float] = mapped_column(default=0.0)
    risk_level: Mapped[str] = mapped_column(String(20), default="low")
    next_action: Mapped[str] = mapped_column(String(30), default="clarify")
    model_alias: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(80), default="companion-analysis-v1")
    classifier_version: Mapped[str] = mapped_column(String(80), default="emotion-classifier-v1")
    schema_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    correction: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ResponseFeedback(Base):
    __tablename__ = "response_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    assistant_message_id: Mapped[str] = mapped_column(String(36), index=True, unique=True)
    scope_id: Mapped[str] = mapped_column(String(120), index=True)
    feedback: Mapped[str] = mapped_column(String(30))
    correction: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SafetyEvent(Base):
    __tablename__ = "safety_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(String(36), index=True)
    scope_id: Mapped[str] = mapped_column(String(120), index=True)
    risk_level: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(40))
    detector_version: Mapped[str] = mapped_column(String(80), default="companion-safety-v1")
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
