import asyncio
import json
from pathlib import Path

from app.api.onebot import OneBotManager
from app.core.config import get_settings
from app.db.models import Job
from app.db.session import SessionLocal


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
