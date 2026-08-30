from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, desc, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.onebot import (
    onebot_manager,
    qq_reply_settings,
    set_qq_reply_settings,
)
from app.core.config import get_settings
from app.db.models import (
    AdminIdentity,
    Artifact,
    Chunk,
    CompanionListeningBuffer,
    CompanionPreference,
    Confirmation,
    Conversation,
    Document,
    EmotionAssessment,
    ExtensionPackage,
    Job,
    KnowledgeBase,
    Memory,
    Message,
    Persona,
    RelationshipProfile,
    ResponseFeedback,
    SafetyEvent,
    ToolRun,
)
from app.db.session import get_db
from app.services.chat import chat_service
from app.services.companion import (
    _safe_relation_value,
    assessment_dict,
    feedback_dict,
    get_or_create_preference,
    parse_emotion_labels_strict,
    preference_dict,
    relationship_content_items,
    relationship_dict,
    safety_event_dict,
)
from app.services.confirmations import resolve_confirmation, utc_isoformat
from app.services.documents import SUPPORTED_SUFFIXES
from app.services.embeddings import get_embedding_provider
from app.services.extensions import (
    ExtensionError,
    delete_package,
    import_github,
    import_zip,
    list_packages,
    package_dict,
    set_package_state,
)
from app.services.jobs import (
    COMPANION_ANALYSIS_RETRY_JOB_TYPE,
    create_job,
    create_manga_download_job,
    delete_manga_artifact,
    job_worker,
)
from app.services.manga import manga_service
from app.services.memories import MemoryService
from app.services.model_profiles import (
    create_profile,
    delete_profile,
    list_profiles,
    set_default_profile,
    test_connection,
    update_profile,
)
from app.services.models import model_registry
from app.services.napcat_logs import napcat_connector
from app.services.operation_logs import operation_logs
from app.services.persona_store import get_persona_store
from app.services.personas import (
    PersonaCard,
    persona_dict,
)
from app.services.runtime import admin_dict, is_owner
from app.services.strategy_guides import (
    list_strategy_guides,
    list_strategy_revisions,
    reset_strategy_guide,
    rollback_strategy_guide,
    strategy_guide_dict,
    update_strategy_guide,
)
from app.services.vector_store import safe_collection_name, vector_store

router = APIRouter()
settings = get_settings()


class ConversationCreate(BaseModel):
    title: str = "新会话"
    model_alias: str | None = None


class ModelSwitch(BaseModel):
    model_alias: str


class ModelProfileCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
    model: str = Field(min_length=1, max_length=240)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    streaming: bool = True
    temperature: float | None = Field(default=None, ge=0, le=2)
    context_window: int = Field(default=1_000_000, gt=0)
    input_soft_limit: int = Field(default=131_072, gt=0)
    max_output_tokens: int = Field(default=16_384, gt=0)
    timeout_seconds: float = Field(default=120, gt=0)


class ModelProfileUpdate(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=240)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    clear_api_key: bool = False
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    streaming: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    context_window: int | None = Field(default=None, gt=0)
    input_soft_limit: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)


class ModelConnectionTest(ModelProfileCreate):
    pass


class PersonaSwitch(BaseModel):
    persona_id: str | None = None


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=50_000)
    sender_id: str = "local-owner"


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    embedding_profile: str = "local-bge"


class EmbeddingSwitch(BaseModel):
    embedding_profile: str


class MemoryUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=5_000)


class MangaSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class MangaDownloadRequest(BaseModel):
    album_id: str = Field(pattern=r"^\d+$")
    requester_id: str = "local-owner"
    conversation_id: str | None = None


class ConfirmationResolve(BaseModel):
    requester_id: str = "local-owner"
    approve: bool = True


class BulkDeleteTokens(BaseModel):
    tokens: list[str] = Field(min_length=1)


class BulkDeleteJobIds(BaseModel):
    ids: list[str] = Field(min_length=1)


class ExtensionGithubImport(BaseModel):
    url: str = Field(min_length=1, max_length=500)


class ExtensionState(BaseModel):
    enabled: bool
    access_policy: str | None = None


class PersonaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    card: PersonaCard


class PersonaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    card: PersonaCard | None = None


class AdminCreate(BaseModel):
    external_id: str = Field(pattern=r"^\d{5,20}$")
    display_name: str | None = Field(default=None, max_length=160)


class MemoryCreate(BaseModel):
    fact_key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=5_000)
    scope_type: str = "global"
    user_id: str | None = None


class NapCatLogConfig(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    token: str | None = Field(default=None, max_length=2_000)
    clear_token: bool = False


class NapCatLogTest(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    token: str | None = Field(default=None, max_length=2_000)


class QQReplySettingsUpdate(BaseModel):
    chunked_output_enabled: bool | None = None
    chunk_target_chars: int | None = Field(default=None, ge=5, le=100)


class CompanionPreferenceUpdate(BaseModel):
    qq_user_id: str = Field(min_length=5, max_length=20, pattern=r"^\d+$")
    companion_enabled: bool | None = None
    support_mode: Literal["auto", "listen", "reflect", "advice"] | None = None
    memory_enabled: bool | None = None
    safety_mode: Literal["standard", "unfiltered"] | None = None
    listening_enabled: bool | None = None
    listening_silence_seconds: int | None = Field(default=None, ge=5, le=120)
    analyzer_model_alias: str | None = Field(default=None, max_length=80)
    boundaries: dict[str, object] | None = None


class StrategyGuideUpdate(BaseModel):
    prompt_text: str = Field(min_length=1, max_length=4_000)
    expected_version: int = Field(ge=1)


class StrategyGuideRollback(BaseModel):
    revision_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class StrategyGuideVersionRequest(BaseModel):
    expected_version: int = Field(ge=1)


class RelationshipUpdate(BaseModel):
    qq_user_id: str = Field(min_length=5, max_length=20, pattern=r"^\d+$")
    nickname: str | None = Field(default=None, max_length=120)
    shared_summary: str | None = Field(default=None, max_length=4_000)
    boundaries: dict[str, object] | None = None
    version: int = Field(default=0, ge=0)


class AssessmentCorrection(BaseModel):
    qq_user_id: str = Field(min_length=5, max_length=20, pattern=r"^\d+$")
    emotions: list[str] = Field(min_length=1, max_length=3)
    support_need: Literal[
        "listen", "comfort", "reflect", "advice", "celebrate", "space", "unknown"
    ] | None = None
    note: str | None = Field(default=None, max_length=500)


class ResponseFeedbackCreate(BaseModel):
    qq_user_id: str = Field(min_length=5, max_length=20, pattern=r"^\d+$")
    feedback: Literal["helpful", "unhelpful", "no-advice"]
    note: str | None = Field(default=None, max_length=500)


class CompanionPrivacyDelete(BaseModel):
    qq_user_id: str = Field(min_length=5, max_length=20, pattern=r"^\d+$")
    confirm_text: str
    categories: list[Literal["relationships", "assessments", "feedback", "safety", "preferences"]] = Field(
        default_factory=lambda: ["relationships", "assessments", "feedback", "safety", "preferences"]
    )


def require_loopback(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(403, "本机管理接口只允许回环地址访问")


def require_companion_owner(qq_user_id: str) -> None:
    if not is_owner(qq_user_id):
        raise HTTPException(403, "情感陪伴仅对已启用的 QQ Owner 开放")


def _default_preference_dict(qq_user_id: str) -> dict[str, object]:
    return {
        "scope_type": "qq_user",
        "scope_id": qq_user_id,
        "companion_enabled": True,
        "support_mode": "auto",
        "memory_enabled": False,
        "safety_mode": "standard",
        "listening_enabled": False,
        "listening_silence_seconds": 30,
        "analyzer_model_alias": None,
        "boundaries": {},
        "created_at": None,
        "updated_at": None,
    }


def conversation_dict(item: Conversation) -> dict[str, object]:
    return {
        "id": item.id,
        "platform": item.platform,
        "external_id": item.external_id,
        "conversation_type": item.conversation_type,
        "title": item.title,
        "model_alias": item.model_alias,
        "persona_id": item.persona_id,
        "owner_id": item.owner_id,
        "summary": item.summary,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def job_dict(item: Job) -> dict[str, object]:
    return {
        "id": item.id,
        "type": item.type,
        "status": item.status,
        "requester_id": item.requester_id,
        "conversation_id": item.conversation_id,
        "payload": json.loads(item.payload),
        "result": json.loads(item.result) if item.result else None,
        "error": item.error,
        "retry_count": item.retry_count,
        "cancel_requested": item.cancel_requested,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


@router.get("/health")
def health(session: Session = Depends(get_db)) -> dict[str, object]:
    database = "connected"
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        database = "unavailable"
    onebot_secure = bool(settings.onebot_token and settings.onebot_token != "change-me")
    onebot_status = (
        "connected"
        if onebot_manager.websocket is not None
        else "configured_disconnected"
        if onebot_secure
        else "needs_configuration"
    )
    return {
        "status": "ok" if database == "connected" else "degraded",
        "python_executable": sys.executable,
        "database": database,
        "chroma": "installed",
        "worker": "running" if job_worker.running else "stopped",
        "onebot": onebot_status,
        "qq": onebot_manager.qq_status,
        "napcat": napcat_connector.public_status(),
        "logs": {
            "session_id": operation_logs.session_id,
            "events": len(operation_logs.snapshot(limit=2_000)),
        },
        "models": model_registry.list(),
        "embedding_profiles": [
            {"alias": "local-bge", "model": settings.local_embedding_model, "configured": True},
            {
                "alias": "online",
                "model": settings.online_embedding_model,
                "configured": bool(os.getenv(settings.online_embedding_api_key_env)),
            },
        ],
        "reranker": "enabled" if settings.rerank_enabled else "disabled",
    }


@router.get("/companion/preferences", dependencies=[Depends(require_loopback)])
def get_companion_preferences(
    qq_user_id: str, session: Session = Depends(get_db)
) -> dict[str, object]:
    require_companion_owner(qq_user_id)
    item = session.scalar(
        select(CompanionPreference).where(
            CompanionPreference.scope_type == "qq_user",
            CompanionPreference.scope_id == qq_user_id,
        )
    )
    return preference_dict(item) if item is not None else _default_preference_dict(qq_user_id)


@router.put("/companion/preferences", dependencies=[Depends(require_loopback)])
def update_companion_preferences(
    payload: CompanionPreferenceUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    require_companion_owner(payload.qq_user_id)
    item = get_or_create_preference(session, payload.qq_user_id)
    if payload.companion_enabled is not None:
        item.companion_enabled = payload.companion_enabled
    if payload.support_mode is not None:
        item.support_mode = payload.support_mode
    if payload.memory_enabled is not None:
        item.memory_enabled = payload.memory_enabled
    if payload.safety_mode is not None:
        item.safety_mode = payload.safety_mode
    if payload.listening_enabled is not None:
        item.listening_enabled = payload.listening_enabled
    if payload.listening_silence_seconds is not None:
        item.listening_silence_seconds = payload.listening_silence_seconds
    if "analyzer_model_alias" in payload.model_fields_set:
        alias = (payload.analyzer_model_alias or "").strip()
        if alias:
            try:
                model_registry.profile(alias)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            item.analyzer_model_alias = alias
        else:
            item.analyzer_model_alias = None
    if payload.boundaries is not None:
        serialized = json.dumps(payload.boundaries, ensure_ascii=False)
        if _safe_relation_value(serialized) is None:
            raise HTTPException(400, "边界配置包含不允许保存的敏感内容")
        item.boundaries = serialized
    session.commit()
    session.refresh(item)
    return preference_dict(item)


def _listening_buffer_count(session: Session, qq_user_id: str) -> int:
    conversation = session.scalar(
        select(Conversation).where(
            Conversation.platform == "qq", Conversation.external_id == f"private:{qq_user_id}"
        )
    )
    if conversation is None:
        return 0
    buffer = session.scalar(
        select(CompanionListeningBuffer).where(
            CompanionListeningBuffer.conversation_id == conversation.id
        )
    )
    if buffer is None:
        return 0
    try:
        values = json.loads(buffer.fragments or "[]")
    except json.JSONDecodeError:
        return 0
    return len(values) if isinstance(values, list) else 0


@router.get("/companion/listening-buffer", dependencies=[Depends(require_loopback)])
def get_listening_buffer(qq_user_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    require_companion_owner(qq_user_id)
    preference = session.scalar(
        select(CompanionPreference).where(
            CompanionPreference.scope_type == "qq_user",
            CompanionPreference.scope_id == qq_user_id,
        )
    )
    return {
        "qq_user_id": qq_user_id,
        "listening_enabled": bool(preference and preference.listening_enabled),
        "listening_silence_seconds": int(
            getattr(preference, "listening_silence_seconds", 30) if preference else 30
        ),
        "fragment_count": _listening_buffer_count(session, qq_user_id),
    }


@router.delete("/companion/listening-buffer", dependencies=[Depends(require_loopback)])
def clear_listening_buffer(qq_user_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    require_companion_owner(qq_user_id)
    conversation = session.scalar(
        select(Conversation).where(
            Conversation.platform == "qq", Conversation.external_id == f"private:{qq_user_id}"
        )
    )
    deleted = 0
    if conversation is not None:
        buffer = session.scalar(
            select(CompanionListeningBuffer).where(
                CompanionListeningBuffer.conversation_id == conversation.id
            )
        )
        if buffer is not None:
            session.delete(buffer)
            deleted = 1
    session.commit()
    return {"cleared": True, "fragment_count": int(deleted)}


@router.get("/companion/strategy-guides", dependencies=[Depends(require_loopback)])
def get_strategy_guides(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [strategy_guide_dict(item) for item in list_strategy_guides(session)]


@router.put("/companion/strategy-guides/{strategy}", dependencies=[Depends(require_loopback)])
def put_strategy_guide(
    strategy: str, payload: StrategyGuideUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        item = update_strategy_guide(
            session, strategy, payload.prompt_text, payload.expected_version
        )
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return strategy_guide_dict(item)


@router.get("/companion/strategy-guides/{strategy}/revisions", dependencies=[Depends(require_loopback)])
def get_strategy_guide_revisions(
    strategy: str, session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    try:
        revisions = list_strategy_revisions(session, strategy)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return [
        {
            "strategy": item.strategy,
            "version": item.version,
            "prompt_text": item.prompt_text,
            "source": item.source,
            "created_at": item.created_at.isoformat(),
        }
        for item in revisions
    ]


@router.post("/companion/strategy-guides/{strategy}/rollback", dependencies=[Depends(require_loopback)])
def post_strategy_guide_rollback(
    strategy: str, payload: StrategyGuideRollback, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        item = rollback_strategy_guide(
            session, strategy, payload.revision_version, payload.expected_version
        )
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return strategy_guide_dict(item)


@router.post("/companion/strategy-guides/{strategy}/reset", dependencies=[Depends(require_loopback)])
def post_strategy_guide_reset(
    strategy: str, payload: StrategyGuideVersionRequest, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        item = reset_strategy_guide(session, strategy, payload.expected_version)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return strategy_guide_dict(item)


@router.get("/companion/relationships/{persona_key}", dependencies=[Depends(require_loopback)])
def get_companion_relationship(
    persona_key: str, qq_user_id: str, session: Session = Depends(get_db)
) -> dict[str, object]:
    require_companion_owner(qq_user_id)
    item = session.scalar(
        select(RelationshipProfile).where(
            RelationshipProfile.scope_id == qq_user_id,
            RelationshipProfile.persona_key == persona_key,
        )
    )
    persona_id = item.persona_id if item is not None else None
    return relationship_dict(item, qq_user_id, persona_id)


@router.get("/companion/relationships", dependencies=[Depends(require_loopback)])
def list_companion_relationships(
    qq_user_id: str, session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    require_companion_owner(qq_user_id)
    items = session.scalars(
        select(RelationshipProfile)
        .where(RelationshipProfile.scope_id == qq_user_id)
        .order_by(RelationshipProfile.persona_key)
    ).all()
    return [relationship_dict(item, qq_user_id, item.persona_id) for item in items]


@router.put("/companion/relationships/{persona_key}", dependencies=[Depends(require_loopback)])
def update_companion_relationship(
    persona_key: str, payload: RelationshipUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    require_companion_owner(payload.qq_user_id)
    item = session.scalar(
        select(RelationshipProfile).where(
            RelationshipProfile.scope_id == payload.qq_user_id,
            RelationshipProfile.persona_key == persona_key,
        )
    )
    if item is not None and payload.version != item.version:
        raise HTTPException(409, "关系资料已被更新，请刷新后重试")
    if item is None:
        if payload.version != 0:
            raise HTTPException(409, "关系资料版本不匹配")
        item = RelationshipProfile(
            scope_id=payload.qq_user_id,
            persona_key=persona_key,
            persona_id=None if persona_key == "default" else persona_key,
            version=1,
        )
        session.add(item)
    else:
        item.version += 1
    if payload.nickname is not None:
        item.nickname = _safe_relation_value(payload.nickname)
        if payload.nickname.strip() and item.nickname is None:
            raise HTTPException(400, "称呼包含不允许保存的敏感内容")
    if payload.shared_summary is not None:
        item.shared_summary = _safe_relation_value(payload.shared_summary) or ""
        if payload.shared_summary.strip() and not item.shared_summary:
            raise HTTPException(400, "关系摘要包含不允许保存的敏感内容")
    if payload.boundaries is not None:
        serialized = json.dumps(payload.boundaries, ensure_ascii=False)
        if _safe_relation_value(serialized) is None:
            raise HTTPException(400, "关系边界包含不允许保存的敏感内容")
        item.boundaries = serialized
    session.commit()
    session.refresh(item)
    return relationship_dict(item, payload.qq_user_id, item.persona_id)


@router.delete("/companion/relationships/{persona_key}", dependencies=[Depends(require_loopback)])
def delete_companion_relationship(
    persona_key: str,
    qq_user_id: str,
    version: int | None = None,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    require_companion_owner(qq_user_id)
    item = session.scalar(
        select(RelationshipProfile).where(
            RelationshipProfile.scope_id == qq_user_id,
            RelationshipProfile.persona_key == persona_key,
        )
    )
    if item is None:
        return {"deleted": False, "persona_key": persona_key}
    if version is not None and version != item.version:
        raise HTTPException(409, "关系资料已被更新，请刷新后重试")
    session.delete(item)
    session.commit()
    return {"deleted": True, "persona_key": persona_key}


@router.get("/companion/assessments", dependencies=[Depends(require_loopback)])
def list_companion_assessments(
    qq_user_id: str, limit: int = 50, session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    require_companion_owner(qq_user_id)
    limit = max(1, min(limit, 100))
    items = session.scalars(
        select(EmotionAssessment)
        .where(EmotionAssessment.scope_id == qq_user_id)
        .order_by(desc(EmotionAssessment.created_at))
        .limit(limit)
    ).all()
    return [assessment_dict(item, session) for item in items]


@router.post("/companion/assessments/{message_id}/correction", dependencies=[Depends(require_loopback)])
def correct_companion_assessment(
    message_id: str,
    payload: AssessmentCorrection,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    require_companion_owner(payload.qq_user_id)
    normalized_emotions: list[str] = []
    for label in payload.emotions:
        parsed, invalid = parse_emotion_labels_strict(label)
        if invalid or len(parsed) != 1:
            raise HTTPException(400, "包含未知情绪标签")
        if parsed[0] not in normalized_emotions:
            normalized_emotions.append(parsed[0])
    if not normalized_emotions:
        raise HTTPException(400, "至少需要一个合法情绪标签")
    item = session.scalar(
        select(EmotionAssessment).where(
            EmotionAssessment.user_message_id == message_id,
            EmotionAssessment.scope_id == payload.qq_user_id,
        )
    )
    if item is None:
        raise HTTPException(404, "找不到属于该 QQ Owner 的情绪分析")
    correction = {
        "emotions": normalized_emotions[:3],
        "support_need": payload.support_need,
        "note": payload.note,
    }
    item.correction = json.dumps(correction, ensure_ascii=False)
    session.commit()
    session.refresh(item)
    return assessment_dict(item, session)


@router.post("/companion/responses/{message_id}/feedback", dependencies=[Depends(require_loopback)])
def create_companion_feedback(
    message_id: str,
    payload: ResponseFeedbackCreate,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    require_companion_owner(payload.qq_user_id)
    assistant = session.get(Message, message_id)
    if assistant is None or assistant.role != "assistant":
        raise HTTPException(404, "助手回复不存在")
    conversation = session.get(Conversation, assistant.conversation_id)
    if conversation is None or conversation.external_id != f"private:{payload.qq_user_id}":
        raise HTTPException(403, "该回复不属于当前 QQ Owner 私聊")
    item = session.scalar(
        select(ResponseFeedback).where(ResponseFeedback.assistant_message_id == message_id)
    )
    if item is None:
        item = ResponseFeedback(
            assistant_message_id=message_id,
            scope_id=payload.qq_user_id,
            feedback=payload.feedback,
        )
        session.add(item)
    else:
        item.feedback = payload.feedback
    if payload.note is not None:
        item.correction = json.dumps({"note": payload.note}, ensure_ascii=False)
    session.commit()
    session.refresh(item)
    return feedback_dict(item)


@router.get("/companion/privacy/export", dependencies=[Depends(require_loopback)])
def export_companion_data(
    qq_user_id: str, session: Session = Depends(get_db)
) -> dict[str, object]:
    require_companion_owner(qq_user_id)
    preference = session.scalar(
        select(CompanionPreference).where(CompanionPreference.scope_id == qq_user_id)
    )
    relationships = session.scalars(
        select(RelationshipProfile).where(RelationshipProfile.scope_id == qq_user_id)
    ).all()
    assessments = session.scalars(
        select(EmotionAssessment)
        .where(EmotionAssessment.scope_id == qq_user_id)
        .order_by(desc(EmotionAssessment.created_at))
    ).all()
    feedback = session.scalars(
        select(ResponseFeedback)
        .where(ResponseFeedback.scope_id == qq_user_id)
        .order_by(desc(ResponseFeedback.created_at))
    ).all()
    safety = session.scalars(
        select(SafetyEvent)
        .where(SafetyEvent.scope_id == qq_user_id)
        .order_by(desc(SafetyEvent.created_at))
    ).all()
    return {
        "scope_id": qq_user_id,
        "preferences": preference_dict(preference) if preference else _default_preference_dict(qq_user_id),
        "relationships": [relationship_dict(item, qq_user_id, item.persona_id) for item in relationships],
        "assessments": [assessment_dict(item, session) for item in assessments],
        "feedback": [feedback_dict(item) for item in feedback],
        "safety_events": [safety_event_dict(item) for item in safety],
        "note": "导出不包含原始聊天正文、系统提示词、模型推理或凭据。",
    }


@router.delete("/companion/privacy/data", dependencies=[Depends(require_loopback)])
def delete_companion_data(
    payload: CompanionPrivacyDelete, session: Session = Depends(get_db)
) -> dict[str, object]:
    require_companion_owner(payload.qq_user_id)
    if payload.confirm_text != "删除陪伴数据":
        raise HTTPException(400, "请输入“删除陪伴数据”确认删除")
    categories = set(payload.categories)
    counts: dict[str, int] = {}
    targets = {
        "relationships": (RelationshipProfile, RelationshipProfile.scope_id == payload.qq_user_id),
        "assessments": (EmotionAssessment, EmotionAssessment.scope_id == payload.qq_user_id),
        "feedback": (ResponseFeedback, ResponseFeedback.scope_id == payload.qq_user_id),
        "safety": (SafetyEvent, SafetyEvent.scope_id == payload.qq_user_id),
        "preferences": (CompanionPreference, CompanionPreference.scope_id == payload.qq_user_id),
    }
    results: dict[str, dict[str, object]] = {}
    for name in categories:
        model, condition = targets[name]
        try:
            if name == "assessments":
                assessment_ids = session.scalars(
                    select(EmotionAssessment.id).where(condition)
                ).all()
                if assessment_ids:
                    session.execute(
                        delete(Job).where(
                            Job.id.in_(assessment_ids),
                            Job.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE,
                        )
                        )
            if name == "preferences":
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq",
                        Conversation.external_id == f"private:{payload.qq_user_id}",
                    )
                )
                if conversation is not None:
                    session.execute(
                        delete(CompanionListeningBuffer).where(
                            CompanionListeningBuffer.conversation_id == conversation.id
                        )
                    )
            count = session.query(model).where(condition).count()
            session.execute(delete(model).where(condition))
            session.commit()
            counts[name] = count
            results[name] = {"success": True, "count": count}
        except Exception as error:
            session.rollback()
            counts[name] = 0
            results[name] = {"success": False, "count": 0, "error": type(error).__name__}
    return {
        "deleted": all(item["success"] for item in results.values()),
        "counts": counts,
        "results": results,
    }


@router.get("/logs", dependencies=[Depends(require_loopback)])
def list_operation_logs(
    source: str | None = None,
    level: str | None = None,
    query: str | None = None,
    limit: int = 500,
    after_id: str | None = None,
) -> dict[str, object]:
    return {
        "session_id": operation_logs.session_id,
        "events": operation_logs.snapshot(
            source=source, level=level, query=query, limit=limit, after_id=after_id
        ),
    }


@router.get("/onebot/reply-settings", dependencies=[Depends(require_loopback)])
def get_qq_reply_settings(session: Session = Depends(get_db)) -> dict[str, object]:
    return qq_reply_settings(session)


@router.put("/onebot/reply-settings", dependencies=[Depends(require_loopback)])
def update_qq_reply_settings(
    payload: QQReplySettingsUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    if payload.chunked_output_enabled is None and payload.chunk_target_chars is None:
        raise HTTPException(422, "至少提供一个 QQ 回复设置")
    set_qq_reply_settings(
        session,
        enabled=payload.chunked_output_enabled,
        target_chars=payload.chunk_target_chars,
    )
    session.commit()
    return qq_reply_settings(session)


@router.get("/logs/active", dependencies=[Depends(require_loopback)])
def active_operation_logs() -> dict[str, object]:
    return {
        "session_id": operation_logs.session_id,
        "operations": operation_logs.active_operations(),
        "napcat": napcat_connector.public_status(),
        "onebot": onebot_manager.status(),
    }


@router.get("/logs/config", dependencies=[Depends(require_loopback)])
def get_log_config() -> dict[str, object]:
    return {"napcat": napcat_connector.public_status()}


@router.put("/logs/config", dependencies=[Depends(require_loopback)])
async def put_log_config(payload: NapCatLogConfig) -> dict[str, object]:
    try:
        return {"napcat": await napcat_connector.configure(payload.url, payload.token, payload.clear_token)}
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/logs/test-connection", dependencies=[Depends(require_loopback)])
async def test_log_connection(payload: NapCatLogTest) -> dict[str, object]:
    try:
        return await napcat_connector.test_connection(payload.url, payload.token)
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/logs/finalize", dependencies=[Depends(require_loopback)])
def finalize_logs() -> dict[str, object]:
    operation_logs.emit(
        source="system", kind="stopped", title="项目停止", message="收到停止请求，正在刷新日志"
    )
    operation_logs.flush()
    return {"session_id": operation_logs.session_id, "flushed": True}


@router.get("/logs/stream", dependencies=[Depends(require_loopback)])
async def stream_operation_logs(
    request: Request,
    source: str | None = None,
    level: str | None = None,
    query: str | None = None,
    after_id: str | None = None,
) -> StreamingResponse:
    subscriber = operation_logs.subscribe(asyncio.get_running_loop())
    initial = operation_logs.snapshot(source=source, level=level, query=query, limit=500, after_id=after_id)

    def frame(event_name: str, data: object) -> str:
        return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def events():
        try:
            for item in initial:
                yield frame("operation" if item.get("operation_id") else "log", item)
            yield frame(
                "status", {"napcat": napcat_connector.public_status(), "onebot": onebot_manager.status()}
            )
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(subscriber.queue.get(), timeout=15)
                    if source and item.get("source") != source:
                        continue
                    if level and item.get("level") != level:
                        continue
                    if query and query.casefold() not in json.dumps(item, ensure_ascii=False).casefold():
                        continue
                    yield frame("operation" if item.get("operation_id") else "log", item)
                    if subscriber.dropped:
                        dropped = subscriber.dropped
                        subscriber.dropped = 0
                        yield frame(
                            "log",
                            operation_logs.emit(
                                source="system",
                                level="warn",
                                kind="message",
                                title="日志订阅过慢",
                                message=f"客户端过慢，已丢弃 {dropped} 条最旧推送",
                                details={"dropped": dropped},
                            ),
                        )
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    yield frame(
                        "status",
                        {"napcat": napcat_connector.public_status(), "onebot": onebot_manager.status()},
                    )
        finally:
            operation_logs.unsubscribe(subscriber)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/models", dependencies=[Depends(require_loopback)])
def list_models(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return list_profiles(session)


@router.post("/models", dependencies=[Depends(require_loopback)])
def create_model_profile(
    payload: ModelProfileCreate, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        profile = create_profile(session, payload.model_dump(exclude={"api_key"}), payload.api_key)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return next(item for item in list_profiles(session) if item["alias"] == profile.alias)


@router.post("/models/test-connection", dependencies=[Depends(require_loopback)])
def test_model_connection(payload: ModelConnectionTest) -> dict[str, object]:
    existing_alias = None
    try:
        model_registry.profile(payload.alias)
        existing_alias = payload.alias
    except ValueError:
        pass
    try:
        return test_connection(
            payload.model_dump(exclude={"api_key"}), payload.api_key, existing_alias=existing_alias
        )
    except (RuntimeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


@router.put("/models/{alias}", dependencies=[Depends(require_loopback)])
def edit_model_profile(
    alias: str, payload: ModelProfileUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        profile = update_profile(
            session,
            alias,
            payload.model_dump(exclude_unset=True, exclude={"api_key", "clear_api_key"}),
            payload.api_key,
            payload.clear_api_key,
        )
    except KeyError as error:
        raise HTTPException(404, f"未知模型配置: {error.args[0]}") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return next(item for item in list_profiles(session) if item["alias"] == profile.alias)


@router.put("/models/{alias}/default", dependencies=[Depends(require_loopback)])
def make_default_model(alias: str, session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        profile = set_default_profile(session, alias)
    except KeyError as error:
        raise HTTPException(404, f"未知模型配置: {error.args[0]}") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return next(item for item in list_profiles(session) if item["alias"] == profile.alias)


@router.delete("/models/{alias}", dependencies=[Depends(require_loopback)])
def remove_model_profile(alias: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    try:
        delete_profile(session, alias)
    except KeyError as error:
        raise HTTPException(404, f"未知模型配置: {error.args[0]}") from error
    except PermissionError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {"deleted": True}


@router.post("/conversations")
def create_conversation(payload: ConversationCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    model_alias = payload.model_alias or model_registry.default_alias()
    model_registry.profile(model_alias)
    item = Conversation(title=payload.title, model_alias=model_alias)
    session.add(item)
    session.commit()
    session.refresh(item)
    return conversation_dict(item)


@router.get("/conversations")
def list_conversations(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    items = session.scalars(select(Conversation).order_by(Conversation.updated_at.desc())).all()
    return [conversation_dict(item) for item in items]


@router.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: str, session: Session = Depends(get_db)) -> list[dict[str, object]]:
    items = session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
    ).all()
    return [
        {
            "id": item.id,
            "sender_id": item.sender_id,
            "role": item.role,
            "content": item.content,
            "created_at": item.created_at.isoformat(),
        }
        for item in items
    ]


@router.delete("/conversations/{conversation_id}", dependencies=[Depends(require_loopback)])
def remove_conversation(conversation_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "会话不存在")
    if conversation.platform != "web":
        raise HTTPException(403, "仅允许删除网页会话")
    session.execute(
        update(Job).where(Job.conversation_id == conversation_id).values(conversation_id=None)
    )
    session.execute(
        update(Confirmation)
        .where(Confirmation.conversation_id == conversation_id)
        .values(conversation_id=None)
    )
    session.execute(
        update(Artifact).where(Artifact.conversation_id == conversation_id).values(conversation_id=None)
    )
    session.execute(
        delete(CompanionListeningBuffer).where(
            CompanionListeningBuffer.conversation_id == conversation_id
        )
    )
    session.execute(delete(ToolRun).where(ToolRun.conversation_id == conversation_id))
    session.delete(conversation)
    session.commit()
    return {"deleted": True, "conversation_id": conversation_id}


@router.put("/conversations/{conversation_id}/model")
def switch_model(
    conversation_id: str, payload: ModelSwitch, session: Session = Depends(get_db)
) -> dict[str, object]:
    model_registry.profile(payload.model_alias)
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "会话不存在")
    conversation.model_alias = payload.model_alias
    session.commit()
    return conversation_dict(conversation)


@router.put("/conversations/{conversation_id}/persona")
def switch_persona(
    conversation_id: str, payload: PersonaSwitch, session: Session = Depends(get_db)
) -> dict[str, object]:
    persona_store = get_persona_store()
    entries = persona_store.sync_db(session)
    session.flush()
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "会话不存在")
    if payload.persona_id is not None and not (
        entries.get(payload.persona_id) is not None
        and entries[payload.persona_id].is_active
    ):
        raise HTTPException(404, "人格不存在")
    conversation.persona_id = payload.persona_id
    session.commit()
    session.refresh(conversation)
    return conversation_dict(conversation)


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, session: Session = Depends(get_db)) -> StreamingResponse:
    conversation = session.get(Conversation, payload.conversation_id) if payload.conversation_id else None
    if conversation is None:
        conversation = Conversation(
            owner_id=payload.sender_id,
            model_alias=model_registry.default_alias(),
        )
        session.add(conversation)
        session.commit()
        session.refresh(conversation)

    async def events():
        async for event in chat_service.stream(session, conversation, payload.sender_id, payload.message):
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/knowledge-bases")
def create_knowledge_base(
    payload: KnowledgeBaseCreate, session: Session = Depends(get_db)
) -> dict[str, object]:
    if payload.embedding_profile not in {"local-bge", "online"}:
        raise HTTPException(400, "embedding_profile 只能是 local-bge 或 online")
    if payload.embedding_profile == "online":
        try:
            get_embedding_provider("online")
        except RuntimeError as error:
            raise HTTPException(400, str(error)) from error
    item = KnowledgeBase(name=payload.name, embedding_profile=payload.embedding_profile)
    session.add(item)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(409, "知识库名称已存在") from error
    session.refresh(item)
    return {"id": item.id, "name": item.name, "embedding_profile": item.embedding_profile}


@router.get("/knowledge-bases")
def list_knowledge_bases(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    items = session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())).all()
    return [{"id": item.id, "name": item.name, "embedding_profile": item.embedding_profile} for item in items]


@router.put("/knowledge-bases/{knowledge_base_id}/embedding")
def switch_knowledge_base_embedding(
    knowledge_base_id: str, payload: EmbeddingSwitch, session: Session = Depends(get_db)
) -> dict[str, object]:
    item = session.get(KnowledgeBase, knowledge_base_id)
    if item is None:
        raise HTTPException(404, "知识库不存在")
    if payload.embedding_profile not in {"local-bge", "online"}:
        raise HTTPException(400, "embedding_profile 只能是 local-bge 或 online")
    try:
        get_embedding_provider(payload.embedding_profile)
    except RuntimeError as error:
        raise HTTPException(400, str(error)) from error
    job = create_job(
        "knowledge_base_reindex",
        {"knowledge_base_id": item.id, "embedding_profile": payload.embedding_profile},
    )
    return {"task_id": job.id, "status": job.status, "active_embedding_profile": item.embedding_profile}


@router.delete("/knowledge-bases/{knowledge_base_id}")
def delete_knowledge_base(knowledge_base_id: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    item = session.get(KnowledgeBase, knowledge_base_id)
    if item is None:
        raise HTTPException(404, "知识库不存在")
    document_paths = [
        Path(path)
        for path in session.scalars(
            select(Document.path).where(Document.knowledge_base_id == item.id)
        )
    ]
    vector_store.delete_collection(safe_collection_name("docs", item.id, item.embedding_profile))
    session.execute(text("DELETE FROM chunk_fts WHERE knowledge_base_id=:kb"), {"kb": item.id})
    session.delete(item)
    session.commit()
    for path in document_paths:
        if path.is_file() and path.parent.resolve() == settings.upload_path.resolve():
            path.unlink(missing_ok=True)
    return {"deleted": True}


@router.post("/documents/{knowledge_base_id}")
async def upload_document(
    knowledge_base_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base is None:
        raise HTTPException(404, "知识库不存在")
    filename = Path(file.filename or "upload").name
    if Path(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(400, "支持 TXT、Markdown、HTML、PDF 和 DOCX")
    content = await file.read(30 * 1024 * 1024 + 1)
    if len(content) > 30 * 1024 * 1024:
        raise HTTPException(413, "单文件不能超过 30 MiB")
    digest = hashlib.sha256(content).hexdigest()
    destination = settings.upload_path / f"{digest[:12]}-{filename}"
    destination.write_bytes(content)
    document = Document(
        knowledge_base_id=knowledge_base.id,
        filename=filename,
        path=str(destination.resolve()),
        sha256=digest,
    )
    session.add(document)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(409, "该知识库中已存在相同内容的文档") from error
    session.refresh(document)
    job = create_job("document_index", {"document_id": document.id})
    return {"document_id": document.id, "status": document.status, "task_id": job.id}


@router.get("/documents")
def list_documents(
    knowledge_base_id: str | None = None, session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    query = select(Document).order_by(Document.created_at.desc())
    if knowledge_base_id:
        query = query.where(Document.knowledge_base_id == knowledge_base_id)
    return [
        {
            "id": item.id,
            "knowledge_base_id": item.knowledge_base_id,
            "filename": item.filename,
            "status": item.status,
            "error": item.error,
        }
        for item in session.scalars(query)
    ]


@router.post("/documents/{document_id}/reindex")
def reindex_document(document_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    item = session.get(Document, document_id)
    if item is None:
        raise HTTPException(404, "文档不存在")
    if item.status in {"queued", "indexing"}:
        raise HTTPException(409, "文档已在索引队列中")

    previous_status = item.status
    previous_error = item.error
    item.status = "queued"
    item.error = None
    session.commit()
    try:
        job = create_job("document_index", {"document_id": item.id})
    except Exception:
        item.status = previous_status
        item.error = previous_error
        session.commit()
        raise
    return {"document_id": item.id, "status": item.status, "task_id": job.id}


@router.delete("/documents/{document_id}")
def delete_document(document_id: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    item = session.get(Document, document_id)
    if item is None:
        raise HTTPException(404, "文档不存在")
    knowledge_base = session.get(KnowledgeBase, item.knowledge_base_id)
    chunk_ids = list(session.scalars(select(Chunk.id).where(Chunk.document_id == item.id)))
    if knowledge_base is not None:
        vector_store.delete_ids(
            safe_collection_name("docs", knowledge_base.id, knowledge_base.embedding_profile),
            chunk_ids,
        )
    session.execute(
        text("DELETE FROM chunk_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=:doc)"),
        {"doc": item.id},
    )
    path = Path(item.path)
    session.delete(item)
    session.commit()
    if path.is_file() and path.parent.resolve() == settings.upload_path.resolve():
        path.unlink(missing_ok=True)
    return {"deleted": True}


@router.get("/memories")
def list_memories(
    scope: str = "all",
    user_id: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    query = select(Memory).order_by(Memory.last_seen_at.desc())
    if scope == "global":
        query = query.where(Memory.scope_type == "global", Memory.user_id.is_(None))
    elif scope == "user":
        query = query.where(Memory.scope_type == "user")
        if user_id:
            query = query.where(Memory.user_id == user_id)
    elif user_id:
        query = query.where(Memory.user_id == user_id)
    if status:
        query = query.where(Memory.status == status)
    if keyword:
        query = query.where(Memory.content.contains(keyword))
    return [
        {
            "id": item.id,
            "scope_type": item.scope_type,
            "user_id": item.user_id,
            "fact_key": item.fact_key,
            "content": item.content,
            "status": item.status,
            "source_message_id": item.source_message_id,
            "created_at": item.created_at.isoformat(),
            "last_seen_at": item.last_seen_at.isoformat(),
        }
    for item in session.scalars(query)
]


@router.get("/memory-center", dependencies=[Depends(require_loopback)])
def list_memory_center(
    memory_type: Literal["all", "fact", "relationship"] = "all",
    scope_type: Literal["all", "global", "user", "group"] = "all",
    scope_id: str | None = None,
    persona_key: str | None = None,
    status: Literal["active", "archived", "all"] = "active",
    keyword: str | None = None,
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    """Return the two internal memory stores through one management surface.

    Relationship profiles are deliberately restricted to currently enabled QQ
    Owners.  They have no archive state, so an ``archived`` request never
    exposes them.  No runtime recall or extraction path uses this facade.
    """
    # Persona files are authoritative; refresh the lightweight index before
    # resolving relationship display names.
    get_persona_store().sync_db(session)
    session.flush()
    rows: list[dict[str, object]] = []
    keyword_text = (keyword or "").strip()
    normalized_keyword = keyword_text.casefold()

    if memory_type in {"all", "fact"}:
        fact_query = select(Memory).order_by(Memory.last_seen_at.desc())
        if scope_type == "global":
            fact_query = fact_query.where(Memory.scope_type == "global", Memory.user_id.is_(None))
            if scope_id:
                fact_query = fact_query.where(Memory.user_id == scope_id)
        elif scope_type in {"user", "group"}:
            fact_query = fact_query.where(Memory.scope_type == scope_type)
            if scope_id:
                fact_query = fact_query.where(Memory.user_id == scope_id)
        elif scope_id:
            fact_query = fact_query.where(Memory.user_id == scope_id)
        if status != "all":
            fact_query = fact_query.where(Memory.status == status)
        if keyword_text:
            fact_query = fact_query.where(
                or_(
                    Memory.fact_key.contains(keyword_text),
                    Memory.content.contains(keyword_text),
                )
            )
        for item in session.scalars(fact_query):
            updated_at = item.last_seen_at.isoformat()
            rows.append(
                {
                    "memory_type": "fact",
                    "id": item.id,
                    "scope_type": item.scope_type,
                    "scope_id": item.user_id,
                    "user_id": item.user_id,
                    "fact_key": item.fact_key,
                    "content": item.content,
                    "status": item.status,
                    "source_message_id": item.source_message_id,
                    "created_at": item.created_at.isoformat(),
                    "last_seen_at": updated_at,
                    "updated_at": updated_at,
                }
            )

    if memory_type in {"all", "relationship"} and status != "archived" and scope_type in {"all", "user"}:
        owner_query = select(AdminIdentity.external_id).where(
            AdminIdentity.platform == "qq",
            AdminIdentity.enabled.is_(True),
        )
        owner_ids = set(session.scalars(owner_query))
        if scope_id:
            owner_ids &= {scope_id}
        if owner_ids:
            relationship_query = select(RelationshipProfile).where(
                RelationshipProfile.scope_id.in_(owner_ids)
            )
            if persona_key:
                relationship_query = relationship_query.where(
                    RelationshipProfile.persona_key == persona_key
                )
            relationships = list(session.scalars(relationship_query))
            persona_ids = {item.persona_id for item in relationships if item.persona_id}
            persona_names = {
                item.id: item.name
                for item in session.scalars(select(Persona).where(Persona.id.in_(persona_ids)))
            } if persona_ids else {}
            for item in relationships:
                try:
                    boundaries: object = json.loads(item.boundaries or "{}")
                except json.JSONDecodeError:
                    boundaries = {}
                content_items = relationship_content_items(
                    item.nickname, item.shared_summary, boundaries
                )
                persona_name = persona_names.get(item.persona_id or "", item.persona_key)
                if normalized_keyword:
                    searchable = " ".join(
                        (
                            item.persona_key,
                            persona_name,
                            item.nickname or "",
                            item.shared_summary or "",
                            json.dumps(boundaries, ensure_ascii=False),
                            " ".join(
                                f"{entry['label']} {entry['content']}"
                                for entry in content_items
                            ),
                        )
                    ).casefold()
                    if normalized_keyword not in searchable:
                        continue
                updated_at = item.updated_at.isoformat()
                rows.append(
                    {
                        "memory_type": "relationship",
                        "id": item.id,
                        "scope_type": "user",
                        "scope_id": item.scope_id,
                        "user_id": item.scope_id,
                        "persona_key": item.persona_key,
                        "persona_id": item.persona_id,
                        "persona_name": persona_name,
                        "nickname": item.nickname,
                        "shared_summary": item.shared_summary,
                        "boundaries": boundaries,
                        "content_items": content_items,
                        "version": item.version,
                        "status": "active",
                        "created_at": item.created_at.isoformat(),
                        "last_seen_at": None,
                        "updated_at": updated_at,
                    }
                )

    rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return rows


@router.post("/memories", dependencies=[Depends(require_loopback)])
def create_memory(payload: MemoryCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    if payload.scope_type not in {"global", "user"}:
        raise HTTPException(400, "scope_type 只能是 global 或 user")
    if payload.scope_type == "global" and payload.user_id is not None:
        raise HTTPException(400, "全局记忆不能绑定 user_id")
    if payload.scope_type == "user" and not payload.user_id:
        raise HTTPException(400, "用户记忆必须提供 user_id")
    try:
        item = (
            MemoryService(session).upsert_global(payload.fact_key, payload.content)
            if payload.scope_type == "global"
            else MemoryService(session).upsert(payload.user_id or "", payload.fact_key, payload.content)
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {
        "id": item.id,
        "scope_type": item.scope_type,
        "user_id": item.user_id,
        "fact_key": item.fact_key,
        "content": item.content,
        "status": item.status,
    }


@router.put("/memories/{memory_id}", dependencies=[Depends(require_loopback)])
def update_memory(
    memory_id: str, payload: MemoryUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        item = MemoryService(session).update(memory_id, payload.content)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"id": item.id, "content": item.content, "status": item.status}


@router.post("/memories/{memory_id}/archive", dependencies=[Depends(require_loopback)])
def archive_memory(memory_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        item = MemoryService(session).archive(memory_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return {"id": item.id, "status": item.status}


@router.delete("/memories/{memory_id}", dependencies=[Depends(require_loopback)])
def delete_memory(memory_id: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    try:
        MemoryService(session).delete(memory_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return {"deleted": True}


@router.post("/memories/{memory_id}/restore", dependencies=[Depends(require_loopback)])
def restore_memory(memory_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        item = MemoryService(session).restore(memory_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return {"id": item.id, "status": item.status}


@router.post("/manga/search")
async def search_manga(payload: MangaSearchRequest) -> dict[str, object]:
    try:
        results = await asyncio.wait_for(asyncio.to_thread(manga_service.search, payload.query), timeout=45)
        return {"results": results}
    except TimeoutError as error:
        raise HTTPException(504, "漫画搜索超时") from error
    except Exception as error:
        raise HTTPException(502, f"漫画搜索失败: {type(error).__name__}: {error}") from error


@router.post("/manga/download")
def request_manga_download(payload: MangaDownloadRequest) -> dict[str, object]:
    try:
        job = create_manga_download_job(
            payload.album_id,
            payload.requester_id,
            payload.conversation_id,
        )
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {
        "task": job_dict(job),
        "message": "任务已创建，无需二次确认。",
    }


@router.post("/confirmations/bulk-delete", dependencies=[Depends(require_loopback)])
def bulk_delete_confirmations(
    payload: BulkDeleteTokens, session: Session = Depends(get_db)
) -> dict[str, int]:
    tokens = list(dict.fromkeys(payload.tokens))
    items = session.scalars(select(Confirmation).where(Confirmation.token.in_(tokens))).all()
    if len(items) != len(tokens):
        raise HTTPException(404, "部分确认记录不存在，未删除任何记录")
    for item in items:
        session.delete(item)
    session.commit()
    return {"deleted": len(items)}


@router.post("/confirmations/{token}")
def confirm(token: str, payload: ConfirmationResolve) -> dict[str, object]:
    try:
        item, job = resolve_confirmation(token, payload.requester_id, payload.approve)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    payload_data = json.loads(item.payload)
    return {
        "confirmation_status": item.status,
        "task": job_dict(job) if job else None,
        "result": payload_data.get("result"),
        "error": payload_data.get("error"),
    }


@router.get("/confirmations")
def list_confirmations(
    status: str = "pending", session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    query = select(Confirmation).order_by(Confirmation.created_at.desc())
    if status != "all":
        query = query.where(Confirmation.status == status)
    items = session.scalars(query).all()
    return [
        {
            "token": item.token,
            "requester_id": item.requester_id,
            "action": item.action,
            "payload": json.loads(item.payload),
            "status": item.status,
            "expires_at": utc_isoformat(item.expires_at),
        }
        for item in items
    ]


@router.get("/tasks")
def list_tasks(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [
        job_dict(item)
        for item in session.scalars(
            select(Job)
            .where(Job.type != COMPANION_ANALYSIS_RETRY_JOB_TYPE)
            .order_by(Job.created_at.desc())
        )
    ]


@router.post("/tasks/{job_id}/cancel")
def cancel_task(job_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    item = session.get(Job, job_id)
    if item is None:
        raise HTTPException(404, "任务不存在")
    if item.status == "queued":
        item.status = "cancelled"
    elif item.status == "running":
        item.cancel_requested = True
    session.commit()
    return job_dict(item)


@router.post("/tasks/bulk-delete", dependencies=[Depends(require_loopback)])
def bulk_delete_tasks(payload: BulkDeleteJobIds, session: Session = Depends(get_db)) -> dict[str, int]:
    ids = list(dict.fromkeys(payload.ids))
    items = session.scalars(select(Job).where(Job.id.in_(ids))).all()
    if len(items) != len(ids):
        raise HTTPException(404, "部分任务记录不存在，未删除任何记录")
    active = [item.id for item in items if item.status not in {"succeeded", "failed", "cancelled"}]
    if active:
        raise HTTPException(409, "排队中或运行中的任务不能删除，未删除任何记录")
    for item in items:
        session.delete(item)
    session.commit()
    return {"deleted": len(items)}


@router.get("/tasks/{job_id}/artifact")
def download_artifact(job_id: str, session: Session = Depends(get_db)) -> FileResponse:
    item = session.get(Job, job_id)
    if item is None or item.status != "succeeded" or not item.result:
        raise HTTPException(404, "任务产物不存在")
    result = json.loads(item.result)
    path = Path(result.get("path", "")).resolve()
    if settings.download_path.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "任务产物路径无效")
    return FileResponse(path, filename=path.name)


@router.delete("/tasks/{job_id}/artifact", dependencies=[Depends(require_loopback)])
def delete_task_artifact(job_id: str) -> dict[str, object]:
    try:
        return delete_manga_artifact(job_id, "local-owner")
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


def _extension_response(item: ExtensionPackage) -> dict[str, object]:
    return package_dict(item)


@router.get("/tools")
def list_tools(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in list_packages(session, "tool"):
        row = _extension_response(item)
        row["recent_runs"] = [
            {
                "id": run.id,
                "conversation_id": run.conversation_id,
                "status": run.status,
                "created_at": run.created_at.isoformat(),
            }
            for run in session.scalars(
                select(ToolRun)
                .where(ToolRun.tool_name == item.name)
                .order_by(ToolRun.created_at.desc())
                .limit(10)
            )
        ]
        result.append(row)
    return result


@router.post("/tools/import", dependencies=[Depends(require_loopback)])
async def import_tool(file: UploadFile = File(...), session: Session = Depends(get_db)) -> dict[str, object]:
    content = await file.read(20 * 1024 * 1024 + 1)
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, "Tool 包不能超过 20 MiB")
    try:
        item = import_zip(session, "tool", content, Path(file.filename or "tool.zip").name)
    except (ExtensionError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    return _extension_response(item)


@router.post("/tools/import/github", dependencies=[Depends(require_loopback)])
def import_tool_github(
    payload: ExtensionGithubImport, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        item = import_github(session, "tool", payload.url)
    except Exception as error:
        raise HTTPException(400, f"GitHub Tool 导入失败: {type(error).__name__}: {error}") from error
    return _extension_response(item)


@router.get("/skills")
def list_skills(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [_extension_response(item) for item in list_packages(session, "skill")]


@router.post("/skills/import", dependencies=[Depends(require_loopback)])
async def import_skill(file: UploadFile = File(...), session: Session = Depends(get_db)) -> dict[str, object]:
    content = await file.read(20 * 1024 * 1024 + 1)
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, "Skill 包不能超过 20 MiB")
    try:
        item = import_zip(session, "skill", content, Path(file.filename or "skill.zip").name)
    except (ExtensionError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    return _extension_response(item)


@router.post("/skills/import/github", dependencies=[Depends(require_loopback)])
def import_skill_github(
    payload: ExtensionGithubImport, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        item = import_github(session, "skill", payload.url)
    except Exception as error:
        raise HTTPException(400, f"GitHub Skill 导入失败: {type(error).__name__}: {error}") from error
    return _extension_response(item)


def _set_extension_state(
    kind: str, name: str, payload: ExtensionState, session: Session
) -> dict[str, object]:
    try:
        item = set_package_state(
            session,
            kind,
            name,
            enabled=payload.enabled,
            access_policy=payload.access_policy,
        )
    except ExtensionError as error:
        status_code = 404 if str(error) == "扩展不存在" else 400
        raise HTTPException(status_code, str(error)) from error
    session.commit()
    session.refresh(item)
    return _extension_response(item)


@router.get("/tools/{name}")
def get_tool(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    item = session.scalar(
        select(ExtensionPackage).where(
            ExtensionPackage.kind == "tool", ExtensionPackage.name == name
        )
    )
    if item is None:
        raise HTTPException(404, "Tool 不存在")
    return _extension_response(item)


@router.post("/tools/{name}/enable", dependencies=[Depends(require_loopback)])
def enable_tool(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _set_extension_state("tool", name, ExtensionState(enabled=True), session)


@router.post("/tools/{name}/disable", dependencies=[Depends(require_loopback)])
def disable_tool(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _set_extension_state("tool", name, ExtensionState(enabled=False), session)


@router.delete("/tools/{name}", dependencies=[Depends(require_loopback)])
def delete_tool(name: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    return _delete_extension("tool", name, session)


@router.post("/skills/{name}/enable", dependencies=[Depends(require_loopback)])
def enable_skill(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _set_extension_state("skill", name, ExtensionState(enabled=True), session)


@router.get("/skills/{name}")
def get_skill(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    item = session.scalar(
        select(ExtensionPackage).where(
            ExtensionPackage.kind == "skill", ExtensionPackage.name == name
        )
    )
    if item is None:
        raise HTTPException(404, "Skill 不存在")
    return _extension_response(item)


@router.post("/skills/{name}/disable", dependencies=[Depends(require_loopback)])
def disable_skill(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _set_extension_state("skill", name, ExtensionState(enabled=False), session)


@router.delete("/skills/{name}", dependencies=[Depends(require_loopback)])
def delete_skill(name: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    return _delete_extension("skill", name, session)


def _delete_extension(kind: str, name: str, session: Session) -> dict[str, bool]:
    try:
        delete_package(session, kind, name)
    except ExtensionError as error:
        status_code = 404 if str(error) == "扩展不存在" else 400
        raise HTTPException(status_code, str(error)) from error
    session.commit()
    return {"deleted": True}


@router.get("/personas")
def list_personas(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    persona_store = get_persona_store()
    entries = persona_store.sync_db(session)
    session.commit()
    rows = list(session.scalars(select(Persona).order_by(Persona.name)))
    result = [persona_dict(item, entries.get(item.id)) for item in rows]
    indexed = {item.id for item in rows}
    result.extend(persona_dict(entry) for persona_id, entry in entries.items() if persona_id not in indexed)
    result.sort(key=lambda item: str(item.get("name", "")).casefold())
    return result


@router.post("/personas", dependencies=[Depends(require_loopback)])
def create_persona(payload: PersonaCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    persona_store = get_persona_store()
    entries = persona_store.sync_db(session)
    normalized_name = payload.name.strip()
    if any(item.name.casefold() == normalized_name.casefold() for item in entries.values()):
        raise HTTPException(409, "人格名称已存在")
    existing_ids = set(entries)
    existing_ids.update(session.scalars(select(Persona.id)))
    persona_id = persona_store.new_id(normalized_name, existing_ids)
    try:
        envelope = persona_store.envelope_for(persona_id, normalized_name, payload.card)
        previous = persona_store.write(envelope)
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error
    item = Persona(
        id=persona_id,
        name=normalized_name,
        raw_prompt="",
        card_json="{}",
        status="active",
        card_version=envelope.card_version,
    )
    session.add(item)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        persona_store.restore(persona_id, previous)
        raise HTTPException(409, "人格名称已存在") from error
    except Exception:
        session.rollback()
        persona_store.restore(persona_id, previous)
        raise
    session.refresh(item)
    return persona_dict(item, persona_store.load(persona_id))


@router.put("/personas/{persona_id}", dependencies=[Depends(require_loopback)])
def update_persona(
    persona_id: str, payload: PersonaUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    persona_store = get_persona_store()
    entries = persona_store.sync_db(session)
    session.flush()
    item = session.get(Persona, persona_id)
    document = entries.get(persona_id) or persona_store.load(persona_id)
    if item is None and document is None:
        raise HTTPException(404, "人格不存在")
    current_name = document.name if document is not None else item.name  # type: ignore[union-attr]
    current_card = document.card if document is not None else None
    if payload.card is None and current_card is None:
        raise HTTPException(400, "无效人格必须同时提交完整结构化角色卡")
    new_name = (payload.name or current_name).strip()
    if not new_name:
        raise HTTPException(400, "人格名称不能为空")
    for other in entries.values():
        if other.id != persona_id and other.name.casefold() == new_name.casefold():
            raise HTTPException(409, "人格名称已存在")
    if item is None:
        item = Persona(id=persona_id, name=new_name, raw_prompt="", card_json="{}")
        session.add(item)
    current_version = document.card_version if document is not None else getattr(item, "card_version", 0) or 0
    next_version = current_version + 1
    try:
        envelope = persona_store.envelope_for(
            persona_id,
            new_name,
            payload.card or current_card,  # type: ignore[arg-type]
            card_version=next_version,
            sources=document.envelope.sources if document and document.envelope else [],
            adaptation=document.envelope.adaptation if document and document.envelope else "",
        )
        previous = persona_store.write(envelope)
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error
    item.name = new_name
    item.raw_prompt = ""
    item.card_json = "{}"
    item.status = "active"
    item.card_version = next_version
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        persona_store.restore(persona_id, previous)
        raise HTTPException(409, "人格名称已存在") from error
    except Exception:
        session.rollback()
        persona_store.restore(persona_id, previous)
        raise
    session.refresh(item)
    return persona_dict(item, persona_store.load(persona_id))


@router.delete("/personas/{persona_id}", dependencies=[Depends(require_loopback)])
def delete_persona(persona_id: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    persona_store = get_persona_store()
    persona_store.sync_db(session)
    session.flush()
    item = session.get(Persona, persona_id)
    document = persona_store.load(persona_id)
    if item is None and document is None:
        raise HTTPException(404, "人格不存在")
    session.query(Conversation).filter(Conversation.persona_id == persona_id).update(
        {Conversation.persona_id: None}, synchronize_session=False
    )
    if item is not None:
        session.delete(item)
    previous: bytes | None = None
    try:
        previous = persona_store.delete(persona_id)
        session.commit()
    except ValueError as error:
        session.rollback()
        raise HTTPException(400, str(error)) from error
    except Exception:
        session.rollback()
        if previous is not None:
            persona_store.restore(persona_id, previous)
        raise
    return {"deleted": True}


@router.get("/admins")
def list_admins(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    rows = [{"id": "local-owner", "platform": "local", "external_id": "local-owner", "enabled": True}]
    rows.extend(
        admin_dict(item)
        for item in session.scalars(select(AdminIdentity).order_by(AdminIdentity.external_id))
    )
    return rows


@router.get("/identities")
def list_identities(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return list_admins(session)


@router.post("/admins", dependencies=[Depends(require_loopback)])
def create_admin(payload: AdminCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    if payload.external_id == "local-owner":
        raise HTTPException(400, "local-owner 已永久存在")
    item = AdminIdentity(
        platform="qq",
        external_id=payload.external_id,
        display_name=payload.display_name,
        created_by="local-owner",
        enabled=True,
    )
    session.add(item)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(409, "该 QQ 已经是 Owner") from error
    session.refresh(item)
    return admin_dict(item)


@router.delete("/admins/{external_id}", dependencies=[Depends(require_loopback)])
def delete_admin(external_id: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    if external_id == "local-owner":
        raise HTTPException(400, "local-owner 不可删除")
    item = session.scalar(select(AdminIdentity).where(AdminIdentity.external_id == external_id))
    if item is None:
        raise HTTPException(404, "Owner 不存在")
    session.delete(item)
    session.commit()
    return {"deleted": True}


@router.get("/artifacts")
def list_artifacts(
    owner_id: str | None = None, session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    query = select(Artifact).order_by(Artifact.created_at.desc())
    if owner_id:
        query = query.where(Artifact.owner_id == owner_id)
    return [
        {
            "id": item.id,
            "owner_id": item.owner_id,
            "conversation_id": item.conversation_id,
            "filename": item.filename,
            "size": item.size,
            "sha256": item.sha256,
            "created_at": item.created_at.isoformat(),
        }
        for item in session.scalars(query)
    ]


@router.get("/artifacts/{artifact_id}/download")
def download_generated_artifact(artifact_id: str, session: Session = Depends(get_db)) -> FileResponse:
    item = session.get(Artifact, artifact_id)
    if item is None:
        raise HTTPException(404, "产物不存在")
    path = Path(item.path).resolve()
    root = (settings.workspace_path / "generated").resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "产物路径无效")
    return FileResponse(path, filename=item.filename, media_type=item.content_type)
