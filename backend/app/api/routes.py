from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, desc, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.chat_routes import router as chat_router
from app.api.conversation_routes import router as conversation_router
from app.api.dependencies import require_loopback
from app.api.extension_routes import router as extension_router
from app.api.knowledge_routes import router as knowledge_router
from app.api.mcp_routes import router as mcp_router
from app.api.memory_routes import router as memory_router
from app.api.model_routes import router as model_router
from app.api.monitoring_routes import router as monitoring_router
from app.api.onebot import onebot_manager
from app.api.task_routes import router as task_router
from app.core.config import get_settings
from app.db.models import (
    AdminIdentity,
    Artifact,
    CompanionListeningBuffer,
    CompanionPreference,
    Conversation,
    EmotionAssessment,
    Job,
    Message,
    Persona,
    RelationshipProfile,
    ResponseFeedback,
    SafetyEvent,
)
from app.db.session import get_db
from app.services.companion import (
    _safe_relation_value,
    assessment_dict,
    feedback_dict,
    get_or_create_preference,
    parse_emotion_labels_strict,
    preference_dict,
    relationship_dict,
    safety_event_dict,
)
from app.services.jobs import (
    COMPANION_ANALYSIS_RETRY_JOB_TYPE,
    job_worker,
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
from app.services.time_context import utc_isoformat

router = APIRouter()
router.include_router(monitoring_router)
router.include_router(model_router)
router.include_router(conversation_router)
router.include_router(chat_router)
router.include_router(extension_router)
router.include_router(task_router)
router.include_router(knowledge_router)
router.include_router(memory_router)
router.include_router(mcp_router)
settings = get_settings()


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
        "timezone": settings.personal_agent_timezone,
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
            "created_at": utc_isoformat(item.created_at),
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
        item = Persona(id=persona_id, name=new_name)
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
        "created_at": utc_isoformat(item.created_at),
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
