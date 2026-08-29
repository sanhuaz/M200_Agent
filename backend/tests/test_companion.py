from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from app.db.models import (
    CompanionPreference,
    Conversation,
    EmotionAssessment,
    Job,
    Message,
    RelationshipProfile,
)
from app.db.session import SessionLocal
from app.main import app
from app.services import companion
from app.services import jobs as jobs_service
from app.services.chat import chat_service
from app.services.companion import (
    COMPANION_ANALYSIS_RETRY_JOB_TYPE,
    CompanionAnalysis,
    CompanionAnalysisPayload,
    analyze_message,
    assessment_dict,
    detect_support_mode,
    extract_emotion_anchors,
    extract_relationship,
    get_or_create_preference,
    get_relationship,
    output_safety_ok,
    parse_emotion_labels,
    parse_emotion_labels_strict,
    resolve_analyzer_alias,
    safety_precheck,
)
from app.services.jobs import JobWorker, create_companion_analysis_retry_job
from app.services.models import model_registry
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage


def test_safety_precheck_prioritizes_imminent_and_medium_boundaries() -> None:
    assert safety_precheck("我现在准备自杀").risk_level == "critical"
    assert safety_precheck("我不想活了").risk_level == "high"
    assert safety_precheck("我是不是抑郁症，给我诊断").risk_level == "medium"
    assert safety_precheck("今天有点累").risk_level == "low"


def test_support_override_and_low_confidence_clarify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: (
            {
                "event": "考试结果",
                "primary_emotion": "sadness",
                "candidate_emotions": ["sadness", "anxiety"],
                "intensity": "medium",
                "support_need": "advice",
                "confidence": 0.92,
                "risk_level": "low",
            },
            True,
        ),
    )
    result = analyze_message(
        "我考砸了，只想说说，不用建议",
        "",
        "default",
        forced_mode=detect_support_mode("我考砸了，只想说说，不用建议"),
    )
    assert result.support_need == "advice"
    assert result.next_action == "listen"

    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: ({"primary_emotion": "sadness"}, False),
    )
    fallback = analyze_message("说不清楚", "", "default")
    assert fallback.schema_valid is False
    assert fallback.analysis_status == "failed"
    assert fallback.next_action == "clarify"
    assert fallback.confidence == 0

    forced_fallback = analyze_message("说不清楚，只想说说", "", "default", forced_mode="listen")
    assert forced_fallback.schema_valid is False
    assert forced_fallback.next_action == "clarify"


def test_medium_risk_always_clarifies_and_high_risk_skips_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_invoke(*_args, **_kwargs):
        nonlocal called
        called = True
        return (
            {
                "event": "",
                "primary_emotion": "anxiety",
                "candidate_emotions": ["anxiety"],
                "intensity": "high",
                "support_need": "advice",
                "confidence": 0.99,
                "risk_level": "low",
            },
            True,
        )

    monkeypatch.setattr(companion, "_invoke_json", fake_invoke)
    medium = analyze_message(
        "请给我诊断，我是不是抑郁",
        "",
        "default",
        safety=safety_precheck("请给我诊断，我是不是抑郁"),
    )
    assert medium.risk_level == "medium"
    assert medium.next_action == "clarify"
    assert called is True
    called = False
    critical = analyze_message(
        "我现在准备自杀",
        "",
        "default",
        safety=safety_precheck("我现在准备自杀"),
    )
    assert critical.next_action == "safety_redirect"
    assert called is False


def test_schema_repair_parser_and_output_boundary() -> None:
    parsed = companion._parse_json("```json\n{\"primary_emotion\": \"joy\"}\n```")
    assert parsed == {"primary_emotion": "joy"}
    assert parse_emotion_labels("焦虑、sadness、不是标签") == ["anxiety", "sadness"]
    assert parse_emotion_labels_strict("焦虑、不是标签") == (["anxiety"], ["不是标签"])
    assert output_safety_ok("我会永远只属于你") is False
    assert output_safety_ok("这都是你的错") is False
    assert output_safety_ok("我会认真听你说") is True


def test_shared_emotion_guide_and_candidate_order_contract() -> None:
    assert companion.PROMPT_VERSION == "companion-analysis-v3"
    assert companion.CLASSIFIER_VERSION == "emotion-classifier-v2"
    assert companion.EXPERIMENTAL_PROMPT_VERSION == "companion-analysis-v4"
    assert companion.EXPERIMENTAL_CLASSIFIER_VERSION == "emotion-classifier-v3"
    for label in companion.EMOTION_LABELS:
        assert f"- {label}：" in companion.EMOTION_DECISION_GUIDE
    assert "disappointment" in companion.EMOTION_DECISION_GUIDE
    assert "frustration" in companion.EMOTION_DECISION_GUIDE
    assert "helplessness" in companion.EMOTION_DECISION_GUIDE
    assert "anxiety" in companion.EMOTION_DECISION_GUIDE
    assert "第一项必须是 primary_emotion" in companion.ANALYSIS_SCHEMA_CONTRACT

    valid = {
        "event": "测试",
        "primary_emotion": "sadness",
        "candidate_emotions": ["sadness", "disappointment"],
        "intensity": "medium",
        "support_need": "comfort",
        "confidence": 0.8,
        "risk_level": "low",
    }
    assert CompanionAnalysisPayload.model_validate(valid).candidate_emotions[0] == "sadness"
    with pytest.raises(ValueError, match="第一项"):
        CompanionAnalysisPayload.model_validate(
            {**valid, "candidate_emotions": ["disappointment", "sadness"]}
        )
    with pytest.raises(ValueError, match="neutral"):
        CompanionAnalysisPayload.model_validate(
            {**valid, "primary_emotion": "neutral", "candidate_emotions": ["neutral", "sadness"]}
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我很失望", ["disappointment"]),
        ("我很害怕失败", ["fear"]),
        ("我不知道怎么选", ["confusion"]),
        ("我很失望，也很焦虑", ["disappointment", "anxiety"]),
        ("我不害怕", []),
        ("“我很难过”", []),
        ("如果我害怕失败就好了", []),
        ("朋友说我很焦虑", []),
    ],
)
def test_explicit_emotion_anchors_filter_non_current_claims(text: str, expected: list[str]) -> None:
    labels, rules = extract_emotion_anchors(text)
    assert labels == expected
    assert len(rules) == len(labels)
    assert all("explicit_" in rule for rule in rules)


def test_unique_anchor_promotes_candidate_without_second_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: (
            {
                "event": "结果",
                "primary_emotion": "sadness",
                "candidate_emotions": ["sadness", "disappointment"],
                "intensity": "medium",
                "support_need": "comfort",
                "confidence": 0.84,
                "risk_level": "low",
            },
            True,
        ),
    )
    result = analyze_message("我很失望", "", "default", use_experimental_prompt=True)
    assert result.primary_emotion == "disappointment"
    assert result.candidate_emotions == ["disappointment", "sadness"]
    assert result.anchor_labels == ["disappointment"]
    assert result.anchor_adjusted is True
    assert result.anchor_conflict is False
    assert result.prompt_version == companion.EXPERIMENTAL_PROMPT_VERSION
    assert result.classifier_version == companion.EXPERIMENTAL_CLASSIFIER_VERSION


def test_unique_anchor_missing_candidate_forces_clarify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: (
            {
                "event": "结果",
                "primary_emotion": "sadness",
                "candidate_emotions": ["sadness"],
                "intensity": "medium",
                "support_need": "comfort",
                "confidence": 0.98,
                "risk_level": "low",
            },
            True,
        ),
    )
    result = analyze_message("我很失望", "", "default", use_experimental_prompt=True)
    assert result.anchor_conflict is True
    assert result.anchor_adjusted is False
    assert result.confidence == 0.64
    assert result.next_action == "clarify"


def test_schema_validation_failure_uses_complete_repair_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        AIMessage(content='{"primary_emotion":"disappointment"}'),
        AIMessage(
            content=json.dumps(
                {
                    "event": "求职受挫",
                    "primary_emotion": "disappointment",
                    "candidate_emotions": ["disappointment", "anxiety"],
                    "intensity": "high",
                    "support_need": "comfort",
                    "confidence": 0.86,
                    "risk_level": "low",
                },
                ensure_ascii=False,
            )
        ),
    ]
    prompts: list[list[object]] = []

    class _RepairModel:
        def bind(self, **_kwargs):
            return self

        def invoke(self, messages):
            prompts.append(messages)
            return responses.pop(0)

    model = _RepairModel()
    monkeypatch.setattr(
        companion.model_registry,
        "profile",
        lambda _alias: SimpleNamespace(base_url="https://api.example.com/v1"),
    )
    monkeypatch.setattr(companion.model_registry, "chat_model", lambda _alias: model)

    result = analyze_message("两百次投递后只剩一次面试，还失败了", "", "default")

    assert result.schema_valid is True
    assert result.primary_emotion == "disappointment"
    assert result.candidate_emotions == ["disappointment", "anxiety"]
    assert len(prompts) == 2
    repair_system = str(prompts[1][0].content)
    assert "candidate_emotions" in repair_system
    assert "confidence（0 到 1 之间的数字）" in repair_system
    assert "不得诊断" in repair_system


@pytest.mark.parametrize(
    "invalid_output, expected_error",
    [
        ("{\"event\": \"截断", "输出不是合法 JSON 对象"),
        (
            json.dumps(
                {
                    "event": "测试",
                    "primary_emotion": "not-an-emotion",
                    "candidate_emotions": ["not-an-emotion"],
                    "intensity": "medium",
                    "support_need": "comfort",
                    "confidence": 0.8,
                    "risk_level": "low",
                }
            ),
            "primary_emotion",
        ),
        (
            json.dumps(
                {
                    "event": "测试",
                    "primary_emotion": "sadness",
                    "candidate_emotions": ["sadness", "sadness"],
                    "intensity": "medium",
                    "support_need": "comfort",
                    "confidence": 0.8,
                    "risk_level": "low",
                }
            ),
            "candidate_emotions",
        ),
        (
            json.dumps(
                {
                    "event": "测试",
                    "primary_emotion": "sadness",
                    "candidate_emotions": ["sadness"],
                    "intensity": "medium",
                    "support_need": "comfort",
                    "confidence": 1.1,
                    "risk_level": "low",
                }
            ),
            "confidence",
        ),
    ],
)
def test_syntax_and_schema_errors_all_trigger_sync_repair(
    monkeypatch: pytest.MonkeyPatch, invalid_output: str, expected_error: str
) -> None:
    valid_output = json.dumps(
        {
            "event": "测试",
            "primary_emotion": "sadness",
            "candidate_emotions": ["sadness"],
            "intensity": "medium",
            "support_need": "comfort",
            "confidence": 0.8,
            "risk_level": "low",
        }
    )
    responses = [AIMessage(content=invalid_output), AIMessage(content=valid_output)]
    prompts: list[list[object]] = []

    class _RepairModel:
        def bind(self, **_kwargs):
            return self

        def invoke(self, messages):
            prompts.append(messages)
            return responses.pop(0)

    monkeypatch.setattr(
        companion.model_registry,
        "profile",
        lambda _alias: SimpleNamespace(base_url="https://api.example.com/v1"),
    )
    monkeypatch.setattr(companion.model_registry, "chat_model", lambda _alias: _RepairModel())

    result = analyze_message("测试结构化恢复", "", "default")

    assert result.schema_valid is True
    assert len(prompts) == 2
    assert expected_error in str(prompts[1][1].content)


def _failed_assessment_fixture(scope_id: str) -> tuple[str, str, str]:
    with SessionLocal.begin() as session:
        conversation = Conversation(
            platform="qq",
            external_id=f"private:{scope_id}",
            model_alias="default",
        )
        session.add(conversation)
        session.flush()
        message = Message(
            conversation_id=conversation.id,
            sender_id=scope_id,
            role="user",
            content="测试结构化分析失败后的恢复",
        )
        session.add(message)
        session.flush()
        assessment = EmotionAssessment(
            user_message_id=message.id,
            scope_id=scope_id,
            model_alias="default",
            schema_valid=False,
        )
        session.add(assessment)
        session.flush()
        return conversation.id, message.id, assessment.id


def _cleanup_assessment_fixture(conversation_id: str, message_id: str, assessment_id: str) -> None:
    with SessionLocal.begin() as session:
        job = session.get(Job, assessment_id)
        if job is not None:
            session.delete(job)
        assessment = session.get(EmotionAssessment, assessment_id)
        if assessment is not None:
            session.delete(assessment)
        message = session.get(Message, message_id)
        if message is not None:
            session.delete(message)
        conversation = session.get(Conversation, conversation_id)
        if conversation is not None:
            session.delete(conversation)


def test_companion_retry_job_is_idempotent_and_updates_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id, message_id, assessment_id = _failed_assessment_fixture("10001")
    try:
        first = create_companion_analysis_retry_job(
            assessment_id=assessment_id,
            user_message_id=message_id,
            requester_id="10001",
            conversation_id=conversation_id,
            forced_mode=None,
        )
        second = create_companion_analysis_retry_job(
            assessment_id=assessment_id,
            user_message_id=message_id,
            requester_id="10001",
            conversation_id=conversation_id,
            forced_mode=None,
        )
        assert first is not None and second is not None
        assert first.id == second.id == assessment_id
        assert first.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE

        monkeypatch.setattr(
            jobs_service,
            "analyze_message",
            lambda *_args, **_kwargs: CompanionAnalysis(
                primary_emotion="disappointment",
                candidate_emotions=["disappointment", "anxiety"],
                intensity="high",
                support_need="comfort",
                confidence=0.86,
                risk_level="low",
                next_action="comfort",
                model_alias="default",
                schema_valid=True,
            ),
        )
        asyncio.run(JobWorker()._execute(assessment_id))

        with SessionLocal() as session:
            assessment = session.get(EmotionAssessment, assessment_id)
            job = session.get(Job, assessment_id)
            assert assessment is not None and job is not None
            assert assessment.schema_valid is True
            assert assessment.primary_emotion == "disappointment"
            assert assessment.confidence == 0.86
            assert job.status == "succeeded"
    finally:
        _cleanup_assessment_fixture(conversation_id, message_id, assessment_id)


def test_companion_retry_job_stops_after_two_background_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id, message_id, assessment_id = _failed_assessment_fixture("10001")
    calls = 0

    def failed_analysis(*_args, **_kwargs) -> CompanionAnalysis:
        nonlocal calls
        calls += 1
        return CompanionAnalysis(schema_valid=False, analysis_status="failed")

    monkeypatch.setattr(jobs_service, "analyze_message", failed_analysis)
    try:
        create_companion_analysis_retry_job(
            assessment_id=assessment_id,
            user_message_id=message_id,
            requester_id="10001",
            conversation_id=conversation_id,
            forced_mode=None,
        )
        worker = JobWorker()
        asyncio.run(worker._execute(assessment_id))
        asyncio.run(worker._execute(assessment_id))
        with SessionLocal() as session:
            assessment = session.get(EmotionAssessment, assessment_id)
            job = session.get(Job, assessment_id)
            assert assessment is not None and job is not None
            assert calls == 2
            assert assessment.schema_valid is False
            assert job.status == "failed"
            assert job.error == "companion_retry_schema_invalid"
    finally:
        _cleanup_assessment_fixture(conversation_id, message_id, assessment_id)


def test_failed_assessment_is_not_rendered_as_neutral() -> None:
    conversation_id, message_id, assessment_id = _failed_assessment_fixture("10001")
    try:
        create_companion_analysis_retry_job(
            assessment_id=assessment_id,
            user_message_id=message_id,
            requester_id="10001",
            conversation_id=conversation_id,
            forced_mode=None,
        )
        with SessionLocal() as session:
            assessment = session.get(EmotionAssessment, assessment_id)
            assert assessment is not None
            pending = assessment_dict(assessment, session)
            assert pending["analysis_status"] == "retrying"
            assert pending["candidate_emotions"] == []
            assert pending["primary_emotion"] is None
            assert pending["confidence"] is None
            job = session.get(Job, assessment_id)
            assert job is not None
            job.status = "failed"
            session.commit()
        with SessionLocal() as session:
            assessment = session.get(EmotionAssessment, assessment_id)
            assert assessment is not None
            failed = assessment_dict(assessment, session)
            assert failed["analysis_status"] == "failed"
            assert failed["primary_emotion"] is None
    finally:
        _cleanup_assessment_fixture(conversation_id, message_id, assessment_id)


def test_relationship_and_preference_scopes_are_isolated() -> None:
    first = "900001"
    second = "900002"
    with SessionLocal.begin() as session:
        first_preference = get_or_create_preference(session, first)
        get_or_create_preference(session, second)
        first_preference.memory_enabled = True
        get_relationship(session, first, None, create=True)
        get_relationship(session, first, "persona-a", create=True)
        get_relationship(session, second, None, create=True)
    with SessionLocal() as session:
        assert session.query(CompanionPreference).filter_by(scope_id=first).one().memory_enabled is True
        assert session.query(CompanionPreference).filter_by(scope_id=second).one().memory_enabled is False
        assert (
            session.query(RelationshipProfile)
            .filter_by(scope_id=first)
            .count()
            == 2
        )
        assert session.query(RelationshipProfile).filter_by(scope_id=second).count() == 1


def test_relationship_extraction_filters_sensitive_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: (
            {
                "nickname": "小林",
                "preferences": ["喜欢散步", "API key=secret"],
                "boundaries": ["不想被催着给建议"],
                "shared_events": ["一起看过展览"],
            },
            True,
        ),
    )
    scope = "900003"
    with SessionLocal() as session:
        item = extract_relationship(session, scope, None, "我喜欢散步，别急着给建议", "", "default")
        assert item is not None
        assert item.nickname == "小林"
        assert "喜欢散步" in json.loads(item.boundaries)["preferences"]
        assert "API key=secret" not in json.dumps(json.loads(item.boundaries), ensure_ascii=False)
        assert "一起看过展览" in item.shared_summary


def test_analyzer_alias_falls_back_to_session_model(monkeypatch: pytest.MonkeyPatch) -> None:
    available = {"session"}

    def fake_chat_model(alias: str):
        if alias not in available:
            raise RuntimeError("not configured")
        return object()

    monkeypatch.setattr(model_registry, "chat_model", fake_chat_model)
    assert resolve_analyzer_alias("missing", "session") == "session"
    assert resolve_analyzer_alias("session", "missing") == "session"


def test_companion_management_api_supports_versioning_export_and_idempotent_delete() -> None:
    scope = "10001"
    with TestClient(app) as client:
        initial = client.get(f"/api/v1/companion/preferences?qq_user_id={scope}")
        assert initial.status_code == 200
        assert initial.json()["companion_enabled"] is True
        updated = client.put(
            "/api/v1/companion/preferences",
            json={
                "qq_user_id": scope,
                "support_mode": "listen",
                "memory_enabled": True,
                "analyzer_model_alias": None,
                "boundaries": {"items": ["不想被催促"]},
            },
        )
        assert updated.status_code == 200
        assert updated.json()["support_mode"] == "listen"
        assert updated.json()["memory_enabled"] is True
        relation = client.put(
            "/api/v1/companion/relationships/default",
            json={
                "qq_user_id": scope,
                "nickname": "小林",
                "shared_summary": "一起看过展览",
                "version": 0,
            },
        )
        assert relation.status_code == 200
        assert relation.json()["version"] == 1
        conflict = client.put(
            "/api/v1/companion/relationships/default",
            json={"qq_user_id": scope, "nickname": "旧资料", "version": 0},
        )
        assert conflict.status_code == 409
        exported = client.get(f"/api/v1/companion/privacy/export?qq_user_id={scope}")
        assert exported.status_code == 200
        assert exported.json()["relationships"][0]["nickname"] == "小林"
        assert "SYSTEM_PROMPT" not in json.dumps(exported.json(), ensure_ascii=False)
        deleted = client.request(
            "DELETE",
            "/api/v1/companion/privacy/data",
            json={"qq_user_id": scope, "confirm_text": "删除陪伴数据"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["counts"]["relationships"] == 1
        repeated = client.request(
            "DELETE",
            "/api/v1/companion/privacy/data",
            json={"qq_user_id": scope, "confirm_text": "删除陪伴数据"},
        )
        assert repeated.status_code == 200
        assert all(value == 0 for value in repeated.json()["counts"].values())


class _CompanionGraphModel:
    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content="我会先听你说，不急着给建议。")


def test_owner_private_stream_emits_analysis_and_safety_bypasses_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_registry, "chat_model", lambda _alias: _CompanionGraphModel())
    monkeypatch.setattr(
        companion,
        "_invoke_json",
        lambda *_args, **_kwargs: (
            {
                "event": "",
                "primary_emotion": "anxiety",
                "candidate_emotions": ["anxiety", "exhaustion"],
                "intensity": "medium",
                "support_need": "advice",
                "confidence": 0.9,
                "risk_level": "low",
            },
            True,
        ),
    )

    async def collect(message: str, conversation: Conversation) -> list[dict[str, object]]:
        with SessionLocal() as session:
            stored = session.get(Conversation, conversation.id)
            assert stored is not None
            return [
                item
                async for item in chat_service.stream(
                    session,
                    stored,
                    "10001",
                    message,
                    platform="qq",
                    is_group=False,
                )
            ]

    with TestClient(app):
        with SessionLocal.begin() as session:
            conversation = Conversation(
                platform="qq",
                external_id="private:10001",
                conversation_type="private",
                title="陪伴测试",
                model_alias="default",
                owner_id="10001",
            )
            session.add(conversation)
            session.flush()
            conversation_id = conversation.id
        with SessionLocal() as session:
            conversation = session.get(Conversation, conversation_id)
            assert conversation is not None
            events = asyncio.run(collect("我今天很焦虑，只想说说", conversation))
        assert any(item["event"] == "companion_analysis" for item in events)
        final = next(item for item in events if item["event"] == "final")
        assert "先听你说" in final["data"]["text"]

        class _FailingGraphModel:
            def bind_tools(self, _tools):
                raise AssertionError("高风险消息不应构建普通 Agent 图")

        monkeypatch.setattr(model_registry, "chat_model", lambda _alias: _FailingGraphModel())
        with SessionLocal() as session:
            conversation = session.get(Conversation, conversation_id)
            assert conversation is not None
            events = asyncio.run(collect("我现在准备自杀", conversation))
        final = next(item for item in events if item["event"] == "final")
        assert "安全" in final["data"]["text"]


def test_owner_private_stream_queues_failed_analysis_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_registry, "chat_model", lambda _alias: _CompanionGraphModel())
    monkeypatch.setattr(companion, "_invoke_json", lambda *_args, **_kwargs: (None, False))
    external_id = f"private:retry-{uuid.uuid4()}"
    with SessionLocal.begin() as session:
        conversation = Conversation(
            platform="qq",
            external_id=external_id,
            conversation_type="private",
            title="陪伴恢复测试",
            model_alias="default",
            owner_id="10001",
        )
        session.add(conversation)
        session.flush()
        conversation_id = conversation.id

    assessment_id: str | None = None
    try:
        async def collect() -> list[dict[str, object]]:
            with SessionLocal() as session:
                conversation = session.get(Conversation, conversation_id)
                assert conversation is not None
                return [
                    item
                    async for item in chat_service.stream(
                        session,
                        conversation,
                        "10001",
                        "这条消息用于验证分析失败恢复",
                        platform="qq",
                        is_group=False,
                    )
                ]

        events = asyncio.run(collect())
        analysis_event = next(item for item in events if item["event"] == "companion_analysis")
        assert analysis_event["data"]["schema_valid"] is False
        assert analysis_event["data"]["analysis_status"] == "retrying"
        assert analysis_event["data"]["candidate_emotions"] == []
        with SessionLocal() as session:
            assessment = session.query(EmotionAssessment).filter_by(scope_id="10001").order_by(
                EmotionAssessment.created_at.desc()
            ).first()
            assert assessment is not None
            assessment_id = assessment.id
            job = session.get(Job, assessment.id)
            assert job is not None
            assert job.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE
            assert job.status == "queued"
    finally:
        with SessionLocal.begin() as session:
            if assessment_id is not None:
                job = session.get(Job, assessment_id)
                if job is not None:
                    session.delete(job)
                assessment = session.get(EmotionAssessment, assessment_id)
                if assessment is not None:
                    session.delete(assessment)
            conversation_row = session.get(Conversation, conversation_id)
            if conversation_row is not None:
                session.delete(conversation_row)
