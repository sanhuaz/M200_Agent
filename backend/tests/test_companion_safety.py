from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from app.api.onebot import OneBotManager
from app.db.models import CompanionPreference, Conversation, EmotionAssessment, Message, SafetyEvent
from app.db.session import SessionLocal
from app.main import app
from app.services import chat as chat_module
from app.services import companion
from app.services.chat import chat_service
from app.services.companion import CompanionAnalysis, SafetyAssessment, analyze_message, assessment_dict
from app.services.models import model_registry
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage


def test_standard_high_safety_redirect_hides_classifier_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: pytest.fail("standard high risk must not call emotion classifier"),
    )
    analysis = analyze_message(
        "我现在准备自杀",
        "",
        "default",
        safety=companion.safety_precheck("我现在准备自杀"),
        safety_mode="standard",
    )
    assert analysis.analysis_status == "safety_redirected"
    assert analysis.next_action == "safety_redirect"
    assert analysis.model_alias is None

    with SessionLocal.begin() as session:
        item = EmotionAssessment(
            user_message_id=str(uuid.uuid4()),
            scope_id="10001",
            primary_emotion=analysis.primary_emotion,
            candidate_emotions=json.dumps(analysis.candidate_emotions),
            intensity=analysis.intensity,
            support_need=analysis.support_need,
            confidence=analysis.confidence,
            risk_level=analysis.risk_level,
            next_action=analysis.next_action,
            model_alias=analysis.model_alias,
            schema_valid=True,
        )
        session.add(item)
        session.flush()
        view = assessment_dict(item, session)
        assert view["analysis_status"] == "safety_redirected"
        assert view["safety_intercepted"] is True
        assert view["candidate_emotions"] == []
        assert view["primary_emotion"] is None
        assert view["confidence"] is None
        session.delete(item)


def test_unfiltered_high_calls_classifier_and_keeps_normal_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: (
            {
                "event": "高风险文本",
                "primary_emotion": "fear",
                "candidate_emotions": ["fear"],
                "intensity": "high",
                "support_need": "comfort",
                "confidence": 0.9,
                "risk_level": "high",
            },
            True,
        ),
    )
    analysis = analyze_message(
        "我不想活了",
        "",
        "default",
        safety=companion.safety_precheck("我不想活了"),
        safety_mode="unfiltered",
    )
    assert analysis.analysis_status == "valid"
    assert analysis.risk_level == "high"
    assert analysis.next_action == "comfort"
    assert analysis.model_alias == "default"


def test_safety_and_rewrite_prompts_receive_context_and_rule_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[tuple[str, str]] = []

    def fake_text(_alias: str, system_prompt: str, human_prompt: str) -> str:
        prompts.append((system_prompt, human_prompt))
        return "我先陪你确认现在是否安全，好吗？"

    monkeypatch.setattr(companion, "_invoke_text_model", fake_text)
    safety = SafetyAssessment("high", ("self_harm",))
    result = companion.generate_safety_response(
        "default",
        "我不想活了",
        "user: 昨晚一直睡不着",
        "你是一个克制、温柔的角色",
        safety,
    )
    assert result is not None
    assert "我不想活了" in prompts[0][1]
    assert "昨晚一直睡不着" in prompts[0][1]
    assert "self_harm" in prompts[0][1]
    assert "角色" in prompts[0][1]

    rewritten = companion.rewrite_blocked_response(
        "default", "我很难受", "你只能依靠我", ["dependency_exclusivity"]
    )
    assert rewritten is not None
    assert len(prompts) == 2
    assert "dependency_exclusivity" in prompts[1][1]


def test_safety_text_failure_falls_back_and_output_violation_codes() -> None:
    assert companion.output_safety_violations("你只能依靠我，而且都是你的错") == [
        "dependency_exclusivity",
        "blame_or_shame",
    ]
    assert companion.output_safety_ok("我会认真听你说") is True


def test_companion_preference_api_safety_mode_and_invalid_value() -> None:
    with TestClient(app) as client:
        initial = client.get("/api/v1/companion/preferences?qq_user_id=10001")
        assert initial.status_code == 200
        assert initial.json()["safety_mode"] == "standard"
        invalid = client.put(
            "/api/v1/companion/preferences",
            json={"qq_user_id": "10001", "safety_mode": "unsafe"},
        )
        assert invalid.status_code == 422
        updated = client.put(
            "/api/v1/companion/preferences",
            json={"qq_user_id": "10001", "safety_mode": "unfiltered"},
        )
        assert updated.status_code == 200
        assert updated.json()["safety_mode"] == "unfiltered"
        restored = client.put(
            "/api/v1/companion/preferences",
            json={"qq_user_id": "10001", "safety_mode": "standard"},
        )
        assert restored.status_code == 200
        assert restored.json()["safety_mode"] == "standard"


def test_onebot_safety_commands_require_confirmation_and_owner_private_scope() -> None:
    manager = OneBotManager()
    assert "确认" in asyncio.run(
        manager._command("/companion safety unfiltered", "10001", "private:10001")
    )
    assert "只有 Owner" in asyncio.run(
        manager._command("/companion status", "20002", "private:20002")
    )
    assert "仅支持 QQ 私聊" in asyncio.run(
        manager._command("/companion status", "10001", "group:20001")
    )
    assert "已启用无过滤模式" in asyncio.run(
        manager._command("/companion safety unfiltered confirm", "10001", "private:10001")
    )
    status = asyncio.run(manager._command("/companion status", "10001", "private:10001"))
    assert status is not None and "无过滤" in status
    assert asyncio.run(
        manager._command("/companion safety standard", "10001", "private:10001")
    ) == "已切回标准防护模式。"


def test_companion_analysis_event_exposes_mode_and_hides_intercepted_labels() -> None:
    redirected = CompanionAnalysis(
        primary_emotion="neutral",
        candidate_emotions=["neutral"],
        risk_level="critical",
        next_action="safety_redirect",
        confidence=1.0,
        analysis_status="safety_redirected",
    )
    event = chat_service._companion_analysis_event(redirected, "standard")
    assert event["analysis_status"] == "safety_redirected"
    assert event["safety_intercepted"] is True
    assert event["safety_mode"] == "standard"
    assert event["candidate_emotions"] == []
    model_redirected = redirected.model_copy(update={"model_alias": "default", "analysis_status": "valid"})
    model_event = chat_service._companion_analysis_event(model_redirected, "standard")
    assert model_event["analysis_status"] == "safety_redirected"
    assert model_event["candidate_emotions"] == []


class _SafetyGraphModel:
    def bind(self, **_kwargs):
        return self

    def invoke(self, _messages):
        return AIMessage(content="我先陪你确认现在是否安全，可以吗？")

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content="普通 Agent 回复")


class _FakeGraph:
    final_text = "普通 Agent 回复"

    async def astream(self, _input, config=None, stream_mode=None):
        yield "values", {"messages": [AIMessage(content=self.final_text)]}


class _UnsafeGraph(_FakeGraph):
    final_text = "你只能依靠我"


def test_unfiltered_high_enters_normal_graph_but_standard_uses_safety_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_registry, "chat_model", lambda _alias: _SafetyGraphModel())
    monkeypatch.setattr(chat_module, "build_agent_graph", lambda *_args, **_kwargs: _FakeGraph())
    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: (
            {
                "event": "测试",
                "primary_emotion": "fear",
                "candidate_emotions": ["fear"],
                "intensity": "high",
                "support_need": "comfort",
                "confidence": 0.9,
                "risk_level": "high",
            },
            True,
        ),
    )
    monkeypatch.setattr(chat_module, "generate_safety_response", lambda *_args: "安全模型回复")
    owner = "10001"
    conversation_ids: list[str] = []

    def make_conversation(mode: str) -> str:
        with SessionLocal.begin() as session:
            preference = companion.get_or_create_preference(session, owner)
            preference.companion_enabled = True
            preference.safety_mode = mode
            conversation = Conversation(
                platform="qq",
                external_id=f"private:{owner}:safety-{uuid.uuid4()}",
                conversation_type="private",
                title="安全模式测试",
                model_alias="default",
                owner_id=owner,
            )
            session.add(conversation)
            session.flush()
            conversation_ids.append(conversation.id)
            return conversation.id

    async def collect(conversation_id: str, text: str) -> list[dict[str, object]]:
        with SessionLocal() as session:
            conversation = session.get(Conversation, conversation_id)
            assert conversation is not None
            return [
                event
                async for event in chat_service.stream(
                    session, conversation, owner, text, platform="qq", is_group=False
                )
            ]

    try:
        standard_events = asyncio.run(collect(make_conversation("standard"), "我不想活了"))
        standard_final = next(item for item in standard_events if item["event"] == "final")
        assert standard_final["data"]["response_source"] == "safety_llm"

        unfiltered_events = asyncio.run(collect(make_conversation("unfiltered"), "我不想活了"))
        unfiltered_final = next(item for item in unfiltered_events if item["event"] == "final")
        assert unfiltered_final["data"]["response_source"] == "agent"
        with SessionLocal() as session:
            messages = session.query(Message).filter(Message.conversation_id == conversation_ids[-1]).all()
            user_message = next(item for item in messages if item.role == "user")
            events = session.query(SafetyEvent).filter(SafetyEvent.message_id == user_message.id).all()
            assert any(item.action == "unfiltered_passthrough" for item in events)
    finally:
        with SessionLocal.begin() as session:
            for conversation_id in conversation_ids:
                messages = session.query(Message).filter(Message.conversation_id == conversation_id).all()
                for message in messages:
                    session.query(SafetyEvent).filter(
                        SafetyEvent.message_id == message.id
                    ).delete()
                    session.query(EmotionAssessment).filter(
                        EmotionAssessment.user_message_id == message.id
                    ).delete()
                    session.delete(message)
                conversation = session.get(Conversation, conversation_id)
                if conversation is not None:
                    session.delete(conversation)
            preference = session.query(CompanionPreference).filter_by(scope_id=owner).one_or_none()
            if preference is not None:
                preference.safety_mode = "standard"


def test_standard_output_review_rewrites_once_then_sends_safe_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "10001"
    conversation_id: str | None = None
    monkeypatch.setattr(model_registry, "chat_model", lambda _alias: _SafetyGraphModel())
    monkeypatch.setattr(chat_module, "build_agent_graph", lambda *_args, **_kwargs: _UnsafeGraph())
    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: (
            {
                "event": "测试",
                "primary_emotion": "sadness",
                "candidate_emotions": ["sadness"],
                "intensity": "medium",
                "support_need": "comfort",
                "confidence": 0.9,
                "risk_level": "low",
            },
            True,
        ),
    )
    captured: list[list[str]] = []

    def fake_rewrite(_alias: str, _user_text: str, _candidate: str, codes: list[str]) -> str:
        captured.append(codes)
        return "我会尊重你的边界，先听你说。"

    monkeypatch.setattr(chat_module, "rewrite_blocked_response", fake_rewrite)
    with SessionLocal.begin() as session:
        preference = companion.get_or_create_preference(session, owner)
        preference.companion_enabled = True
        preference.safety_mode = "standard"
        conversation = Conversation(
            platform="qq",
            external_id=f"private:{owner}:rewrite-{uuid.uuid4()}",
            conversation_type="private",
            title="输出复核测试",
            model_alias="default",
            owner_id=owner,
        )
        session.add(conversation)
        session.flush()
        conversation_id = conversation.id

    async def collect() -> list[dict[str, object]]:
        with SessionLocal() as session:
            conversation = session.get(Conversation, conversation_id)
            assert conversation is not None
            return [
                event
                async for event in chat_service.stream(
                    session, conversation, owner, "我今天很难受", platform="qq", is_group=False
                )
            ]

    try:
        events = asyncio.run(collect())
        final = next(item for item in events if item["event"] == "final")
        assert final["data"]["response_source"] == "rewritten"
        assert final["data"]["text"] == "我会尊重你的边界，先听你说。"
        assert captured == [["dependency_exclusivity"]]
    finally:
        with SessionLocal.begin() as session:
            if conversation_id:
                messages = session.query(Message).filter(Message.conversation_id == conversation_id).all()
                for message in messages:
                    session.query(SafetyEvent).filter(SafetyEvent.message_id == message.id).delete()
                    session.query(EmotionAssessment).filter(
                        EmotionAssessment.user_message_id == message.id
                    ).delete()
                    session.delete(message)
                conversation = session.get(Conversation, conversation_id)
                if conversation is not None:
                    session.delete(conversation)
