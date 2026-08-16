from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Chunk, Confirmation, Conversation, Document, Job, KnowledgeBase, Memory, Message
from app.db.session import get_db
from app.services.chat import chat_service
from app.services.confirmations import resolve_confirmation
from app.services.documents import SUPPORTED_SUFFIXES
from app.services.embeddings import get_embedding_provider
from app.services.jobs import create_job, job_worker
from app.services.manga import manga_service
from app.services.models import model_registry
from app.services.vector_store import safe_collection_name, vector_store

router = APIRouter()
settings = get_settings()


class ConversationCreate(BaseModel):
    title: str = "新会话"
    model_alias: str = "default"


class ModelSwitch(BaseModel):
    model_alias: str


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


def conversation_dict(item: Conversation) -> dict[str, object]:
    return {
        "id": item.id,
        "platform": item.platform,
        "external_id": item.external_id,
        "conversation_type": item.conversation_type,
        "title": item.title,
        "model_alias": item.model_alias,
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
    from app.api.onebot import onebot_manager

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


@router.get("/models")
def list_models() -> list[dict[str, object]]:
    return model_registry.list()


@router.post("/conversations")
def create_conversation(payload: ConversationCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    model_registry.profile(payload.model_alias)
    item = Conversation(title=payload.title, model_alias=payload.model_alias)
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


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, session: Session = Depends(get_db)) -> StreamingResponse:
    conversation = session.get(Conversation, payload.conversation_id) if payload.conversation_id else None
    if conversation is None:
        conversation = Conversation(owner_id=payload.sender_id)
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
    user_id: str = "local-owner", status: str | None = None, session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    query = select(Memory).where(Memory.user_id == user_id).order_by(Memory.last_seen_at.desc())
    if status:
        query = query.where(Memory.status == status)
    return [
        {
            "id": item.id,
            "fact_key": item.fact_key,
            "content": item.content,
            "status": item.status,
            "source_message_id": item.source_message_id,
            "created_at": item.created_at.isoformat(),
            "last_seen_at": item.last_seen_at.isoformat(),
        }
        for item in session.scalars(query)
    ]


@router.put("/memories/{memory_id}")
def update_memory(
    memory_id: str, payload: MemoryUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    item = session.get(Memory, memory_id)
    if item is None:
        raise HTTPException(404, "记忆不存在")
    item.content = payload.content
    profile = settings.default_embedding_profile
    provider = get_embedding_provider(profile)
    vector_store.upsert_documents(
        safe_collection_name("memory", item.user_id, profile),
        [item.id],
        [item.content],
        provider.embed_documents([item.content]),
        [{"user_id": item.user_id, "fact_key": item.fact_key}],
    )
    session.commit()
    return {"id": item.id, "content": item.content, "status": item.status}


@router.post("/memories/{memory_id}/archive")
def archive_memory(memory_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    item = session.get(Memory, memory_id)
    if item is None:
        raise HTTPException(404, "记忆不存在")
    item.status = "archived"
    session.commit()
    return {"id": item.id, "status": item.status}


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    item = session.get(Memory, memory_id)
    if item is None:
        raise HTTPException(404, "记忆不存在")
    profile = settings.default_embedding_profile
    vector_store.delete_ids(safe_collection_name("memory", item.user_id, profile), [item.id])
    session.delete(item)
    session.commit()
    return {"deleted": True}


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
    from app.services.confirmations import create_download_confirmation

    try:
        item = create_download_confirmation(payload.album_id, payload.requester_id, payload.conversation_id)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    return {"token": item.token, "status": item.status, "expires_at": item.expires_at.isoformat()}


@router.post("/confirmations/{token}")
def confirm(token: str, payload: ConfirmationResolve) -> dict[str, object]:
    try:
        item, job = resolve_confirmation(token, payload.requester_id, payload.approve)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"confirmation_status": item.status, "task": job_dict(job) if job else None}


@router.get("/confirmations")
def list_confirmations(
    status: str = "pending", session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    items = session.scalars(
        select(Confirmation).where(Confirmation.status == status).order_by(Confirmation.created_at.desc())
    ).all()
    return [
        {
            "token": item.token,
            "requester_id": item.requester_id,
            "action": item.action,
            "payload": json.loads(item.payload),
            "status": item.status,
            "expires_at": item.expires_at.isoformat(),
        }
        for item in items
    ]


@router.get("/tasks")
def list_tasks(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [job_dict(item) for item in session.scalars(select(Job).order_by(Job.created_at.desc()))]


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
