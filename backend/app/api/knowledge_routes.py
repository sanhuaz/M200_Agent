from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Chunk, Document, KnowledgeBase
from app.db.session import get_db
from app.services.documents import SUPPORTED_SUFFIXES
from app.services.embeddings import get_embedding_provider
from app.services.jobs import create_job
from app.services.vector_store import safe_collection_name, vector_store

router = APIRouter()
settings = get_settings()


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    embedding_profile: str = "local-bge"


class EmbeddingSwitch(BaseModel):
    embedding_profile: str


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
    return [
        {"id": item.id, "name": item.name, "embedding_profile": item.embedding_profile}
        for item in items
    ]


@router.put("/knowledge-bases/{knowledge_base_id}/embedding")
def switch_knowledge_base_embedding(
    knowledge_base_id: str,
    payload: EmbeddingSwitch,
    session: Session = Depends(get_db),
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
    return {
        "task_id": job.id,
        "status": job.status,
        "active_embedding_profile": item.embedding_profile,
    }


@router.delete("/knowledge-bases/{knowledge_base_id}")
def delete_knowledge_base(
    knowledge_base_id: str, session: Session = Depends(get_db)
) -> dict[str, bool]:
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
def reindex_document(
    document_id: str, session: Session = Depends(get_db)
) -> dict[str, object]:
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
        text(
            "DELETE FROM chunk_fts WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE document_id=:doc)"
        ),
        {"doc": item.id},
    )
    path = Path(item.path)
    session.delete(item)
    session.commit()
    if path.is_file() and path.parent.resolve() == settings.upload_path.resolve():
        path.unlink(missing_ok=True)
    return {"deleted": True}
