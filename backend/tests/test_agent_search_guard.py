from app.workflows.agent import KnowledgeSearchGuard


def test_search_guard_deduplicates_normalized_query_per_knowledge_base() -> None:
    guard = KnowledgeSearchGuard(max_unique_searches=4)

    assert guard.reserve("  什么是 RAG？ ", "kb-a") == "allowed"
    assert guard.reserve("什么是   rag？", "kb-a") == "duplicate"
    assert guard.force_final is True


def test_search_guard_allows_same_query_across_knowledge_bases() -> None:
    guard = KnowledgeSearchGuard(max_unique_searches=4)

    assert guard.reserve("什么是 RAG？", "kb-a") == "allowed"
    assert guard.reserve("什么是 RAG？", "kb-b") == "allowed"
    assert guard.force_final is False


def test_search_guard_enforces_unique_search_limit() -> None:
    guard = KnowledgeSearchGuard(max_unique_searches=2)

    assert guard.reserve("问题一", "kb-a") == "allowed"
    assert guard.reserve("问题二", "kb-a") == "allowed"
    assert guard.force_final is True
    assert guard.reserve("问题三", "kb-a") == "limit"
