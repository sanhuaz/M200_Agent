from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from app.api import routes
from app.db.models import Document, KnowledgeBase
from app.db.session import SessionLocal
from app.main import app
from app.services.manga import manga_service
from app.services.models import model_registry
from app.workflows import agent as agent_workflow
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


class FakeMangaToolModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_manga",
                        "args": {"query": "测试漫画"},
                        "id": "manga-call",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="候选已返回")


class FakeFileToolModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "create_files",
                        "args": {"files": [{"filename": "main.py", "content": "print(1)"}]},
                        "id": "file-call",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="文件已经创建")


class FakeMangaDownloadModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_manga_download",
                        "args": {"album_id": "123"},
                        "id": "manga-download-call",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="任务已创建，下载成功后系统会自动发送文件。")


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


def test_manga_array_tool_result_is_fed_back_without_attribute_error(monkeypatch) -> None:
    fake_model = FakeMangaToolModel()
    monkeypatch.setattr(model_registry, "chat_model", lambda _alias: fake_model)
    monkeypatch.setattr(
        manga_service,
        "search",
        lambda _query: [{"album_id": "123", "title": "测试漫画", "tags": []}],
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"message": "搜索测试漫画", "sender_id": "manga-test-user"},
        )
        assert response.status_code == 200
        assert "候选已返回" in response.text
        assert "AttributeError" not in response.text


def test_create_files_tool_emits_artifact_event(monkeypatch) -> None:
    monkeypatch.setattr(model_registry, "chat_model", lambda _alias: FakeFileToolModel())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"message": "创建一个 main.py", "sender_id": "file-test-user"},
        )
        assert response.status_code == 200
        assert "event: artifact_created" in response.text
        assert "文件已经创建" in response.text


def test_owner_manga_download_starts_without_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(model_registry, "chat_model", lambda _alias: FakeMangaDownloadModel())
    monkeypatch.setattr(
        agent_workflow,
        "create_manga_download_job",
        lambda *_args: SimpleNamespace(
            id="direct-manga-task", type="manga_download", status="queued"
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={"message": "下载 123", "sender_id": "local-owner"},
        )
        assert response.status_code == 200
        assert "event: task_created" in response.text
        assert "event: pending_confirmation" not in response.text
        assert "系统会自动发送文件" in response.text


def test_manga_download_api_returns_task_without_token(monkeypatch) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    fake_job = SimpleNamespace(
        id="direct-api-task",
        type="manga_download",
        status="queued",
        requester_id="local-owner",
        conversation_id=None,
        payload='{"album_id":"123"}',
        result=None,
        error=None,
        retry_count=0,
        cancel_requested=False,
        created_at=now,
        updated_at=now,
    )
    monkeypatch.setattr(routes, "create_manga_download_job", lambda *_args: fake_job)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/manga/download",
            json={"album_id": "123", "requester_id": "local-owner"},
        )

    assert response.status_code == 200
    assert response.json()["task"]["id"] == "direct-api-task"
    assert "token" not in response.json()


def test_failed_document_can_be_queued_for_reindex(monkeypatch, tmp_path) -> None:
    document_path = tmp_path / "retry.md"
    document_path.write_text("需要重新索引的内容", encoding="utf-8")
    with SessionLocal.begin() as session:
        knowledge_base = KnowledgeBase(name="reindex-api-test")
        session.add(knowledge_base)
        session.flush()
        document = Document(
            knowledge_base_id=knowledge_base.id,
            filename=document_path.name,
            path=str(document_path),
            sha256="3" * 64,
            status="failed",
            error="provider rejected batch",
        )
        session.add(document)
        session.flush()
        document_id = document.id

    monkeypatch.setattr(
        routes,
        "create_job",
        lambda job_type, payload: SimpleNamespace(id="reindex-task", status="queued"),
    )
    with TestClient(app) as client:
        response = client.post(f"/api/v1/documents/{document_id}/reindex")

    assert response.status_code == 200
    assert response.json() == {
        "document_id": document_id,
        "status": "queued",
        "task_id": "reindex-task",
    }
    with SessionLocal() as session:
        refreshed = session.get(Document, document_id)
        assert refreshed is not None
        assert refreshed.status == "queued"
        assert refreshed.error is None
