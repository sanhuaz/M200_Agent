import json
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.db.models import Job
from app.db.session import SessionLocal
from app.services.jobs import delete_manga_artifact
from app.services.manga import MangaService
from PIL import Image
from pypdf import PdfReader


def create_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (20, 30), color)
    try:
        image.save(path)
    finally:
        image.close()


def test_export_downloaded_images_creates_multi_page_pdf(tmp_path: Path) -> None:
    create_image(tmp_path / "chapter-1" / "00001.webp", (255, 0, 0))
    create_image(tmp_path / "chapter-2" / "00001.webp", (0, 255, 0))

    pdf_path = MangaService.export_downloaded_images(tmp_path, "123")

    assert pdf_path == tmp_path / "JM123.pdf"
    assert pdf_path.stat().st_size > 0
    assert len(PdfReader(pdf_path).pages) == 2


def test_export_downloaded_images_requires_images(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="没有找到图片或 PDF"):
        MangaService.export_downloaded_images(tmp_path, "123")


def create_completed_job(delivery_status: str, requester_id: str = "10001") -> tuple[str, Path]:
    with SessionLocal.begin() as session:
        job = Job(
            type="manga_download",
            status="succeeded",
            requester_id=requester_id,
            payload=json.dumps({"album_id": "123"}),
        )
        session.add(job)
        session.flush()
        job_id = job.id
    job_dir = get_settings().download_path / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = job_dir / "JM123.pdf"
    artifact_path.write_bytes(b"%PDF-test")
    with SessionLocal.begin() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.result = json.dumps(
            {
                "album_id": "123",
                "path": str(artifact_path.resolve()),
                "size": artifact_path.stat().st_size,
                "delivery_status": delivery_status,
            }
        )
    return job_id, job_dir


def test_owner_can_delete_sent_manga_and_audit_is_preserved() -> None:
    job_id, job_dir = create_completed_job("sent")

    result = delete_manga_artifact(job_id, "10001")

    assert result["deleted"] is True
    assert not job_dir.exists()
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert json.loads(job.result or "{}")["artifact_deleted"] is True


def test_qq_manga_cannot_be_deleted_before_successful_delivery() -> None:
    job_id, job_dir = create_completed_job("failed")

    with pytest.raises(ValueError, match="尚未成功发送"):
        delete_manga_artifact(job_id, "10001")

    assert job_dir.is_dir()
