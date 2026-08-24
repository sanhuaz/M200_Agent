from __future__ import annotations

import os

from app.core.config import ModelProfile
from app.db.models import Conversation
from app.db.session import SessionLocal
from app.main import app
from app.services import model_profiles
from app.services import models as model_service
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage


def _cleanup_alias(alias: str) -> None:
    with SessionLocal() as session:
        for item in session.query(Conversation).where(Conversation.model_alias == alias):
            item.model_alias = "default"
        session.commit()
    with SessionLocal() as session:
        try:
            model_profiles.delete_profile(session, alias)
        except (KeyError, PermissionError, RuntimeError):
            session.rollback()


def test_model_profile_crud_and_reference_guard() -> None:
    alias = "api-test-model"
    _cleanup_alias(alias)
    payload = {
        "alias": alias,
        "model": "test-chat-model",
        "base_url": "https://provider.example/v1",
        "reasoning_effort": "medium",
        "streaming": False,
        "temperature": 0.4,
        "context_window": 1000,
        "input_soft_limit": 800,
        "max_output_tokens": 200,
        "timeout_seconds": 5,
    }
    with TestClient(app) as client:
        created = client.post("/api/v1/models", json=payload)
        assert created.status_code == 200
        result = created.json()
        assert result["alias"] == alias
        assert result["streaming"] is False
        assert result["reasoning_effort"] == "medium"
        assert "api_key" not in result
        assert "api_key_env" not in result

        duplicate = client.post("/api/v1/models", json=payload)
        assert duplicate.status_code == 400

        conversation = client.post(
            "/api/v1/conversations", json={"title": "模型引用测试", "model_alias": alias}
        ).json()
        blocked = client.delete(f"/api/v1/models/{alias}")
        assert blocked.status_code == 409

        with SessionLocal() as session:
            item = session.get(Conversation, conversation["id"])
            assert item is not None
            item.model_alias = "default"
            session.commit()

        updated = client.put(
            f"/api/v1/models/{alias}",
            json={"temperature": None, "streaming": True},
        )
        assert updated.status_code == 200
        assert updated.json()["temperature"] is None
        assert updated.json()["streaming"] is True

        removed = client.delete(f"/api/v1/models/{alias}")
        assert removed.status_code == 200
        assert client.delete("/api/v1/models/default").status_code == 400


def test_model_api_key_is_written_only_to_env_and_can_be_cleared(monkeypatch, tmp_path) -> None:
    alias = "secret-test-model"
    _cleanup_alias(alias)
    monkeypatch.setattr(model_profiles, "PROJECT_ROOT", tmp_path)
    payload = {
        "alias": alias,
        "model": "secret-chat-model",
        "base_url": "https://provider.example/v1",
        "api_key": "secret-value-for-test",
    }
    with TestClient(app) as client:
        created = client.post("/api/v1/models", json=payload)
        assert created.status_code == 200
        result = created.json()
        assert result["has_api_key"] is True
        assert "secret-value-for-test" not in created.text
        env_text = next(tmp_path.glob(".env"), None)
        assert env_text is not None
        assert "secret-value-for-test" in env_text.read_text(encoding="utf-8")

        cleared = client.put(f"/api/v1/models/{alias}", json={"clear_api_key": True})
        assert cleared.status_code == 200
        assert cleared.json()["has_api_key"] is False
        assert '=""' in env_text.read_text(encoding="utf-8")

    with SessionLocal() as session:
        profile = model_profiles._profiles_from_setting(
            session.get(model_profiles.AppSetting, model_profiles.MODEL_PROFILES_SETTING_KEY)
        )
        env_names = [item.api_key_env for item in profile if item.alias == alias]
    _cleanup_alias(alias)
    for env_name in env_names:
        os.environ.pop(env_name, None)


def test_model_connection_test_does_not_persist(monkeypatch) -> None:
    calls: list[object] = []

    class FakeModel:
        def invoke(self, messages):
            calls.append(messages)
            return object()

    monkeypatch.setattr(model_profiles, "build_chat_model", lambda *_args, **_kwargs: FakeModel())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/models/test-connection",
            json={
                "alias": "temporary-probe",
                "model": "probe-model",
                "base_url": "https://provider.example/v1",
                "api_key": "probe-secret",
            },
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert calls
    with TestClient(app) as client:
        aliases = [item["alias"] for item in client.get("/api/v1/models").json()]
    assert "temporary-probe" not in aliases


def test_chat_model_receives_runtime_parameters(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(model_service, "ChatOpenAI", FakeChatOpenAI)
    profile = ModelProfile(
        alias="parameter-test",
        model="test-model",
        base_url="https://provider.example/v1",
        api_key_env="PARAMETER_TEST_KEY",
        reasoning_effort="high",
        streaming=False,
        temperature=0.7,
        max_output_tokens=321,
        timeout_seconds=9,
    )
    model_service.build_chat_model(profile, "secret", max_tokens=8)
    assert captured["model"] == "test-model"
    assert captured["base_url"] == "https://provider.example/v1"
    assert captured["reasoning_effort"] == "high"
    assert captured["streaming"] is False
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 8
    assert captured["timeout"] == 9


def test_non_streaming_profile_buffers_chat_text(monkeypatch) -> None:
    class OfflineModel:
        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            return AIMessage(content="一次性回答")

    profile = model_service.model_registry.profile("default").model_copy(update={"streaming": False})
    monkeypatch.setattr(model_service.model_registry, "profile", lambda _alias: profile)
    monkeypatch.setattr(model_service.model_registry, "chat_model", lambda _alias: OfflineModel())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"message": "测试非流式", "sender_id": "non-stream-test-user"},
        )
    assert response.status_code == 200
    assert response.text.count("event: token") == 1
    assert "一次性回答" in response.text
