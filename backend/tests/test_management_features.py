from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db.models import (
    AppSetting,
    Artifact,
    Confirmation,
    Conversation,
    Job,
    Message,
    ToolRun,
)
from app.db.session import SessionLocal
from app.main import app
from app.services import model_profiles
from app.services.models import model_registry
from fastapi.testclient import TestClient
from sqlalchemy import func, select


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def test_default_model_switch_updates_all_conversations(monkeypatch, tmp_path: Path) -> None:
    alias = _unique("switch")
    monkeypatch.setattr(model_profiles, "PROJECT_ROOT", tmp_path)
    with SessionLocal() as session:
        profile = model_profiles.create_profile(
            session,
            {
                "alias": alias,
                "model": "test-model",
                "base_url": "https://provider.example/v1",
            },
            "unit-test-key",
        )
        monkeypatch.setenv(profile.api_key_env, "unit-test-key")
        web = Conversation(title="默认切换网页", model_alias="default")
        qq = Conversation(
            platform="qq",
            external_id=_unique("private"),
            title="默认切换 QQ",
            model_alias="default",
        )
        session.add_all([web, qq])
        session.commit()
        web_id, qq_id = web.id, qq.id

    try:
        with TestClient(app) as client:
            response = client.put(f"/api/v1/models/{alias}/default")
            assert response.status_code == 200
            assert response.json()["is_default"] is True
            rows = {
                item["id"]: item
                for item in client.get("/api/v1/conversations").json()
                if item["id"] in {web_id, qq_id}
            }
            assert rows[web_id]["model_alias"] == alias
            assert rows[qq_id]["model_alias"] == alias
            assert any(
                item["alias"] == alias and item["is_default"]
                for item in client.get("/api/v1/models").json()
            )
    finally:
        with SessionLocal() as session:
            session.execute(
                Conversation.__table__.update()
                .where(Conversation.model_alias == alias)
                .values(model_alias="default")
            )
            setting = session.get(AppSetting, model_profiles.DEFAULT_MODEL_SETTING_KEY)
            if setting is not None:
                setting.value = "default"
            session.commit()
            model_profiles.delete_profile(session, alias)
        with SessionLocal() as session:
            profiles = model_profiles._profiles_from_setting(
                session.get(AppSetting, model_profiles.MODEL_PROFILES_SETTING_KEY)
            )
        model_registry.replace(profiles, default_alias="default")


def test_conversation_delete_is_web_only_and_detaches_records() -> None:
    web_id = _unique("web")
    qq_id = _unique("qq")
    with SessionLocal() as session:
        web = Conversation(id=web_id, title="待删除网页", model_alias="default")
        qq = Conversation(
            id=qq_id,
            platform="qq",
            external_id=_unique("private"),
            title="不可删除 QQ",
            model_alias="default",
        )
        session.add_all([web, qq])
        session.flush()
        message = Message(conversation_id=web_id, role="user", content="测试消息")
        session.add(message)
        session.add(ToolRun(conversation_id=web_id, tool_name="test", arguments="{}", status="ok"))
        session.add(
            Job(
                conversation_id=web_id,
                type="test",
                status="succeeded",
                payload="{}",
                result="{}",
            )
        )
        session.add(
            Confirmation(
                token=_unique("token"),
                requester_id="local-owner",
                conversation_id=web_id,
                action="test",
                payload="{}",
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5),
            )
        )
        session.add(
            Artifact(
                owner_id="local-owner",
                conversation_id=web_id,
                filename="keep.pdf",
                path=str(Path("keep.pdf")),
                sha256="0" * 64,
                size=0,
            )
        )
        session.commit()

    with TestClient(app) as client:
        denied = client.delete(f"/api/v1/conversations/{qq_id}")
        assert denied.status_code == 403
        deleted = client.delete(f"/api/v1/conversations/{web_id}")
        assert deleted.status_code == 200

    with SessionLocal() as session:
        assert session.get(Conversation, web_id) is None
        assert (
            session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == web_id))
            == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(ToolRun).where(ToolRun.conversation_id == web_id))
            == 0
        )
        assert session.scalar(select(func.count()).select_from(Job).where(Job.conversation_id == web_id)) == 0
        assert (
            session.scalar(
                select(func.count()).select_from(Confirmation).where(Confirmation.conversation_id == web_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Artifact).where(Artifact.conversation_id == web_id)
            )
            == 0
        )
        job = session.scalar(select(Job).where(Job.conversation_id == web_id))
        assert job is None
        assert session.scalar(select(func.count()).select_from(Job).where(Job.type == "test")) >= 1
        session.delete(session.get(Conversation, qq_id))
        session.commit()


def test_bulk_delete_rejects_active_tasks_and_allows_confirmations() -> None:
    terminal_id = _unique("terminal")
    running_id = _unique("running")
    token = _unique("token")
    with TestClient(app) as client:
        with SessionLocal.begin() as session:
            session.add_all(
                [
                    Job(id=terminal_id, type="test", status="succeeded", payload="{}", result="{}"),
                    Job(id=running_id, type="test", status="running", payload="{}"),
                    Confirmation(
                        token=token,
                        requester_id="local-owner",
                        action="test",
                        payload="{}",
                        status="pending",
                        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5),
                    ),
                ]
            )
        blocked = client.post("/api/v1/tasks/bulk-delete", json={"ids": [terminal_id, running_id]})
        assert blocked.status_code == 409
        assert client.post("/api/v1/tasks/bulk-delete", json={"ids": [terminal_id]}).json() == {"deleted": 1}
        assert client.post(
            "/api/v1/confirmations/bulk-delete", json={"tokens": [token]}
        ).json() == {"deleted": 1}

    with SessionLocal.begin() as session:
        running = session.get(Job, running_id)
        if running is not None:
            running.status = "cancelled"
            session.delete(running)
