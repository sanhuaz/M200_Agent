from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
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

    @staticmethod
    def _scope_key(scope_type: str, scope_id: str) -> str:
        if scope_type == "group":
            return f"group:{scope_id}"
        return scope_id

    @staticmethod
    def _scope_filter(scope_type: str, scope_id: str):
        return or_(
            and_(Memory.scope_type == "global", Memory.user_id.is_(None)),
            and_(Memory.scope_type == scope_type, Memory.user_id == scope_id),
        )

    def recall(
        self,
        user_id: str,
        query: str,
        limit: int = 5,
        *,
        scope_type: str = "user",
        scope_id: str | None = None,
    ) -> list[Memory]:
        # 群聊使用群作用域，不把当前发言者的私聊记忆带入群上下文。
        key = scope_id or user_id
        scope_filter = self._scope_filter(scope_type, key)
        has_memory = self.session.scalar(
            select(Memory.id)
            .where(
                Memory.status == "active",
                scope_filter,
            )
            .limit(1)
        )
        if has_memory is None:
            return []
        profile = get_settings().default_embedding_profile
        try:
            provider = get_embedding_provider(profile)
            query_embedding = provider.embed_query(query)
        except Exception:
            # 本地 BGE 尚未下载时，记忆召回退化为精确关键词，不阻塞当前聊天。
            terms = [term for term in re.findall(r"[\w\u4e00-\u9fff]+", query) if len(term) >= 2]
            fallback = select(Memory).where(
                Memory.status == "active",
                scope_filter,
            )
            if terms:
                fallback = fallback.where(
                    Memory.content.contains(terms[0])
                    | Memory.fact_key.contains(terms[0])
                )
            return list(self.session.scalars(fallback.order_by(Memory.last_seen_at.desc()).limit(limit)))
        vector_rows: list[dict[str, object]] = []
        for scope in ("global", self._scope_key(scope_type, key)):
            collection = safe_collection_name("memory", scope, profile)
            vector_rows.extend(vector_store.query(collection, query_embedding, limit))
        def score(row: dict[str, object]) -> float:
            value = row.get("score")
            return float(value) if isinstance(value, (int, float, str)) else 0.0

        vector_rows.sort(key=score, reverse=True)
        ids = [str(row["id"]) for row in vector_rows[:limit]]
        if not ids:
            return []
        by_id = {
            item.id: item
            for item in self.session.scalars(
                select(Memory).where(
                    Memory.id.in_(ids),
                    Memory.status == "active",
                    scope_filter,
                )
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
        *,
        scope_type: str = "user",
        scope_id: str | None = None,
    ) -> Memory:
        if SENSITIVE_PATTERN.search(content):
            raise ValueError("疑似凭证内容不能写入长期记忆")
        key = scope_id or user_id
        active = self.session.scalar(
            select(Memory).where(
                Memory.scope_type == scope_type,
                Memory.user_id == key,
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
            vector_store.delete_ids(
                safe_collection_name(
                    "memory", self._scope_key(scope_type, key), get_settings().default_embedding_profile
                ),
                [active.id],
            )
        memory = Memory(
            scope_type=scope_type,
            user_id=key,
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
            safe_collection_name("memory", self._scope_key(scope_type, key), profile),
            [memory.id],
            [memory.content],
            provider.embed_documents([memory.content]),
            [{"scope_type": scope_type, "scope_id": key, "fact_key": fact_key}],
        )
        self.session.commit()
        return memory

    def upsert_global(
        self,
        fact_key: str,
        content: str,
        source_message_id: str | None = None,
        extraction_model: str | None = None,
    ) -> Memory:
        if SENSITIVE_PATTERN.search(content):
            raise ValueError("疑似凭证内容不能写入长期记忆")
        active = self.session.scalar(
            select(Memory).where(
                Memory.scope_type == "global",
                Memory.user_id.is_(None),
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
            vector_store.delete_ids(
                safe_collection_name("memory", "global", get_settings().default_embedding_profile),
                [active.id],
            )
        memory = Memory(
            scope_type="global",
            user_id=None,
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
            safe_collection_name("memory", "global", profile),
            [memory.id],
            [memory.content],
            provider.embed_documents([memory.content]),
            [{"scope_type": "global", "fact_key": fact_key}],
        )
        self.session.commit()
        return memory

    def extract_from_turn(
        self,
        user_id: str,
        user_text: str,
        source_message_id: str | None,
        model_alias: str,
        *,
        scope_type: str = "user",
        scope_id: str | None = None,
        context: str = "",
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
                        + (
                            "当前是QQ群作用域，只提取群共同决定、项目事实或群级偏好，"
                            "不要提取发言者个人信息。"
                            if scope_type == "group"
                            else ""
                        )
                    ),
                ),
                (
                    "human",
                    (
                        f"相关近期对话（仅用于指代消解，不得把助手猜测写入记忆）：\n{context}\n\n"
                        f"本轮用户消息：\n{user_text}"
                    ),
                ),
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
                scope_type=scope_type,
                scope_id=scope_id,
            )
            for fact in result.facts
            if fact.fact_key.strip() and fact.content.strip()
        ]
