from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.api.dependencies import require_loopback
from app.db.models import (
    Artifact,
    CompanionListeningBuffer,
    Confirmation,
    Conversation,
    Job,
    Message,
    MessageAttachment,
    ToolRun,
)
from app.db.session import get_db
from app.services.chat_attachments import attachment_dict, stage_deletions
from app.services.models import model_registry
from app.services.persona_store import get_persona_store
from app.services.time_context import utc_isoformat

router = APIRouter()


class ConversationCreate(BaseModel):
    title: str = "新会话"
    model_alias: str | None = None


class ModelSwitch(BaseModel):
    model_alias: str


class PersonaSwitch(BaseModel):
    persona_id: str | None = None


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
        "summary_format_version": item.summary_format_version,
        "created_at": utc_isoformat(item.created_at),
        "updated_at": utc_isoformat(item.updated_at),
    }


@router.post("/conversations")
def create_conversation(
    payload: ConversationCreate, session: Session = Depends(get_db)
) -> dict[str, object]:
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
def list_messages(
    conversation_id: str, session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    items = session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
    ).all()
    return [
        {
            "id": item.id,
            "sender_id": item.sender_id,
            "role": item.role,
            "content": item.content,
            "created_at": utc_isoformat(item.created_at),
            "attachments": [attachment_dict(attachment) for attachment in item.attachments],
        }
        for item in items
    ]


@router.delete("/conversations/{conversation_id}", dependencies=[Depends(require_loopback)])
def remove_conversation(
    conversation_id: str, session: Session = Depends(get_db)
) -> dict[str, object]:
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
    attachments = session.scalars(
        select(MessageAttachment)
        .join(Message, Message.id == MessageAttachment.message_id)
        .where(Message.conversation_id == conversation_id)
    ).all()
    deletion_batch = stage_deletions(attachments)
    session.delete(conversation)
    try:
        session.commit()
    except Exception:
        session.rollback()
        deletion_batch.restore()
        raise
    deletion_batch.finalize()
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
    entries = get_persona_store().sync_db(session)
    session.flush()
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "会话不存在")
    if payload.persona_id is not None and not (
        entries.get(payload.persona_id) is not None and entries[payload.persona_id].is_active
    ):
        raise HTTPException(404, "人格不存在")
    conversation.persona_id = payload.persona_id
    session.commit()
    session.refresh(conversation)
    return conversation_dict(conversation)
