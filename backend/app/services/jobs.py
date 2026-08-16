from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.models import Document, Job
from app.db.session import SessionLocal
from app.services.documents import index_document, reindex_knowledge_base
from app.services.manga import manga_service

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
