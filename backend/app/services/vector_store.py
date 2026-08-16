from __future__ import annotations

import re
from typing import Any, cast

import chromadb
from chromadb.api import ClientAPI

from app.core.config import get_settings


def safe_collection_name(prefix: str, value: str, profile: str) -> str:
    raw = f"{prefix}-{value}-{profile}".lower()
    cleaned = re.sub(r"[^a-z0-9._-]", "-", raw)
    return cleaned[:200].strip("-.")


class VectorStore:
    def __init__(self) -> None:
        self._client: ClientAPI | None = None

    @property
    def client(self) -> ClientAPI:
        if self._client is None:
            self._client = chromadb.PersistentClient(path=str(get_settings().chroma_path))
        return self._client

    def upsert_documents(
        self,
        collection_name: str,
        ids: list[str],
        contents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, str | int]],
    ) -> None:
        collection = self.client.get_or_create_collection(collection_name)
        collection.upsert(
            ids=ids,
            documents=contents,
            embeddings=cast(Any, embeddings),
            metadatas=cast(Any, metadatas),
        )

    def query(
        self,
        collection_name: str,
        embedding: list[float],
        limit: int,
        where: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        try:
            collection = self.client.get_collection(collection_name)
        except Exception:
            return []
        result = collection.query(query_embeddings=[embedding], n_results=limit, where=cast(Any, where))
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            {
                "id": item_id,
                "content": documents[index],
                "metadata": metadatas[index] or {},
                "score": 1.0 / (1.0 + float(distances[index])),
            }
            for index, item_id in enumerate(ids)
        ]

    def delete_collection(self, collection_name: str) -> None:
        try:
            self.client.delete_collection(collection_name)
        except Exception:
            return

    def delete_ids(self, collection_name: str, ids: list[str]) -> None:
        if not ids:
            return
        try:
            self.client.get_collection(collection_name).delete(ids=ids)
        except Exception:
            return


vector_store = VectorStore()
