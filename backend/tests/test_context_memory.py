from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from app.api.onebot import OneBotManager
from app.core.config import ModelProfile
from app.db.models import Conversation, Memory, Message, Persona
from app.db.session import SessionLocal
from app.services.chat import ChatService
from app.services.context import (
    TOOL_RESULT_MAX_CHARS,
    build_context_messages,
    choose_summary_batch,
    estimate_messages_tokens,
    limit_tool_content,
    pending_message_count,
)
from app.services.memories import MemoryService
from app.services.models import model_registry
from langchain_core.messages import AIMessage, HumanMessage


def _profile(*, input_soft_limit: int = 512) -> ModelProfile:
    return ModelProfile(
        alias="test",
        model="unconfigured",
        base_url="https://api.example.com/v1",
        api_key_env="PERSONAL_AGENT_TEST_KEY",
        context_window=1_000_000,
        input_soft_limit=input_soft_limit,
        max_output_tokens=16_384,
    )


def test_context_builder_keeps_latest_message_within_budget() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    history = [
        Message(
            id=f"context-message-{index}",
            conversation_id="context-conversation",
            sender_id="user",
            role="user" if index % 2 == 0 else "assistant",
            content=f"历史消息 {index} " + ("很长的内容 " * 30),
            created_at=now + timedelta(seconds=index),
        )
        for index in range(20)
    ]
    snapshot = build_context_messages("系统约束", history, _profile(input_soft_limit=700))
    assert snapshot.messages[-1].content == history[-1].content
    assert snapshot.estimated_tokens <= snapshot.soft_limit
    assert snapshot.candidate_count == len(history)


def test_summary_batch_is_incremental_and_bounded() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        Message(
            id=f"summary-message-{index}",
            conversation_id="summary-conversation",
            sender_id="user",
            role="user",
            content=f"第 {index} 条历史 " + ("内容 " * 200),
            created_at=now + timedelta(seconds=index),
        )
        for index in range(256)
    ]
    batch = choose_summary_batch(rows, "此前摘要：用户在学习 Agent。")
    assert batch
    assert batch[0].id == rows[0].id
    assert batch[-1].id != rows[-1].id
    assert estimate_messages_tokens(
        [
            HumanMessage(content="\n".join(item.content for item in batch))
        ]
    ) <= 56_000


def test_group_memory_does_not_recall_private_memory(monkeypatch) -> None:
    class Provider:
        def embed_query(self, _query: str) -> list[float]:
            return [1.0]

    monkeypatch.setattr("app.services.memories.get_embedding_provider", lambda _profile: Provider())
    monkeypatch.setattr(
        "app.services.memories.vector_store.query",
        lambda collection, _embedding, _limit: (
            [
                {"id": "global-context-memory"},
                {"id": "group-context-memory"},
                {"id": "private-context-memory"},
            ]
            if "global" in collection or "group-group-42" in collection
            else []
        ),
    )
    with SessionLocal() as session:
        session.add_all(
            [
                Memory(
                    id="global-context-memory",
                    scope_type="global",
                    user_id=None,
                    fact_key="policy.language",
                    content="全局使用中文",
                ),
                Memory(
                    id="group-context-memory",
                    scope_type="group",
                    user_id="group:42",
                    fact_key="project.topic",
                    content="本群讨论 PersonalAgent",
                ),
                Memory(
                    id="private-context-memory",
                    scope_type="user",
                    user_id="alice",
                    fact_key="preference.secret",
                    content="Alice 的私聊事实",
                ),
            ]
        )
        session.commit()
        memories = MemoryService(session).recall(
            "alice",
            "讨论",
            scope_type="group",
            scope_id="group:42",
        )
        assert {item.id for item in memories} == {"global-context-memory", "group-context-memory"}


def test_tool_result_is_valid_json_and_bounded() -> None:
    content = json.dumps(
        {"kind": "tool_result", "data": {"evidence": [{"content": "证据 " * 20_000}]}},
        ensure_ascii=False,
    )
    limited = limit_tool_content(content)
    assert len(limited) <= TOOL_RESULT_MAX_CHARS
    parsed = json.loads(limited)
    assert parsed["kind"] == "tool_result"


def test_incremental_compaction_updates_boundary(monkeypatch) -> None:
    class FakeSummaryModel:
        def bind(self, **_kwargs):
            return self

        def invoke(self, _messages):
            return AIMessage(content="已压缩的历史摘要")

    monkeypatch.setattr(model_registry, "chat_model", lambda _alias: FakeSummaryModel())
    conversation = Conversation(title="增量摘要测试", owner_id="summary-owner")
    with SessionLocal() as session:
        session.add(conversation)
        session.flush()
        now = datetime.now(UTC).replace(tzinfo=None)
        for index in range(50):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    sender_id="summary-owner",
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"历史 {index}",
                    created_at=now + timedelta(seconds=index),
                )
            )
        session.commit()
        service = ChatService()
        refreshed = session.get(Conversation, conversation.id)
        assert refreshed is not None
        service._compact_conversation(session, refreshed, "default")
        session.refresh(refreshed)
        assert refreshed.summary == "已压缩的历史摘要"
        assert refreshed.summary_up_to_message_id is not None
        assert pending_message_count(session, refreshed) == 12


def test_qq_new_archives_previous_conversation() -> None:
    external_id = "private:test-context-rotation"
    with SessionLocal.begin() as session:
        persona = Persona(name="rotation-persona", raw_prompt="保持简洁")
        session.add(persona)
        session.flush()
        session.add(
            Conversation(
                platform="qq",
                external_id=external_id,
                title="待归档会话",
                owner_id="10001",
                persona_id=persona.id,
            )
        )
    response = asyncio.run(OneBotManager()._command("/new", "10001", external_id))
    assert response is not None and "归档" in response
    with SessionLocal() as session:
        current = session.query(Conversation).filter_by(platform="qq", external_id=external_id).one()
        assert current.persona_id is not None
        archived = session.query(Conversation).filter(
            Conversation.external_id.like("archive:%"),
            Conversation.title.like("%已归档%"),
        ).one()
        assert "已归档" in archived.title
