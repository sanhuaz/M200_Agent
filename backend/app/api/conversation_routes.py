from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
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
    ToolRun,
)
from app.db.session import get_db
from app.services.chat import chat_service
from app.services.models import model_registry
from app.services.persona_store import get_persona_store

router = APIRouter()


class ConversationCreate(BaseModel):
    title: str = "新会话"
    model_alias: str | None = None


class ModelSwitch(BaseModel):
    model_alias: str


class PersonaSwitch(BaseModel):
    persona_id: str | None = None


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=50_000)
    sender_id: str = "local-owner"


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
            "created_at": item.created_at.isoformat(),
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


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest, session: Session = Depends(get_db)
) -> StreamingResponse:
    conversation = (
        session.get(Conversation, payload.conversation_id) if payload.conversation_id else None
    )
    if conversation is None:
        conversation = Conversation(
            owner_id=payload.sender_id,
            model_alias=model_registry.default_alias(),
        )
        session.add(conversation)
        session.commit()
        session.refresh(conversation)

    async def events():
        async for event in chat_service.stream(
            session, conversation, payload.sender_id, payload.message
        ):
            yield (
                f"event: {event['event']}\n"
                f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
            )

    return StreamingResponse(events(), media_type="text/event-stream")
