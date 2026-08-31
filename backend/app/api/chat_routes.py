from __future__ import annotations

import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_loopback
from app.db.models import Conversation, MessageAttachment
from app.db.session import get_db
from app.domain.chat_inputs import ChatAttachmentInput, ChatTurnInput
from app.services.chat import chat_service
from app.services.chat_attachments import (
    AttachmentValidationError,
    attachment_path,
    delete_attachment,
    validate_inputs,
)
from app.services.models import model_registry

router = APIRouter()
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=50_000)
    sender_id: str = "local-owner"


def _format_event(event: dict[str, object]) -> str:
    return (
        f"event: {event['event']}\n"
        f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
    )


async def _error_events(message: str, code: str) -> AsyncGenerator[str]:
    yield _format_event({"event": "error", "data": {"message": message, "code": code}})


async def _read_images(images: list[UploadFile]) -> tuple[ChatAttachmentInput, ...]:
    if len(images) > 4:
        raise AttachmentValidationError("每条消息最多附带 4 张图片")
    total = 0
    values: list[ChatAttachmentInput] = []
    for image in images:
        remaining = MAX_UPLOAD_BYTES - total
        if remaining <= 0:
            raise AttachmentValidationError("每条消息图片总大小不能超过 32 MiB")
        payload = await image.read(remaining + 1)
        total += len(payload)
        if len(payload) > remaining:
            raise AttachmentValidationError("每条消息图片总大小不能超过 32 MiB")
        values.append(
            ChatAttachmentInput(
                content=payload,
                original_filename=image.filename or "image",
                source="web",
                content_type=image.content_type,
            )
        )
    return tuple(values)


def _get_or_create_conversation(
    session: Session,
    conversation_id: str | None,
    sender_id: str,
    model_alias: str,
    persona_id: str | None = None,
) -> tuple[Conversation, bool]:
    conversation = session.get(Conversation, conversation_id) if conversation_id else None
    if conversation_id and conversation is None:
        raise HTTPException(404, "会话不存在")
    if conversation is None:
        conversation = Conversation(
            owner_id=sender_id,
            model_alias=model_alias,
            persona_id=persona_id,
        )
        session.add(conversation)
        session.commit()
        session.refresh(conversation)
        return conversation, True
    changed = False
    if conversation.model_alias != model_alias:
        conversation.model_alias = model_alias
        changed = True
    if persona_id is not None and conversation.persona_id != persona_id:
        conversation.persona_id = persona_id
        changed = True
    if changed:
        session.commit()
    return conversation, False


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest, session: Session = Depends(get_db)
) -> StreamingResponse:
    existing = session.get(Conversation, payload.conversation_id) if payload.conversation_id else None
    model_alias = existing.model_alias if existing is not None else model_registry.default_alias()
    conversation, _created = _get_or_create_conversation(
        session, payload.conversation_id if existing is not None else None, payload.sender_id, model_alias
    )

    async def events() -> AsyncGenerator[str]:
        async for event in chat_service.stream(
            session, conversation, payload.sender_id, payload.message
        ):
            yield _format_event(event)

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/chat/stream/multimodal", dependencies=[Depends(require_loopback)])
async def chat_stream_multimodal(
    content: str = Form(default=""),
    message: str | None = Form(default=None),
    conversation_id: str | None = Form(default=None),
    sender_id: str = Form(default="local-owner"),
    model_profile_id: str | None = Form(default=None),
    model_alias: str | None = Form(default=None),
    persona_id: str | None = Form(default=None),
    images: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_db),
) -> StreamingResponse:
    text = content if content != "" else (message or "")
    existing = session.get(Conversation, conversation_id) if conversation_id else None
    if conversation_id and existing is None:
        raise HTTPException(404, "会话不存在")
    selected_alias = model_profile_id or model_alias or (
        existing.model_alias if existing is not None else model_registry.default_alias()
    )
    try:
        profile = model_registry.profile(selected_alias)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error

    try:
        inputs = await _read_images(images)
        validate_inputs(inputs)
    except AttachmentValidationError as error:
        return StreamingResponse(
            _error_events(str(error), "invalid_attachment"), media_type="text/event-stream"
        )
    if not text and not inputs:
        return StreamingResponse(
            _error_events("请输入文字或至少选择一张图片。", "invalid_input"),
            media_type="text/event-stream",
        )
    if inputs and not profile.supports_vision:
        return StreamingResponse(
            _error_events("当前模型未启用识图能力，请在模型管理中开启后重试。", "vision_not_supported"),
            media_type="text/event-stream",
        )

    conversation, created_conversation = _get_or_create_conversation(
        session,
        conversation_id,
        sender_id,
        selected_alias,
        persona_id,
    )

    turn = ChatTurnInput(
        text=text,
        attachments=inputs,
        conversation_id=conversation.id,
        sender_id=sender_id,
        platform="web",
        is_group=False,
        model_alias=selected_alias,
        persona_id=persona_id,
    )

    async def events() -> AsyncGenerator[str]:
        remove_new_conversation = False
        try:
            async for event in chat_service.stream(
                session, conversation, sender_id, turn
            ):
                if event.get("event") == "error":
                    data = event.get("data")
                    if isinstance(data, dict) and data.get("code") in {
                        "invalid_attachment",
                        "vision_not_supported",
                        "persistence_failed",
                    }:
                        remove_new_conversation = created_conversation
                yield _format_event(event)
        finally:
            if remove_new_conversation:
                try:
                    session.rollback()
                    stale = session.get(Conversation, conversation.id)
                    if stale is not None:
                        session.delete(stale)
                        session.commit()
                except Exception:
                    session.rollback()

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get(
    "/messages/{message_id}/attachments/{attachment_id}",
    dependencies=[Depends(require_loopback)],
)
def get_message_attachment(
    message_id: str, attachment_id: str, session: Session = Depends(get_db)
) -> FileResponse:
    item = session.scalar(
        select(MessageAttachment).where(
            MessageAttachment.id == attachment_id,
            MessageAttachment.message_id == message_id,
        )
    )
    if item is None:
        raise HTTPException(404, "附件不存在")
    try:
        path = attachment_path(item)
    except ValueError as error:
        raise HTTPException(404, "附件路径无效") from error
    if not path.is_file():
        raise HTTPException(404, "附件文件不存在")
    return FileResponse(path, media_type=item.mime_type, filename=item.original_filename)


@router.delete(
    "/messages/{message_id}/attachments/{attachment_id}",
    dependencies=[Depends(require_loopback)],
)
def remove_message_attachment(
    message_id: str, attachment_id: str, session: Session = Depends(get_db)
) -> dict[str, object]:
    if not delete_attachment(session, message_id, attachment_id):
        raise HTTPException(404, "附件不存在")
    return {"deleted": True, "message_id": message_id, "attachment_id": attachment_id}
