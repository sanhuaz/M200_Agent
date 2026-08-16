from __future__ import annotations

from pathlib import Path

from app.db.models import Chunk, Document, KnowledgeBase
from app.db.session import SessionLocal, initialize_database
from app.services import documents as document_service
from sqlalchemy import func, select, text


class FakeEmbeddings:
    profile = "local-bge"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]


def test_indexing_keeps_keyword_rows_for_other_documents(tmp_path: Path, monkeypatch) -> None:
    initialize_database()
    monkeypatch.setattr(document_service, "get_embedding_provider", lambda _profile: FakeEmbeddings())
    monkeypatch.setattr(document_service.vector_store, "delete_ids", lambda *_args: None)
    monkeypatch.setattr(document_service.vector_store, "upsert_documents", lambda *_args: None)
    first_path = tmp_path / "one.md"
    second_path = tmp_path / "two.txt"
    first_path.write_text("# 第一章\n\nLangGraph 编排状态节点。", encoding="utf-8")
    second_path.write_text("SQLite FTS5 负责精确关键词检索。", encoding="utf-8")
    with SessionLocal() as session:
        kb = KnowledgeBase(name="test-kb", embedding_profile="local-bge")
        session.add(kb)
        session.flush()
        first = Document(
            knowledge_base_id=kb.id,
            filename=first_path.name,
            path=str(first_path),
            sha256="1" * 64,
        )
        second = Document(
            knowledge_base_id=kb.id,
            filename=second_path.name,
            path=str(second_path),
            sha256="2" * 64,
        )
        session.add_all([first, second])
        session.commit()
        document_service.index_document(session, first.id)
        document_service.index_document(session, second.id)
        assert session.scalar(select(func.count()).select_from(Chunk)) == 2
        fts_count = session.execute(text("SELECT count(*) FROM chunk_fts")).scalar_one()
        assert fts_count == 2


def test_chunker_preserves_heading_and_size() -> None:
    blocks = [
        document_service.ParsedBlock("第一段" * 100, ["标题"], 1),
        document_service.ParsedBlock("第二段" * 100, ["标题"], 1),
    ]
    chunks = document_service.chunk_blocks(blocks, target_chars=300, max_chars=500)
    assert chunks
    assert all(len(chunk.content) <= 500 for chunk in chunks)
    assert all(chunk.heading_path == ["标题"] for chunk in chunks)
