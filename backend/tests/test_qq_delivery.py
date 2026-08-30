from __future__ import annotations

import asyncio
from uuid import uuid4

from app.api import onebot as onebot_module
from app.api.onebot import OneBotManager
from app.db.models import AppSetting, Conversation, Message
from app.db.session import SessionLocal
from app.main import app
from app.services.qq_delivery import chunk_length_bounds, split_qq_reply
from fastapi.testclient import TestClient
from sqlalchemy import select


def test_split_qq_reply_keeps_short_text_and_semantic_order() -> None:
    assert split_qq_reply("你好，今天还好吗？") == ["你好，今天还好吗？"]

    text = (
        "第一段先说说发生了什么。第二段补充当时的感受，也可以慢慢想，"
        "不用急着马上得出结论。最后再一起看看接下来想怎么做。"
    )
    chunks = split_qq_reply(text)

    assert len(chunks) > 1
    assert "".join(chunks) == text
    minimum, maximum = chunk_length_bounds(20)
    assert all(len(chunk) <= maximum for chunk in chunks)
    assert any(len(chunk) >= minimum for chunk in chunks)


def test_chunk_length_bounds_follow_configured_target() -> None:
    assert chunk_length_bounds(10) == (7, 13)
    assert chunk_length_bounds(20) == (14, 26)
    assert chunk_length_bounds(4) == (14, 26)
    assert chunk_length_bounds(101) == (14, 26)


def test_split_qq_reply_keeps_urls_and_code_blocks_atomic() -> None:
    url = "https://example.com/a-very-long-path-with-many-characters-1234567890"
    code = "```python\nprint('hello')\nfor index in range(10):\n    print(index)\n```"
    text = f"链接在这里：{url}。代码也放在下面：{code}。看完告诉我结果。"

    chunks = split_qq_reply(text)

    assert "".join(chunks) == text
    assert any(url in chunk for chunk in chunks)
    assert any(code in chunk for chunk in chunks)


def test_split_qq_reply_limits_extremely_long_replies_without_dropping_text() -> None:
    sentence = "这是一段足够长的连续回复内容，用来验证超长消息的分批上限。"
    text = sentence * 20

    chunks = split_qq_reply(text)

    assert len(chunks) <= 12
    assert "".join(chunks) == text


def test_split_qq_reply_preserves_internal_english_whitespace() -> None:
    text = (
        "This is a long English sentence that should keep spaces between words when it is divided "
        "into multiple natural chunks, even though the target length is relatively short."
    )

    chunks = split_qq_reply(text)

    assert len(chunks) > 1
    assert "".join(chunks) == text


def test_qq_reply_settings_default_and_persist() -> None:
    with SessionLocal.begin() as session:
        for key in (
            onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY,
            onebot_module.QQ_CHUNK_TARGET_SETTING_KEY,
        ):
            setting = session.get(AppSetting, key)
            if setting is not None:
                session.delete(setting)

    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/onebot/reply-settings")
            assert response.status_code == 200
            assert response.json() == {
                "chunked_output_enabled": True,
                "chunk_target_chars": 20,
                "chunk_min_chars": 14,
                "chunk_max_chars": 26,
            }

            response = client.put(
                "/api/v1/onebot/reply-settings",
                json={"chunked_output_enabled": False},
            )
            assert response.status_code == 200
            assert response.json() == {
                "chunked_output_enabled": False,
                "chunk_target_chars": 20,
                "chunk_min_chars": 14,
                "chunk_max_chars": 26,
            }

            response = client.put(
                "/api/v1/onebot/reply-settings",
                json={"chunk_target_chars": 10},
            )
            assert response.status_code == 200
            assert response.json()["chunk_target_chars"] == 10
            assert response.json()["chunk_min_chars"] == 7
            assert response.json()["chunk_max_chars"] == 13

            response = client.put(
                "/api/v1/onebot/reply-settings",
                json={"chunk_target_chars": 101},
            )
            assert response.status_code == 422

            response = client.get("/api/v1/onebot/reply-settings")
            assert response.status_code == 200
            assert response.json()["chunked_output_enabled"] is False
            assert response.json()["chunk_target_chars"] == 10
    finally:
        with SessionLocal.begin() as session:
            for key in (
                onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY,
                onebot_module.QQ_CHUNK_TARGET_SETTING_KEY,
            ):
                setting = session.get(AppSetting, key)
                if setting is not None:
                    session.delete(setting)


def test_onebot_sends_reviewed_reply_in_order_and_keeps_one_message(monkeypatch) -> None:
    async def scenario() -> None:
        manager = OneBotManager()
        sent: list[str] = []
        owner_id = str(9_000_000_000 + (uuid4().int % 900_000_000))
        external_id = f"private:{owner_id}"
        reply = (
            "第一句先接住你的表达，然后再补充一些背景和感受。最后我们再看看接下来想怎么做，"
            "不用急着现在就决定，慢慢来就好。"
        )

        async def fake_stream(session, conversation, *_args: object, **_kwargs: object):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    sender_id="assistant",
                    role="assistant",
                    content=reply,
                )
            )
            session.commit()
            yield {"event": "final", "data": {"text": reply, "response_source": "agent"}}

        async def fake_send(_user_id: str, text: str, _group_id: str | None = None) -> None:
            sent.append(text)

        monkeypatch.setattr(onebot_module.chat_service, "stream", fake_stream)
        monkeypatch.setattr(manager, "send_text", fake_send)
        monkeypatch.setattr(onebot_module, "qq_reply_delay_seconds", lambda _text: 0.0)
        with SessionLocal.begin() as session:
            setting = session.get(AppSetting, onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY)
            if setting is None:
                session.add(AppSetting(key=onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY, value="true"))
            else:
                setting.value = "true"

        try:
            await manager._run_chat_response(owner_id, "用户输入", "message-qq-stream", None, external_id)
            assert len(sent) > 1
            assert "".join(sent) == reply
            with SessionLocal() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                assert conversation is not None
                messages = session.scalars(
                    select(Message).where(Message.conversation_id == conversation.id)
                ).all()
                assert [item.content for item in messages if item.role == "assistant"] == [reply]
        finally:
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is not None:
                    session.delete(conversation)

    asyncio.run(scenario())


def test_onebot_reply_setting_off_sends_one_message(monkeypatch) -> None:
    async def scenario() -> None:
        manager = OneBotManager()
        sent: list[str] = []
        owner_id = str(9_000_000_000 + (uuid4().int % 900_000_000))
        external_id = f"private:{owner_id}"
        reply = "第一句先说清楚。第二句再补一点背景，确保关闭开关时仍然一次完整发送。"

        async def fake_stream(*_args: object, **_kwargs: object):
            yield {"event": "final", "data": {"text": reply, "response_source": "agent"}}

        async def fake_send(_user_id: str, text: str, _group_id: str | None = None) -> None:
            sent.append(text)

        monkeypatch.setattr(onebot_module.chat_service, "stream", fake_stream)
        monkeypatch.setattr(manager, "send_text", fake_send)
        with SessionLocal.begin() as session:
            setting = session.get(AppSetting, onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY)
            if setting is None:
                session.add(AppSetting(key=onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY, value="false"))
            else:
                setting.value = "false"
        try:
            await manager._run_chat_response(owner_id, "用户输入", "message-qq-single", None, external_id)
            assert sent == [reply]
        finally:
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is not None:
                    session.delete(conversation)
                setting = session.get(AppSetting, onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY)
                if setting is not None:
                    session.delete(setting)

    asyncio.run(scenario())


def test_onebot_safety_redirect_stays_single_and_failed_chunk_stops(monkeypatch) -> None:
    async def scenario() -> None:
        manager = OneBotManager()
        owner_id = str(9_000_000_000 + (uuid4().int % 900_000_000))
        external_id = f"private:{owner_id}"
        sent: list[str] = []
        safety_reply = "我先陪你把眼前这一刻稳住。如果你正处在紧急危险中，请立即联系当地急救或危机支持。"
        long_reply = (
            "第一段先接住你的表达并说明当前情况。第二段继续补充一些内容，"
            "让这条回复足够长，能够拆成多个消息。第三段再说最后一句。"
        )
        mode = "safety"

        async def fake_stream(*_args: object, **_kwargs: object):
            if mode == "safety":
                yield {"event": "companion_analysis", "data": {"safety_intercepted": True}}
                yield {"event": "final", "data": {"text": safety_reply, "response_source": "safety_llm"}}
            else:
                yield {"event": "final", "data": {"text": long_reply, "response_source": "agent"}}

        async def fake_send(_user_id: str, text: str, _group_id: str | None = None) -> None:
            sent.append(text)
            if mode == "failure" and len(sent) == 2:
                raise RuntimeError("send failed")

        monkeypatch.setattr(onebot_module.chat_service, "stream", fake_stream)
        monkeypatch.setattr(manager, "send_text", fake_send)
        monkeypatch.setattr(onebot_module, "qq_reply_delay_seconds", lambda _text: 0.0)
        with SessionLocal.begin() as session:
            setting = session.get(AppSetting, onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY)
            if setting is None:
                session.add(AppSetting(key=onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY, value="true"))
            else:
                setting.value = "true"
        try:
            await manager._run_chat_response(owner_id, "高风险输入", "message-safety", None, external_id)
            assert sent == [safety_reply]

            sent.clear()
            mode = "failure"
            await manager._run_chat_response(owner_id, "普通输入", "message-failure", None, external_id)
            assert len(sent) == 2
        finally:
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is not None:
                    session.delete(conversation)
                setting = session.get(AppSetting, onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY)
                if setting is not None:
                    session.delete(setting)

    asyncio.run(scenario())


def test_onebot_fallback_reply_stays_single(monkeypatch) -> None:
    async def scenario() -> None:
        manager = OneBotManager()
        owner_id = str(9_000_000_000 + (uuid4().int % 900_000_000))
        external_id = f"private:{owner_id}"
        sent: list[str] = []
        fallback = "这是一个较长的降级回复，应该完整地一次发送，而不是进入自然分批节奏。"

        async def fake_stream(*_args: object, **_kwargs: object):
            yield {"event": "final", "data": {"text": fallback, "response_source": "template"}}

        async def fake_send(_user_id: str, text: str, _group_id: str | None = None) -> None:
            sent.append(text)

        monkeypatch.setattr(onebot_module.chat_service, "stream", fake_stream)
        monkeypatch.setattr(manager, "send_text", fake_send)
        with SessionLocal.begin() as session:
            setting = session.get(AppSetting, onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY)
            if setting is None:
                session.add(AppSetting(key=onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY, value="true"))
            else:
                setting.value = "true"
        try:
            await manager._run_chat_response(owner_id, "普通输入", "message-fallback", None, external_id)
            assert sent == [fallback]
        finally:
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is not None:
                    session.delete(conversation)
                setting = session.get(AppSetting, onebot_module.QQ_CHUNKED_OUTPUT_SETTING_KEY)
                if setting is not None:
                    session.delete(setting)

    asyncio.run(scenario())


def test_onebot_reply_lock_serializes_same_conversation(monkeypatch) -> None:
    async def scenario() -> None:
        manager = OneBotManager()
        owner_id = str(9_000_000_000 + (uuid4().int % 900_000_000))
        external_id = f"private:{owner_id}"
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def fake_stream(*args: object, **_kwargs: object):
            text = str(args[3])
            if text == "第一轮":
                started.set()
                await release.wait()
            yield {"event": "final", "data": {"text": text, "response_source": "agent"}}

        async def fake_send(_user_id: str, text: str, _group_id: str | None = None) -> None:
            calls.append(text)

        monkeypatch.setattr(onebot_module.chat_service, "stream", fake_stream)
        monkeypatch.setattr(manager, "send_text", fake_send)
        try:
            first = asyncio.create_task(
                manager._run_chat_response(owner_id, "第一轮", "message-lock-1", None, external_id)
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            second = asyncio.create_task(
                manager._run_chat_response(owner_id, "第二轮", "message-lock-2", None, external_id)
            )
            await asyncio.sleep(0)
            assert not second.done()
            release.set()
            await asyncio.gather(first, second)
            assert calls == ["第一轮", "第二轮"]
        finally:
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    __import__("sqlalchemy").select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is not None:
                    session.delete(conversation)

    asyncio.run(scenario())
