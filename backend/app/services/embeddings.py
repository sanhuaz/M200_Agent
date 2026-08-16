from __future__ import annotations

import os
from functools import lru_cache
from typing import Protocol

from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr
from sentence_transformers import SentenceTransformer

from app.core.config import get_settings


class EmbeddingProvider(Protocol):
    profile: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class LocalBgeEmbeddings:
    profile = "local-bge"

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: SentenceTransformer | None = None

    def _get_model(self) -> SentenceTransformer:
        if self._model is None:
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._get_model().encode(texts, normalize_embeddings=True)
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class OnlineEmbeddings:
    profile = "online"

    def __init__(self) -> None:
        settings = get_settings()
        api_key = os.getenv(settings.online_embedding_api_key_env, "")
        if not api_key:
            raise RuntimeError(f"在线 Embedding 未配置环境变量 {settings.online_embedding_api_key_env}")
        self._client = OpenAIEmbeddings(
            model=settings.online_embedding_model,
            api_key=SecretStr(api_key),
            base_url=settings.online_embedding_base_url,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._client.embed_query(text)


@lru_cache(maxsize=2)
def get_embedding_provider(profile: str) -> EmbeddingProvider:
    settings = get_settings()
    if profile == "local-bge":
        return LocalBgeEmbeddings(settings.local_embedding_model)
    if profile == "online":
        return OnlineEmbeddings()
    raise ValueError(f"未知 Embedding 配置: {profile}")
