from __future__ import annotations

import asyncio
from uuid import uuid4

from app.api.onebot import OneBotManager
from app.db.models import (
    AdminIdentity,
    CompanionPreference,
    Conversation,
    Message,
)
from app.db.session import SessionLocal
from app.main import app
from app.services.companion import response_style_violations
from app.services.strategy_guides import DEFAULT_STRATEGY_GUIDES
from fastapi.testclient import TestClient
from sqlalchemy import select


def test_structured_persona_card_is_runtime_config_and_legacy_is_rejected() -> None:
    name = f"card-persona-{uuid4()}"
    legacy_name = f"legacy-persona-{uuid4()}"
    card = {
        "identity": {"role": "住在海边的电台编辑", "setting": "近未来海港"},
        "voice": {"sentence_length": "短句", "catchphrases": ["慢慢来"]},
        "boundaries": {"avoid_machine_tone": ["不要写客服式总结"]},
    }
    with TestClient(app) as client:
        created = client.post("/api/v1/personas", json={"name": name, "card": card})
        assert created.status_code == 200
        row = created.json()
        assert row["status"] == "active"
        assert row["card"]["identity"]["role"] == "住在海边的电台编辑"
        assert "legacy_prompt" not in row

        legacy = client.post(
            "/api/v1/personas",
            json={"name": legacy_name, "raw_prompt": "保留这段旧人格草稿"},
        )
        assert legacy.status_code == 422

        assert client.delete(f"/api/v1/personas/{row['id']}").status_code == 200


def test_strategy_guides_are_versioned_and_safety_redirect_is_not_editable() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/companion/strategy-guides")
        assert response.status_code == 200
        rows = response.json()
        assert {row["strategy"] for row in rows} == {
            "listen", "validate", "clarify", "comfort", "reflect", "advise", "celebrate"
        }
        current = next(row for row in rows if row["strategy"] == "listen")
        edited = client.put(
            "/api/v1/companion/strategy-guides/listen",
            json={"prompt_text": "先接住表达，保持角色自然说话。", "expected_version": current["version"]},
        )
        assert edited.status_code == 200
        assert edited.json()["version"] == current["version"] + 1
        conflict = client.put(
            "/api/v1/companion/strategy-guides/listen",
            json={"prompt_text": "过期版本", "expected_version": current["version"]},
        )
        assert conflict.status_code == 409
        revisions = client.get("/api/v1/companion/strategy-guides/listen/revisions")
        assert revisions.status_code == 200
        assert len(revisions.json()) >= 2
        reset = client.post(
            "/api/v1/companion/strategy-guides/listen/reset",
            json={"expected_version": edited.json()["version"]},
        )
        assert reset.status_code == 200
        assert reset.json()["prompt_text"] == DEFAULT_STRATEGY_GUIDES["listen"]
        assert client.put(
            "/api/v1/companion/strategy-guides/safety_redirect",
            json={"prompt_text": "不能编辑", "expected_version": 1},
        ).status_code == 400


def test_onebot_normalizes_non_text_events_and_buffers_owner_fragments() -> None:
    assert OneBotManager._text_from_event({"raw_message": "[CQ:file,file=a.txt,url=x]"}) == ""
    assert OneBotManager._text_from_event({"message": [{"type": "image", "data": {}}]}) == ""
    assert OneBotManager._text_from_event(
        {"message_type": "group", "raw_message": "[CQ:at,qq=10001] 你好"}
    ) == "[CQ:at,qq=10001] 你好"

    async def scenario() -> None:
        manager = OneBotManager()
        calls: list[tuple[str, str]] = []
        owner_id = str(9000000000 + (uuid4().int % 900000000))
        external_id = f"private:{owner_id}"

        async def fake_run(user_id: str, text: str, *_args: object) -> None:
            calls.append((user_id, text))

        async def fake_send(*_args: object, **_kwargs: object) -> None:
            return None

        manager._run_chat_response = fake_run  # type: ignore[method-assign]
        manager.send_text = fake_send  # type: ignore[method-assign]
        with SessionLocal.begin() as session:
            session.add(AdminIdentity(platform="qq", external_id=owner_id, enabled=True, created_by="test"))
        with SessionLocal.begin() as session:
            from app.services.companion import get_or_create_preference

            preference = get_or_create_preference(session, owner_id)
            preference.companion_enabled = True
            preference.listening_enabled = True
            preference.listening_silence_seconds = 30
        assert await manager._handle_listening_input("第一段", owner_id, external_id, "m1") == ""
        assert await manager._handle_listening_input("第二段", owner_id, external_id, "m2") == ""
        assert await manager._handle_listening_input("/listening done", owner_id, external_id, "m3") == ""
        assert calls == [(owner_id, "第一段\n第二段")]
        with SessionLocal.begin() as session:
            conversation = session.scalar(select(Conversation).where(
                Conversation.platform == "qq", Conversation.external_id == external_id
            ))
            if conversation is not None:
                session.delete(conversation)
            preference = session.scalar(
                select(CompanionPreference).where(CompanionPreference.scope_id == owner_id)
            )
            if preference is not None:
                session.delete(preference)
            admin = session.scalar(select(AdminIdentity).where(AdminIdentity.external_id == owner_id))
            if admin is not None:
                session.delete(admin)

    asyncio.run(scenario())


def test_listening_processing_keeps_next_batch_without_duplicate_calls() -> None:
    async def scenario() -> None:
        manager = OneBotManager()
        calls: list[str] = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        owner_id = str(9000000000 + (uuid4().int % 900000000))
        external_id = f"private:{owner_id}"

        async def fake_run(_user_id: str, text: str, *_args: object) -> None:
            calls.append(text)
            if text == "第一批":
                first_started.set()
                await release_first.wait()

        manager._run_chat_response = fake_run  # type: ignore[method-assign]
        with SessionLocal.begin() as session:
            session.add(AdminIdentity(platform="qq", external_id=owner_id, enabled=True, created_by="test"))
        with SessionLocal.begin() as session:
            from app.services.companion import get_or_create_preference

            preference = get_or_create_preference(session, owner_id)
            preference.companion_enabled = True
            preference.listening_enabled = True
            preference.listening_silence_seconds = 30
        try:
            assert await manager._handle_listening_input("第一批", owner_id, external_id, "m1") == ""
            first_flush = asyncio.create_task(manager._flush_listening_now(owner_id, external_id))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            assert await manager._handle_listening_input("第二批", owner_id, external_id, "m2") == ""
            second_flush = asyncio.create_task(manager._flush_listening_now(owner_id, external_id))
            await asyncio.sleep(0)
            assert not second_flush.done()
            release_first.set()
            await first_flush
            await second_flush
            assert calls == ["第一批", "第二批"]
        finally:
            for task in list(manager._listening_tasks.values()):
                task.cancel()
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is not None:
                    session.delete(conversation)
                preference = session.scalar(
                    select(CompanionPreference).where(CompanionPreference.scope_id == owner_id)
                )
                if preference is not None:
                    session.delete(preference)
                admin = session.scalar(select(AdminIdentity).where(AdminIdentity.external_id == owner_id))
                if admin is not None:
                    session.delete(admin)

    asyncio.run(scenario())


def test_onebot_empty_file_receipt_does_not_create_message_or_call_chat() -> None:
    async def scenario() -> None:
        manager = OneBotManager()
        called = False

        async def fake_run(*_args: object, **_kwargs: object) -> None:
            nonlocal called
            called = True

        manager._run_chat_response = fake_run  # type: ignore[method-assign]
        before: int
        with SessionLocal() as session:
            before = session.query(Message).count()
        await manager._handle_message(
            {
                "message_id": f"empty-{uuid4()}",
                "message_type": "private",
                "user_id": "10001",
                "raw_message": "[CQ:file,file=received.txt,url=https://example.invalid]",
            }
        )
        with SessionLocal() as session:
            assert session.query(Message).count() == before
        assert called is False

    asyncio.run(scenario())


def test_companion_style_guard_only_flags_high_signal_drift() -> None:
    assert "advice_overreach" in response_style_violations(
        "你可以先列一个计划。", "我只听我说，不用建议", "listen"
    )
    assert "structured_list" in response_style_violations(
        "1. 第一件事\n2. 第二件事\n3. 第三件事", "帮我说说", "reflect"
    )
    assert response_style_violations("可以给你三个建议：", "给我建议", "advise") == []
