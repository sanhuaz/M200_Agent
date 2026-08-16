from app.services.retrieval import SearchHit, reciprocal_rank_fusion


def hit(chunk_id: str) -> SearchHit:
    return SearchHit(chunk_id, chunk_id, "doc.txt", [], None)


def test_rrf_merges_and_rewards_cross_retriever_hits() -> None:
    merged = reciprocal_rank_fusion([[hit("a"), hit("b")], [hit("b"), hit("c")]])
    assert [item.chunk_id for item in merged] == ["b", "a", "c"]
    assert merged[0].rrf_score > merged[1].rrf_score
