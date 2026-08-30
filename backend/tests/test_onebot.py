import asyncio
import json
import uuid
from pathlib import Path

from app.api.onebot import OneBotManager
from app.core.config import get_settings
from app.db.models import CompanionPreference, EmotionAssessment, Job, Memory
from app.db.session import SessionLocal
from app.services.companion import COMPANION_ANALYSIS_RETRY_JOB_TYPE
from app.services.memories import memory_collection_name


def test_help_returns_grouped_multiline_commands() -> None:
    response = asyncio.run(OneBotManager()._command("/help", "10001", "private:10001"))

    assert response is not None
    assert response.startswith("可用命令：\n\n【会话与模型】\n")
    assert "\n\n【知识库与记忆】\n" in response
    assert "\n\n【人格】\n" in response
    assert "\n\n【Tools 与 Skills】\n" in response
    assert "\n\n【漫画】\n" in response
    assert response.endswith("/cancel <token>")


def test_successful_qq_delivery_is_recorded(monkeypatch) -> None:
    with SessionLocal.begin() as session:
        job = Job(
            type="manga_download",
            status="succeeded",
            requester_id="10001",
            payload=json.dumps({"album_id": "456"}),
        )
        session.add(job)
        session.flush()
        job_id = job.id
    job_dir = get_settings().download_path / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = job_dir / "JM456.pdf"
    artifact_path.write_bytes(b"%PDF-test")
    with SessionLocal.begin() as session:
        stored = session.get(Job, job_id)
        assert stored is not None
        stored.result = json.dumps(
            {"album_id": "456", "path": str(artifact_path.resolve()), "size": artifact_path.stat().st_size}
        )
    with SessionLocal() as session:
        detached = session.get(Job, job_id)
        assert detached is not None
        session.expunge(detached)

    sent_files: list[Path] = []
    sent_texts: list[str] = []

    async def fake_send_file(_user_id: str, path: Path) -> None:
        sent_files.append(path)

    async def fake_send_text(_user_id: str, text: str, _group_id: str | None = None) -> None:
        sent_texts.append(text)

    manager = OneBotManager()
    monkeypatch.setattr(manager, "send_private_file", fake_send_file)
    monkeypatch.setattr(manager, "send_text", fake_send_text)

    asyncio.run(manager.notify_job(detached))

    assert sent_files == [artifact_path]
    assert any(f"/jm delete {job_id}" in text for text in sent_texts)
    with SessionLocal() as session:
        stored = session.get(Job, job_id)
        assert stored is not None
        result = json.loads(stored.result or "{}")
        assert result["delivery_status"] == "sent"
        assert result["delivery_error"] is None


def test_companion_commands_enforce_owner_private_scope_and_update_preference() -> None:
    manager = OneBotManager()
    assert asyncio.run(manager._command("/support invalid", "10001", "private:10001")) == (
        "用法：/support auto|listen|reflect|advice"
    )
    assert "情绪标签无效" in asyncio.run(
        manager._command("/emotion correct 焦虑、不是标签", "10001", "private:10001")
    )
    assert asyncio.run(manager._command("/support listen", "10001", "private:10001"))
    assert asyncio.run(manager._command("/companion memory on", "10001", "private:10001"))
    assert asyncio.run(manager._command("/companion pause", "10001", "private:10001"))
    with SessionLocal() as session:
        preference = session.query(CompanionPreference).filter_by(scope_id="10001").one()
        assert preference.companion_enabled is False
        assert preference.memory_enabled is True
        preference.companion_enabled = True
        preference.memory_enabled = False
        preference.support_mode = "auto"
        session.commit()
    assert "仅支持 QQ 私聊" in asyncio.run(
        manager._command("/companion resume", "10001", "group:20001")
    )
    assert "只有 Owner" in asyncio.run(
        manager._command("/support listen", "20002", "private:20002")
    )


def test_qq_memory_archive_uses_the_same_vector_cleanup_as_web(monkeypatch) -> None:
    memory_id = str(uuid.uuid4())
    with SessionLocal.begin() as session:
        session.add(
            Memory(
                id=memory_id,
                scope_type="user",
                user_id="10001",
                fact_key=f"qq-memory-{memory_id}",
                content="需要归档的 QQ 记忆",
            )
        )
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "app.services.memories.vector_store.delete_ids",
        lambda collection, ids: calls.append((collection, ids)),
    )
    try:
        response = asyncio.run(
            OneBotManager()._command(
                f"/memory delete {memory_id}",
                "10001",
                "private:10001",
            )
        )
        assert response == "已删除指定记忆。"
        expected = memory_collection_name(
            "user",
            "10001",
            get_settings().default_embedding_profile,
        )
        assert calls == [(expected, [memory_id])]
        with SessionLocal() as session:
            item = session.get(Memory, memory_id)
            assert item is not None and item.status == "archived"
    finally:
        with SessionLocal.begin() as session:
            item = session.get(Memory, memory_id)
            if item is not None:
                session.delete(item)


def test_emotion_command_hides_failed_placeholder_and_uses_correction() -> None:
    message_id = str(uuid.uuid4())
    assessment_id = str(uuid.uuid4())
    with SessionLocal.begin() as session:
        assessment = EmotionAssessment(
            id=assessment_id,
            user_message_id=message_id,
            scope_id="10001",
            schema_valid=False,
        )
        session.add(assessment)
        session.add(
            Job(
                id=assessment_id,
                type=COMPANION_ANALYSIS_RETRY_JOB_TYPE,
                status="queued",
                requester_id="10001",
                payload=json.dumps(
                    {"assessment_id": assessment_id, "user_message_id": message_id}
                ),
            )
        )

    manager = OneBotManager()
    try:
        retrying = asyncio.run(manager._command("/emotion", "10001", "private:10001"))
        assert retrying == "最近一次情绪分析正在重试，暂时没有可靠结果。"
        with SessionLocal.begin() as session:
            job = session.get(Job, assessment_id)
            assert job is not None
            job.status = "failed"
        failed = asyncio.run(manager._command("/emotion", "10001", "private:10001"))
        assert failed is not None
        assert "分析失败" in failed
        assert "平淡" not in failed
        with SessionLocal.begin() as session:
            assessment = session.get(EmotionAssessment, assessment_id)
            assert assessment is not None
            assessment.correction = json.dumps(
                {"emotions": ["disappointment"], "support_need": "comfort"},
                ensure_ascii=False,
            )
        corrected = asyncio.run(manager._command("/emotion", "10001", "private:10001"))
        assert corrected is not None
        assert "失望" in corrected
        assert "已采用你的纠正" in corrected
    finally:
        with SessionLocal.begin() as session:
            job = session.get(Job, assessment_id)
            if job is not None:
                session.delete(job)
            assessment = session.get(EmotionAssessment, assessment_id)
            if assessment is not None:
                session.delete(assessment)
