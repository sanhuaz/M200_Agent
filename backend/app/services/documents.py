from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import jieba
from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from pypdf import PdfReader
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document, KnowledgeBase
from app.services.embeddings import get_embedding_provider
from app.services.vector_store import safe_collection_name, vector_store

SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx"}


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    content: str
    heading_path: list[str]
    page_number: int | None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def parse_document(path: Path) -> list[ParsedBlock]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"不支持的文件格式: {suffix}")
    if suffix in {".txt", ".md", ".markdown"}:
        return _parse_text(path)
    if suffix in {".html", ".htm"}:
        return _parse_html(path)
    if suffix == ".pdf":
        return _parse_pdf(path)
    return _parse_docx(path)


def _parse_text(path: Path) -> list[ParsedBlock]:
    text_content = path.read_text(encoding="utf-8-sig")
    blocks: list[ParsedBlock] = []
    headings: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text_content):
        value = paragraph.strip()
        if not value:
            continue
        if value.startswith("#"):
            heading = value.lstrip("#").strip()
            headings = [heading] if heading else headings
        blocks.append(ParsedBlock(value, headings.copy(), None))
    return blocks


def _parse_html(path: Path) -> list[ParsedBlock]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8-sig"), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    blocks: list[ParsedBlock] = []
    headings: list[str] = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        value = node.get_text(" ", strip=True)
        if not value:
            continue
        if node.name and node.name.startswith("h"):
            level = int(node.name[1])
            headings = headings[: level - 1] + [value]
        blocks.append(ParsedBlock(value, headings.copy(), None))
    return blocks


def _parse_pdf(path: Path) -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    for page_number, page in enumerate(PdfReader(str(path)).pages, start=1):
        for paragraph in re.split(r"\n\s*\n", page.extract_text() or ""):
            value = paragraph.strip()
            if value:
                blocks.append(ParsedBlock(value, [], page_number))
    if not blocks:
        raise ValueError("PDF 未提取到文本，可能是扫描版 PDF")
    return blocks


def _parse_docx(path: Path) -> list[ParsedBlock]:
    blocks: list[ParsedBlock] = []
    headings: list[str] = []
    for paragraph in DocxDocument(str(path)).paragraphs:
        value = paragraph.text.strip()
        if not value:
            continue
        style_name = paragraph.style.name if paragraph.style else None
        style = style_name.lower() if style_name else ""
        if style.startswith("heading"):
            digits = re.findall(r"\d+", style)
            level = int(digits[0]) if digits else 1
            headings = headings[: level - 1] + [value]
        blocks.append(ParsedBlock(value, headings.copy(), None))
    return blocks


def chunk_blocks(
    blocks: list[ParsedBlock], target_chars: int = 1000, max_chars: int = 1600
) -> list[ParsedBlock]:
    chunks: list[ParsedBlock] = []
    buffer: list[str] = []
    current: ParsedBlock | None = None
    for block in blocks:
        value = block.content
        if len(value) > max_chars:
            if buffer and current:
                chunks.append(ParsedBlock("\n\n".join(buffer), current.heading_path, current.page_number))
                buffer = []
            for start in range(0, len(value), max_chars - 120):
                chunks.append(
                    ParsedBlock(value[start : start + max_chars], block.heading_path, block.page_number)
                )
            current = None
            continue
        projected = sum(len(item) for item in buffer) + len(value)
        same_location = current is None or (
            current.heading_path == block.heading_path and current.page_number == block.page_number
        )
        if buffer and (projected > target_chars or not same_location):
            assert current is not None
            chunks.append(ParsedBlock("\n\n".join(buffer), current.heading_path, current.page_number))
            buffer = []
        current = block
        buffer.append(value)
    if buffer and current:
        chunks.append(ParsedBlock("\n\n".join(buffer), current.heading_path, current.page_number))
    return chunks


def index_document(session: Session, document_id: str) -> dict[str, object]:
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError("文档不存在")
    knowledge_base = session.get(KnowledgeBase, document.knowledge_base_id)
    if knowledge_base is None:
        raise ValueError("知识库不存在")
    document.status = "indexing"
    session.commit()
    blocks = chunk_blocks(parse_document(Path(document.path)))
    if not blocks:
        raise ValueError("文档没有可索引内容")
    old_chunk_ids = list(session.scalars(select(Chunk.id).where(Chunk.document_id == document.id)))
    session.execute(
        text(
            "DELETE FROM chunk_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=:document_id)"
        ),
        {"document_id": document.id},
    )
    session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    chunks = [
        Chunk(
            document_id=document.id,
            knowledge_base_id=knowledge_base.id,
            content=block.content,
            heading_path=json.dumps(block.heading_path, ensure_ascii=False),
            page_number=block.page_number,
            position=index,
        )
        for index, block in enumerate(blocks)
    ]
    session.add_all(chunks)
    session.flush()
    provider = get_embedding_provider(knowledge_base.embedding_profile)
    embeddings = provider.embed_documents([chunk.content for chunk in chunks])
    collection = safe_collection_name("docs", knowledge_base.id, knowledge_base.embedding_profile)
    vector_store.delete_ids(collection, old_chunk_ids)
    vector_store.upsert_documents(
        collection,
        [chunk.id for chunk in chunks],
        [chunk.content for chunk in chunks],
        embeddings,
        [
            {
                "document_id": document.id,
                "filename": document.filename,
                "page_number": chunk.page_number or 0,
                "position": chunk.position,
            }
            for chunk in chunks
        ],
    )
    for chunk in chunks:
        tokens = " ".join(jieba.cut_for_search(chunk.content))
        session.execute(
            text(
                "INSERT INTO chunk_fts(chunk_id, knowledge_base_id, tokens, content) "
                "VALUES(:chunk_id,:kb,:tokens,:content)"
            ),
            {"chunk_id": chunk.id, "kb": knowledge_base.id, "tokens": tokens, "content": chunk.content},
        )
    document.status = "ready"
    document.error = None
    session.commit()
    return {"document_id": document.id, "chunks": len(chunks)}


def reindex_knowledge_base(
    session: Session, knowledge_base_id: str, embedding_profile: str
) -> dict[str, object]:
    knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base is None:
        raise ValueError("知识库不存在")
    old_profile = knowledge_base.embedding_profile
    if old_profile == embedding_profile:
        return {"knowledge_base_id": knowledge_base.id, "embedding_profile": old_profile, "changed": False}

    rows = session.execute(
        select(Chunk, Document.filename)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.knowledge_base_id == knowledge_base.id)
        .order_by(Chunk.document_id, Chunk.position)
    ).all()
    provider = get_embedding_provider(embedding_profile)
    contents = [chunk.content for chunk, _filename in rows]
    embeddings = provider.embed_documents(contents) if contents else []
    new_collection = safe_collection_name("docs", knowledge_base.id, embedding_profile)
    vector_store.delete_collection(new_collection)
    try:
        if rows:
            vector_store.upsert_documents(
                new_collection,
                [chunk.id for chunk, _filename in rows],
                contents,
                embeddings,
                [
                    {
                        "document_id": chunk.document_id,
                        "filename": filename,
                        "page_number": chunk.page_number or 0,
                        "position": chunk.position,
                    }
                    for chunk, filename in rows
                ],
            )
        knowledge_base.embedding_profile = embedding_profile
        session.commit()
    except Exception:
        session.rollback()
        vector_store.delete_collection(new_collection)
        raise
    vector_store.delete_collection(safe_collection_name("docs", knowledge_base.id, old_profile))
    return {
        "knowledge_base_id": knowledge_base.id,
        "embedding_profile": embedding_profile,
        "chunks": len(rows),
        "changed": True,
    }
