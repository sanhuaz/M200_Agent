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


def memory_scope_key(scope_type: str, scope_id: str) -> str:
    """Return the stable vector-store scope key used by runtime recall.

    Group conversations currently pass their ``group:<id>`` external id as the
    scope id.  Keep the existing key shape for compatibility with already
    indexed vectors; management operations must call this same helper instead
    of rebuilding the collection name independently.
    """
    if scope_type == "group":
        return f"group:{scope_id}"
    return scope_id


def memory_collection_name(scope_type: str, scope_id: str, profile: str) -> str:
    return safe_collection_name("memory", memory_scope_key(scope_type, scope_id), profile)


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
        return memory_scope_key(scope_type, scope_id)

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
        for current_scope_type, current_scope_id in (("global", "global"), (scope_type, key)):
            collection = memory_collection_name(current_scope_type, current_scope_id, profile)
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
        key = scope_id or user_id
        return self._upsert(
            scope_type=scope_type,
            scope_id=key,
            database_user_id=key,
            fact_key=fact_key,
            content=content,
            source_message_id=source_message_id,
            extraction_model=extraction_model,
        )

    def _upsert(
        self,
        *,
        scope_type: str,
        scope_id: str,
        database_user_id: str | None,
        fact_key: str,
        content: str,
        source_message_id: str | None,
        extraction_model: str | None,
    ) -> Memory:
        if SENSITIVE_PATTERN.search(content):
            raise ValueError("疑似凭证内容不能写入长期记忆")
        user_condition = (
            Memory.user_id.is_(None)
            if database_user_id is None
            else Memory.user_id == database_user_id
        )
        active = self.session.scalar(
            select(Memory).where(
                Memory.scope_type == scope_type,
                user_condition,
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
                memory_collection_name(
                    scope_type, scope_id, get_settings().default_embedding_profile
                ),
                [active.id],
            )
        memory = Memory(
            scope_type=scope_type,
            user_id=database_user_id,
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
            memory_collection_name(scope_type, scope_id, profile),
            [memory.id],
            [memory.content],
            provider.embed_documents([memory.content]),
            [{"scope_type": scope_type, "scope_id": scope_id, "fact_key": fact_key}],
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
        return self._upsert(
            scope_type="global",
            scope_id="global",
            database_user_id=None,
            fact_key=fact_key[:200],
            content=content,
            source_message_id=source_message_id,
            extraction_model=extraction_model,
        )

    @staticmethod
    def _collection(item: Memory, profile: str) -> str:
        return memory_collection_name(item.scope_type, item.user_id or "global", profile)

    def update(self, memory_id: str, content: str) -> Memory:
        if SENSITIVE_PATTERN.search(content):
            raise ValueError("疑似凭证内容不能写入长期记忆")
        item = self.session.get(Memory, memory_id)
        if item is None:
            raise LookupError("记忆不存在")
        item.content = content
        profile = get_settings().default_embedding_profile
        provider = get_embedding_provider(profile)
        vector_store.upsert_documents(
            self._collection(item, profile),
            [item.id],
            [item.content],
            provider.embed_documents([item.content]),
            [
                {
                    "scope_type": item.scope_type,
                    "scope_id": item.user_id or "global",
                    "fact_key": item.fact_key,
                }
            ],
        )
        self.session.commit()
        return item

    def archive(self, memory_id: str) -> Memory:
        item = self.session.get(Memory, memory_id)
        if item is None:
            raise LookupError("记忆不存在")
        item.status = "archived"
        profile = get_settings().default_embedding_profile
        vector_store.delete_ids(self._collection(item, profile), [item.id])
        self.session.commit()
        return item

    def delete(self, memory_id: str) -> None:
        item = self.session.get(Memory, memory_id)
        if item is None:
            raise LookupError("记忆不存在")
        profile = get_settings().default_embedding_profile
        vector_store.delete_ids(self._collection(item, profile), [item.id])
        self.session.delete(item)
        self.session.commit()

    def restore(self, memory_id: str) -> Memory:
        item = self.session.get(Memory, memory_id)
        if item is None:
            raise LookupError("记忆不存在")
        item.status = "active"
        profile = get_settings().default_embedding_profile
        provider = get_embedding_provider(profile)
        vector_store.upsert_documents(
            self._collection(item, profile),
            [item.id],
            [item.content],
            provider.embed_documents([item.content]),
            [
                {
                    "scope_type": item.scope_type,
                    "scope_id": item.user_id or "global",
                    "fact_key": item.fact_key,
                }
            ],
        )
        self.session.commit()
        return item

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
        time_context: str = "",
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
                        "必须依据消息时间上下文理解今天、昨天、昨晚和刚才；不要把历史事件改写为当前状态。"
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
                        f"当前时间上下文：\n{time_context or '未提供'}\n\n"
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
