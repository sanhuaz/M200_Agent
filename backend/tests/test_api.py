from __future__ import annotations

import sys

from app.main import app
from app.services.models import model_registry
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage


def test_health_reports_exact_working_interpreter() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["python_executable"] == sys.executable
        assert payload["database"] == "connected"
        assert payload["worker"] == "running"


def test_conversation_lifecycle_and_model_switch() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/conversations",
            json={"title": "测试会话", "model_alias": "default"},
        )
        assert created.status_code == 200
        conversation_id = created.json()["id"]
        assert client.get("/api/v1/conversations").json()[0]["id"] == conversation_id
        switched = client.put(
            f"/api/v1/conversations/{conversation_id}/model",
            json={"model_alias": "default"},
        )
        assert switched.status_code == 200
        assert switched.json()["model_alias"] == "default"


class FakeToolModel:
    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content="离线模型回答")


def test_chat_sse_runs_langgraph_with_async_sqlite_checkpoint(monkeypatch) -> None:
    monkeypatch.setattr(model_registry, "chat_model", lambda _alias: FakeToolModel())
    with TestClient(app) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={"title": "Graph 测试", "model_alias": "default"},
        ).json()
        response = client.post(
            "/api/v1/chat/stream",
            json={"conversation_id": conversation["id"], "message": "你好"},
        )
        assert response.status_code == 200
        assert "event: final" in response.text
        assert "离线模型回答" in response.text
