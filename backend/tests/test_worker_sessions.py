from __future__ import annotations

from contextlib import nullcontext

from app.services import chat_support, jobs


def test_memory_recall_creates_worker_session(monkeypatch) -> None:
    worker_session = object()
    seen: dict[str, object] = {}

    class FakeMemoryService:
        def __init__(self, session) -> None:
            seen["session"] = session

        def recall(self, user_id, query, limit, *, scope_type, scope_id):
            seen["arguments"] = (user_id, query, limit, scope_type, scope_id)
            return []

    monkeypatch.setattr(chat_support, "SessionLocal", lambda: nullcontext(worker_session))
    monkeypatch.setattr(chat_support, "MemoryService", FakeMemoryService)

    result = chat_support.recall_memories_in_worker_session(
        "owner",
        "query",
        5,
        scope_type="user",
        scope_id="owner",
    )

    assert result == []
    assert seen == {
        "session": worker_session,
        "arguments": ("owner", "query", 5, "user", "owner"),
    }


def test_document_jobs_create_worker_sessions(monkeypatch) -> None:
    worker_sessions = [object(), object()]
    opened: list[object] = []
    indexed: list[tuple[object, ...]] = []

    def session_factory():
        session = worker_sessions[len(opened)]
        opened.append(session)
        return nullcontext(session)

    monkeypatch.setattr(jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(
        jobs,
        "index_document",
        lambda session, document_id: indexed.append((session, document_id)) or {"indexed": True},
    )
    monkeypatch.setattr(
        jobs,
        "reindex_knowledge_base",
        lambda session, knowledge_base_id, profile: (
            indexed.append((session, knowledge_base_id, profile)) or {"reindexed": True}
        ),
    )

    assert jobs._index_document_in_worker_session("doc-1") == {"indexed": True}
    assert jobs._reindex_knowledge_base_in_worker_session("kb-1", "local-bge") == {
        "reindexed": True
    }
    assert indexed == [
        (worker_sessions[0], "doc-1"),
        (worker_sessions[1], "kb-1", "local-bge"),
    ]
