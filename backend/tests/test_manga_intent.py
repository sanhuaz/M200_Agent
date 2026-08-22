from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.db.models import Conversation, Message, ToolRun
from app.db.session import SessionLocal
from app.services.chat import _has_immediate_search_results
from app.services.manga_intent import detect_manga_intent
from app.workflows.agent import tool_envelope


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("搜索漫画海贼王", {"search"}),
        ("帮我找本子", {"search"}),
        ("查询 JM123", {"search"}),
        ("下载 JM123", {"download"}),
        ("下载这部漫画", {"download"}),
        ("删除漫画任务 abc123", {"delete"}),
        ("搜索漫画并下载 JM123", {"search", "download"}),
        ("搜索漫画、下载 JM123、删除漫画任务 abc123", {"search", "download", "delete"}),
    ],
)
def test_explicit_manga_intent(text: str, expected: set[str]) -> None:
    assert detect_manga_intent(text).actions == frozenset(expected)


@pytest.mark.parametrize(
    "text",
    [
        "搜一下这个",
        "下载 123",
        "这个词是什么意思",
        "帮我查一下漫画这个词是什么意思",
        "请告诉我如何搜索漫画",
        "你会搜索漫画吗",
        "不要搜索漫画",
        "普通聊天里提到漫画",
        "我昨天搜索漫画",
    ],
)
def test_ambiguous_or_negative_text_does_not_open_manga_tools(text: str) -> None:
    assert not detect_manga_intent(text).actions


def test_followup_download_requires_immediate_non_empty_search() -> None:
    assert detect_manga_intent("下载第一个", previous_search_results=True).actions == frozenset(
        {"download"}
    )
    assert not detect_manga_intent("下载第一个", previous_search_results=False).actions
    assert not detect_manga_intent("不要下载第一个", previous_search_results=True).actions
    assert not detect_manga_intent("你会下载第一个吗", previous_search_results=True).actions


def test_previous_non_empty_search_is_only_valid_for_the_immediate_turn() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    with SessionLocal.begin() as session:
        conversation = Conversation(title="漫画紧邻追问测试")
        session.add(conversation)
        session.flush()
        previous_user = Message(
            conversation_id=conversation.id,
            sender_id="manga-followup-user",
            role="user",
            content="搜索漫画海贼王",
            created_at=now,
        )
        previous_assistant = Message(
            conversation_id=conversation.id,
            sender_id="assistant",
            role="assistant",
            content="找到候选",
            created_at=now + timedelta(seconds=1),
        )
        current = Message(
            conversation_id=conversation.id,
            sender_id="manga-followup-user",
            role="user",
            content="下载第一个",
            created_at=now + timedelta(seconds=2),
        )
        session.add_all([previous_user, previous_assistant, current])
        session.add(
            ToolRun(
                conversation_id=conversation.id,
                tool_name="search_manga",
                arguments="{}",
                result=tool_envelope({"results": [{"album_id": "123"}]}),
                status="succeeded",
                created_at=now + timedelta(milliseconds=500),
            )
        )
        session.flush()
        assert _has_immediate_search_results(session, conversation, current.id)

        intervening = Message(
            conversation_id=conversation.id,
            sender_id="manga-followup-user",
            role="user",
            content="换个话题",
            created_at=now + timedelta(seconds=2, milliseconds=500),
        )
        session.add(intervening)
        next_message = Message(
            conversation_id=conversation.id,
            sender_id="manga-followup-user",
            role="user",
            content="下载第二个",
            created_at=now + timedelta(seconds=3),
        )
        session.add(next_message)
        session.flush()
        assert not _has_immediate_search_results(session, conversation, next_message.id)
