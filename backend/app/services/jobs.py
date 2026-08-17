from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.models import Document, Job
from app.db.session import SessionLocal
from app.services.documents import index_document, reindex_knowledge_base
from app.services.manga import manga_service
from app.services.runtime import is_owner

logger = logging.getLogger(__name__)
JobNotifier = Callable[[Job], Awaitable[None]]


class JobWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self.notifier: JobNotifier | None = None

    def start(self) -> None:
        if self._task is None:
            with SessionLocal.begin() as session:
                session.execute(update(Job).where(Job.status == "running").values(status="queued"))
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="personal-agent-worker")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task:
            await self._task
            self._task = None
        self._stop = None

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            job_id = self._next_job_id()
            if job_id is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                except TimeoutError:
                    continue
            else:
                await self._execute(job_id)

    def _next_job_id(self) -> str | None:
        with SessionLocal.begin() as session:
            job = session.scalar(select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(1))
            if job is None:
                return None
            job.status = "running"
            return job.id

    async def _execute(self, job_id: str) -> None:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            try:
                if job.cancel_requested:
                    job.status = "cancelled"
                elif job.type == "document_index":
                    result = await asyncio.to_thread(
                        index_document, session, json.loads(job.payload)["document_id"]
                    )
                    job.result = json.dumps(result, ensure_ascii=False)
                    job.status = "succeeded"
                elif job.type == "knowledge_base_reindex":
                    payload = json.loads(job.payload)
                    result = await asyncio.to_thread(
                        reindex_knowledge_base,
                        session,
                        str(payload["knowledge_base_id"]),
                        str(payload["embedding_profile"]),
                    )
                    job.result = json.dumps(result, ensure_ascii=False)
                    job.status = "succeeded"
                elif job.type == "manga_download":
                    album_id = str(json.loads(job.payload)["album_id"])
                    path = await manga_service.download_pdf(album_id, get_settings().download_path / job.id)
                    job.result = json.dumps(
                        {"album_id": album_id, "path": str(path.resolve()), "size": path.stat().st_size},
                        ensure_ascii=False,
                    )
                    job.status = "succeeded"
                else:
                    raise ValueError(f"未知任务类型: {job.type}")
            except Exception as error:
                logger.exception("任务 %s 执行失败", job.id)
                session.rollback()
                job = session.get(Job, job_id)
                if job is None:
                    return
                if job.retry_count < 2 and job.type == "manga_download":
                    job.retry_count += 1
                    job.status = "queued"
                else:
                    job.status = "failed"
                    job.error = f"{type(error).__name__}: {error}"
                    if job.type == "document_index":
                        payload = json.loads(job.payload)
                        document = session.get(Document, payload.get("document_id"))
                        if document:
                            document.status = "failed"
                            document.error = job.error
            session.commit()
            session.refresh(job)
            if self.notifier and job.status in {"succeeded", "failed", "cancelled"}:
                try:
                    await self.notifier(job)
                except Exception:
                    logger.exception("任务通知失败: %s", job.id)


job_worker = JobWorker()


def create_job(
    job_type: str,
    payload: dict[str, object],
    requester_id: str = "local-owner",
    conversation_id: str | None = None,
) -> Job:
    with SessionLocal.begin() as session:
        job = Job(
            type=job_type,
            payload=json.dumps(payload, ensure_ascii=False),
            requester_id=requester_id,
            conversation_id=conversation_id,
        )
        session.add(job)
        session.flush()
        return job


def create_manga_download_job(
    album_id: str,
    requester_id: str,
    conversation_id: str | None = None,
) -> Job:
    if not is_owner(requester_id):
        raise PermissionError("只有 Owner 可以创建漫画下载任务")
    if not album_id.isdigit():
        raise ValueError("漫画 ID 必须为数字")
    return create_job(
        "manga_download",
        {"album_id": album_id},
        requester_id=requester_id,
        conversation_id=conversation_id,
    )


def update_job_result(job_id: str, updates: dict[str, object]) -> dict[str, object]:
    with SessionLocal.begin() as session:
        job = session.get(Job, job_id)
        if job is None or not job.result:
            raise ValueError("任务结果不存在")
        result = json.loads(job.result)
        result.update(updates)
        job.result = json.dumps(result, ensure_ascii=False)
        return result


def delete_manga_artifact(job_id: str, requester_id: str) -> dict[str, object]:
    if not is_owner(requester_id):
        raise PermissionError("只有 Owner 可以删除漫画产物")

    settings = get_settings()
    base_dir = settings.download_path.resolve()
    with SessionLocal.begin() as session:
        job = session.get(Job, job_id)
        if job is None or job.type != "manga_download":
            raise ValueError("漫画下载任务不存在")
        if job.status != "succeeded" or not job.result:
            raise ValueError("只有已成功完成的漫画任务可以删除产物")

        result = json.loads(job.result)
        if result.get("artifact_deleted"):
            raise ValueError("该漫画产物已经删除")
        if job.requester_id.isdigit() and result.get("delivery_status") != "sent":
            raise ValueError("QQ 文件尚未成功发送，不允许删除本地产物")

        job_dir = (base_dir / job.id).resolve()
        if job_dir.parent != base_dir or not job_dir.is_dir():
            raise ValueError("漫画任务目录不存在或路径无效")
        artifact_path = Path(str(result.get("path", ""))).resolve()
        if job_dir not in artifact_path.parents or not artifact_path.is_file():
            raise ValueError("漫画任务产物不存在或路径无效")

        deleted_size = 0
        for path in job_dir.rglob("*"):
            if path.is_symlink() or path.is_junction():
                raise ValueError("漫画任务目录包含链接，拒绝删除")
            if path.is_file():
                deleted_size += path.stat().st_size
        shutil.rmtree(job_dir)

        result.update(
            {
                "artifact_deleted": True,
                "deleted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "deleted_by": requester_id,
                "deleted_size": deleted_size,
            }
        )
        job.result = json.dumps(result, ensure_ascii=False)
        return {
            "job_id": job.id,
            "album_id": result.get("album_id"),
            "deleted": True,
            "deleted_size": deleted_size,
        }
