from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.models import (
    CompanionPreference,
    Conversation,
    Document,
    EmotionAssessment,
    Job,
    Message,
    SafetyEvent,
)
from app.db.session import SessionLocal
from app.services.companion import (
    COMPANION_ANALYSIS_RETRY_JOB_TYPE,
    SafetyMode,
    SupportMode,
    analyze_message,
    detect_support_mode,
    resolve_analyzer_alias,
)
from app.services.documents import index_document, reindex_knowledge_base
from app.services.manga import manga_service
from app.services.operation_logs import operation_logs
from app.services.runtime import is_owner
from app.services.time_context import current_time_context, format_messages_for_model

logger = logging.getLogger(__name__)
JobNotifier = Callable[[Job], Awaitable[None]]


def _index_document_in_worker_session(document_id: str) -> dict[str, object]:
    with SessionLocal() as worker_session:
        return index_document(worker_session, document_id)


def _reindex_knowledge_base_in_worker_session(
    knowledge_base_id: str,
    embedding_profile: str,
) -> dict[str, object]:
    with SessionLocal() as worker_session:
        return reindex_knowledge_base(worker_session, knowledge_base_id, embedding_profile)


def _companion_retry_context(session, message: Message) -> str:
    messages = list(
        session.scalars(
            select(Message)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.created_at < message.created_at,
            )
            .order_by(Message.created_at.desc())
            .limit(6)
        )
    )
    messages.reverse()
    return format_messages_for_model(messages)


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
            operation_logs.emit(
                source="worker", kind="started", title="Worker 启动", message="后台任务 Worker 已启动"
            )

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
        operation_logs.emit(
            source="worker", kind="succeeded", title="Worker 停止", message="后台任务 Worker 已停止"
        )

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
            operation_logs.emit(
                source="worker",
                kind="started",
                title="任务开始",
                message=f"开始执行 {job.type}",
                operation_id=job.id,
                details={"job_id": job.id, "type": job.type},
            )
            return job.id

    async def _execute_companion_analysis_retry(self, session, job: Job) -> dict[str, object]:
        try:
            payload = json.loads(job.payload)
        except json.JSONDecodeError as error:
            raise ValueError("companion_retry_invalid_payload") from error
        assessment_id = str(payload.get("assessment_id") or "")
        user_message_id = str(payload.get("user_message_id") or "")
        assessment = session.get(EmotionAssessment, assessment_id)
        message = session.get(Message, user_message_id)
        if assessment is None or message is None:
            return {"status": "skipped", "reason": "target_missing", "assessment_id": assessment_id}
        if assessment.schema_valid:
            return {"status": "skipped", "reason": "already_valid", "assessment_id": assessment_id}
        conversation = session.get(Conversation, message.conversation_id)
        if conversation is None:
            raise ValueError("companion_retry_conversation_missing")

        forced_value = payload.get("forced_mode")
        if isinstance(forced_value, str) and forced_value in {"listen", "reflect", "advice"}:
            forced_mode = cast(SupportMode, forced_value)
        else:
            forced_mode = detect_support_mode(message.content)
        preferred_alias = str(payload.get("analyzer_model_alias") or assessment.model_alias or "")
        analyzer_alias = resolve_analyzer_alias(preferred_alias or None, conversation.model_alias)
        safety_value = payload.get("safety_mode")
        if safety_value not in {"standard", "unfiltered"}:
            preference = session.scalar(
                select(CompanionPreference).where(
                    CompanionPreference.scope_type == "qq_user",
                    CompanionPreference.scope_id == assessment.scope_id,
                )
            )
            safety_value = getattr(preference, "safety_mode", "standard") if preference else "standard"
        safety_mode: SafetyMode = cast(
            SafetyMode, safety_value if safety_value in {"standard", "unfiltered"} else "standard"
        )
        time_context = current_time_context()
        analysis = await asyncio.to_thread(
            analyze_message,
            message.content,
            _companion_retry_context(session, message),
            analyzer_alias,
            forced_mode=forced_mode,
            safety_mode=safety_mode,
            allow_repair=False,
            time_context=time_context,
        )
        if not analysis.schema_valid:
            raise ValueError("companion_retry_schema_invalid")

        assessment.candidate_emotions = json.dumps(analysis.candidate_emotions, ensure_ascii=False)
        assessment.primary_emotion = analysis.primary_emotion
        assessment.intensity = analysis.intensity
        assessment.support_need = analysis.support_need
        assessment.confidence = analysis.confidence
        assessment.risk_level = analysis.risk_level
        assessment.next_action = analysis.next_action
        assessment.model_alias = analysis.model_alias or assessment.model_alias
        assessment.prompt_version = analysis.prompt_version
        assessment.classifier_version = analysis.classifier_version
        assessment.schema_valid = True
        if analysis.risk_level in {"high", "critical"}:
            safety_action = (
                "unfiltered_passthrough"
                if safety_mode == "unfiltered"
                else analysis.next_action
            )
            existing_safety = session.scalar(
                select(SafetyEvent)
                .where(
                    SafetyEvent.message_id == message.id,
                    SafetyEvent.risk_level.in_(["high", "critical"]),
                    SafetyEvent.action == safety_action,
                )
                .limit(1)
            )
            if existing_safety is None:
                session.add(
                    SafetyEvent(
                        message_id=message.id,
                        scope_id=assessment.scope_id,
                        risk_level=analysis.risk_level,
                        action=safety_action,
                        details=json.dumps(
                            {
                                "source": "analysis_retry",
                                "safety_mode": safety_mode,
                                "rules": [],
                                "model_alias": analysis.model_alias or analyzer_alias,
                                "prompt_version": analysis.prompt_version,
                                "classifier_version": analysis.classifier_version,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
        return {
            "status": "valid",
            "assessment_id": assessment_id,
            "risk_level": analysis.risk_level,
            "model_alias": analysis.model_alias,
            "next_action": analysis.next_action,
            "safety_mode": safety_mode,
        }

    async def _execute(self, job_id: str) -> None:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            try:
                if job.cancel_requested:
                    job.status = "cancelled"
                    operation_logs.finish_operation(
                        job.id,
                        source="worker",
                        title="任务取消",
                        message=f"任务 {job.id} 已取消",
                        success=False,
                        details={"job_id": job.id, "status": "cancelled"},
                    )
                elif job.type == "document_index":
                    result = await asyncio.to_thread(
                        _index_document_in_worker_session,
                        json.loads(job.payload)["document_id"],
                    )
                    job.result = json.dumps(result, ensure_ascii=False)
                    job.status = "succeeded"
                    operation_logs.finish_operation(
                        job.id,
                        source="worker",
                        title="任务完成",
                        message=f"文档索引任务 {job.id} 已完成",
                        details={"job_id": job.id, "type": job.type},
                    )
                elif job.type == "knowledge_base_reindex":
                    payload = json.loads(job.payload)
                    result = await asyncio.to_thread(
                        _reindex_knowledge_base_in_worker_session,
                        str(payload["knowledge_base_id"]),
                        str(payload["embedding_profile"]),
                    )
                    job.result = json.dumps(result, ensure_ascii=False)
                    job.status = "succeeded"
                    operation_logs.finish_operation(
                        job.id,
                        source="worker",
                        title="任务完成",
                        message=f"知识库重建任务 {job.id} 已完成",
                        details={"job_id": job.id, "type": job.type},
                    )
                elif job.type == "manga_download":
                    album_id = str(json.loads(job.payload)["album_id"])
                    path = await manga_service.download_pdf(
                        album_id, get_settings().download_path / job.id, operation_id=job.id
                    )
                    job.result = json.dumps(
                        {"album_id": album_id, "path": str(path.resolve()), "size": path.stat().st_size},
                        ensure_ascii=False,
                    )
                    job.status = "succeeded"
                    operation_logs.finish_operation(
                        job.id,
                        source="worker",
                        title="任务完成",
                        message=f"漫画下载任务 {job.id} 已完成",
                        details={"job_id": job.id, "type": job.type, "path": str(path.resolve())},
                    )
                elif job.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE:
                    result = await self._execute_companion_analysis_retry(session, job)
                    job.result = json.dumps(result, ensure_ascii=False)
                    job.status = "succeeded"
                    operation_logs.finish_operation(
                        job.id,
                        source="worker",
                        title="陪伴分析恢复完成",
                        message=f"陪伴分析任务 {job.id} 已完成",
                        details={"job_id": job.id, "type": job.type, "status": result.get("status")},
                    )
                else:
                    raise ValueError(f"未知任务类型: {job.type}")
            except Exception as error:
                logger.exception("任务 %s 执行失败", job.id)
                session.rollback()
                job = session.get(Job, job_id)
                if job is None:
                    return
                if job.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE:
                    safe_errors = {
                        "companion_retry_invalid_payload",
                        "companion_retry_conversation_missing",
                        "companion_retry_schema_invalid",
                    }
                    job.error = (
                        str(error)
                        if str(error) in safe_errors
                        else "companion_retry_failed"
                    )
                else:
                    job.error = f"{type(error).__name__}: {error}"
                should_retry = (
                    job.type == "manga_download" and job.retry_count < 2
                ) or (
                    job.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE and job.retry_count < 1
                )
                if should_retry:
                    job.retry_count += 1
                    job.status = "queued"
                    operation_logs.update_operation(
                        job.id,
                        source="worker",
                        title="任务重试",
                        message=f"任务将在稍后重试（第 {job.retry_count} 次）",
                        details={"job_id": job.id, "retry_count": job.retry_count},
                    )
                else:
                    job.status = "failed"
                    operation_logs.finish_operation(
                        job.id,
                        source="worker",
                        title="任务失败",
                        message=f"任务 {job.id} 执行失败",
                        success=False,
                        details={"job_id": job.id, "type": job.type, "error": job.error},
                    )
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


def create_companion_analysis_retry_job(
    *,
    assessment_id: str,
    user_message_id: str,
    requester_id: str,
    conversation_id: str,
    forced_mode: SupportMode | None,
    analyzer_model_alias: str | None = None,
    safety_mode: SafetyMode = "standard",
) -> Job | None:
    """Queue one idempotent, non-user-visible recovery task for a failed assessment."""
    with SessionLocal.begin() as session:
        existing = session.get(Job, assessment_id)
        if existing is not None:
            if existing.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE:
                return existing
            logger.warning("陪伴分析任务 ID 冲突，跳过恢复任务: assessment=%s", assessment_id)
            return None
        job = Job(
            id=assessment_id,
            type=COMPANION_ANALYSIS_RETRY_JOB_TYPE,
            requester_id=requester_id,
            conversation_id=conversation_id,
            payload=json.dumps(
                {
                    "assessment_id": assessment_id,
                    "user_message_id": user_message_id,
                    "forced_mode": forced_mode,
                    "analyzer_model_alias": analyzer_model_alias,
                    "safety_mode": safety_mode,
                },
                ensure_ascii=False,
            ),
        )
        session.add(job)
        session.flush()
        operation_logs.emit(
            source="worker",
            kind="queued",
            title="陪伴分析恢复排队",
            message=f"已创建陪伴分析恢复任务 {job.id}",
            operation_id=job.id,
            details={
                "job_id": job.id,
                "type": job.type,
                "assessment_id": assessment_id,
                "user_message_id": user_message_id,
            },
        )
        return job


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
        operation_logs.emit(
            source="worker",
            kind="queued",
            title="任务排队",
            message=f"已创建 {job_type} 任务",
            operation_id=job.id,
            details={
                "job_id": job.id,
                "type": job_type,
                "requester_id": requester_id,
                "conversation_id": conversation_id,
                "payload": payload,
            },
        )
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
