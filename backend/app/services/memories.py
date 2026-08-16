from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Memory
from app.services.embeddings import get_embedding_provider
from app.services.models import model_registry
from app.services.vector_store import safe_collection_name, vector_store

SENSITIVE_PATTERN = re.compile(r"(?i)(api[_ -]?key|token|password|passwd|secret|密码|密钥)\s*[:=：]")


class ExtractedFact(BaseModel):
    fact_key: str = Field(description="稳定、简短的事实键，例如 preference.language")
    content: str = Field(description="一条可以独立理解的中文事实")


class ExtractedFacts(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)


class MemoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def recall(self, user_id: str, query: str, limit: int = 5) -> list[Memory]:
        has_memory = self.session.scalar(
            select(Memory.id).where(Memory.user_id == user_id, Memory.status == "active").limit(1)
        )
        if has_memory is None:
            return []
        profile = get_settings().default_embedding_profile
        provider = get_embedding_provider(profile)
        collection = safe_collection_name("memory", user_id, profile)
        vector_rows = vector_store.query(collection, provider.embed_query(query), limit)
        ids = [str(row["id"]) for row in vector_rows]
        if not ids:
            return []
        by_id = {
            item.id: item
            for item in self.session.scalars(
                select(Memory).where(Memory.id.in_(ids), Memory.status == "active")
            )
        }
        return [by_id[item_id] for item_id in ids if item_id in by_id]

    def upsert(
        self,
        user_id: str,
        fact_key: str,
        content: str,
        source_message_id: str | None = None,
        extraction_model: str | None = None,
    ) -> Memory:
        if SENSITIVE_PATTERN.search(content):
            raise ValueError("疑似凭证内容不能写入长期记忆")
        active = self.session.scalar(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.fact_key == fact_key,
                Memory.status == "active",
            )
        )
        now = datetime.now(UTC).replace(tzinfo=None)
        if active and active.content == content:
            active.last_seen_at = now
            self.session.commit()
            return active
        if active:
            active.status = "archived"
        memory = Memory(
            user_id=user_id,
            fact_key=fact_key[:200],
            content=content,
            source_message_id=source_message_id,
            extraction_model=extraction_model,
        )
        self.session.add(memory)
        self.session.flush()
        profile = get_settings().default_embedding_profile
        provider = get_embedding_provider(profile)
        vector_store.upsert_documents(
            safe_collection_name("memory", user_id, profile),
            [memory.id],
            [memory.content],
            provider.embed_documents([memory.content]),
            [{"user_id": user_id, "fact_key": fact_key}],
        )
        self.session.commit()
        return memory

    def extract_from_turn(
        self,
        user_id: str,
        user_text: str,
        source_message_id: str | None,
        model_alias: str,
    ) -> list[Memory]:
        if SENSITIVE_PATTERN.search(user_text):
            return []
        base_model = model_registry.chat_model(model_alias)
        profile = model_registry.profile(model_alias)
        if "api.deepseek.com" in profile.base_url:
            base_model = base_model.model_copy(
                update={"extra_body": {"thinking": {"type": "disabled"}}}
            )
        model = base_model.with_structured_output(
            ExtractedFacts,
            method="function_calling",
        )
        raw_result = model.invoke(
            [
                (
                    "system",
                    (
                        "从用户消息中提取稳定事实、长期偏好或明确长期事件。"
                        "不要提取临时请求、猜测、第三方隐私或任何凭证。没有则返回空列表。"
                    ),
                ),
                ("human", user_text),
            ]
        )
        result = ExtractedFacts.model_validate(raw_result)
        return [
            self.upsert(
                user_id,
                fact.fact_key,
                fact.content,
                source_message_id,
                model_alias,
            )
            for fact in result.facts
            if fact.fact_key.strip() and fact.content.strip()
        ]
