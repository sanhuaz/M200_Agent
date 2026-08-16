from __future__ import annotations

import json
from dataclasses import dataclass, replace

import httpx
import jieba
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Chunk, Document, KnowledgeBase
from app.services.embeddings import get_embedding_provider
from app.services.vector_store import safe_collection_name, vector_store


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: str
    content: str
    filename: str
    heading_path: list[str]
    page_number: int | None
    vector_score: float | None = None
    keyword_score: float | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None


def reciprocal_rank_fusion(rankings: list[list[SearchHit]], constant: int = 60) -> list[SearchHit]:
    scores: dict[str, float] = {}
    values: dict[str, SearchHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1 / (constant + rank)
            existing = values.get(hit.chunk_id)
            values[hit.chunk_id] = (
                replace(
                    existing,
                    vector_score=hit.vector_score
                    if hit.vector_score is not None
                    else existing.vector_score,
                    keyword_score=hit.keyword_score
                    if hit.keyword_score is not None
                    else existing.keyword_score,
                )
                if existing
                else hit
            )
    return sorted(
        (replace(values[key], rrf_score=score) for key, score in scores.items()),
        key=lambda item: item.rrf_score,
        reverse=True,
    )


class HybridRetriever:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.reranker_status = "disabled"

    def search(self, knowledge_base_id: str, query: str, recall: int = 20, top_n: int = 5) -> list[SearchHit]:
        kb = self.session.get(KnowledgeBase, knowledge_base_id)
        if kb is None:
            raise ValueError("知识库不存在")
        provider = get_embedding_provider(kb.embedding_profile)
        collection = safe_collection_name("docs", kb.id, kb.embedding_profile)
        vector_rows = vector_store.query(collection, provider.embed_query(query), recall)
        vector_hits = [
            self._hydrate(str(row["id"]), vector_score=float(str(row["score"]))) for row in vector_rows
        ]
        vector_hits = [item for item in vector_hits if item is not None]
        keyword_hits = self._keyword_search(kb.id, query, recall)
        fused = reciprocal_rank_fusion([vector_hits, keyword_hits])
        if get_settings().rerank_enabled:
            return self._rerank(query, fused, top_n)
        self.reranker_status = "disabled"
        return fused[:top_n]

    def _hydrate(self, chunk_id: str, **scores: float) -> SearchHit | None:
        row = self.session.execute(
            select(Chunk, Document.filename)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.id == chunk_id)
        ).first()
        if row is None:
            return None
        chunk, filename = row
        return SearchHit(
            chunk_id=chunk.id,
            content=chunk.content,
            filename=filename,
            heading_path=json.loads(chunk.heading_path),
            page_number=chunk.page_number,
            **scores,
        )

    def _keyword_search(self, kb_id: str, query: str, limit: int) -> list[SearchHit]:
        tokens = [token.strip() for token in jieba.cut_for_search(query) if token.strip()]
        if not tokens:
            return []
        match_query = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
        rows = self.session.execute(
            text(
                "SELECT chunk_id, bm25(chunk_fts) AS score FROM chunk_fts "
                "WHERE knowledge_base_id=:kb AND chunk_fts MATCH :query ORDER BY score LIMIT :limit"
            ),
            {"kb": kb_id, "query": match_query, "limit": limit},
        ).mappings()
        hits: list[SearchHit] = []
        for row in rows:
            hit = self._hydrate(str(row["chunk_id"]), keyword_score=-float(row["score"]))
            if hit:
                hits.append(hit)
        return hits

    def _rerank(self, query: str, candidates: list[SearchHit], top_n: int) -> list[SearchHit]:
        settings = get_settings()
        if not candidates:
            self.reranker_status = "not_needed"
            return []
        if not settings.rerank_api_key:
            self.reranker_status = "degraded_missing_key"
            return candidates[:top_n]
        try:
            response = httpx.post(
                settings.rerank_url,
                headers={"Authorization": f"Bearer {settings.rerank_api_key}"},
                json={
                    "model": settings.rerank_model,
                    "query": query,
                    "documents": [item.content for item in candidates],
                    "top_n": min(top_n, len(candidates)),
                },
                timeout=30,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            reranked = [
                replace(candidates[item["index"]], rerank_score=float(item["relevance_score"]))
                for item in results
                if 0 <= int(item["index"]) < len(candidates)
            ]
            if reranked:
                self.reranker_status = "succeeded"
                return reranked
            self.reranker_status = "degraded_empty_result"
            return candidates[:top_n]
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            self.reranker_status = "degraded_error"
            return candidates[:top_n]
