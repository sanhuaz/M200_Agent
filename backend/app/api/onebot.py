from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, WebSocket
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.onebot_delivery import send_private_file as deliver_private_file
from app.api.onebot_delivery import send_text as deliver_text
from app.api.onebot_media import OneBotMediaError, extract_image_refs, fetch_attachments
from app.api.onebot_messages import normalize_cq_text, text_from_event
from app.api.onebot_transport import OneBotTransport
from app.core.config import get_settings
from app.db.models import (
    AppSetting,
    Artifact,
    CompanionListeningBuffer,
    Conversation,
    EmotionAssessment,
    ExtensionPackage,
    Job,
    KnowledgeBase,
    Memory,
    Message,
    ProcessedEvent,
    ResponseFeedback,
)
from app.db.session import SessionLocal
from app.domain.chat_inputs import ChatAttachmentInput, ChatTurnInput
from app.domain.reply_types import ReplyPlan
from app.services.chat import chat_service
from app.services.chat_attachments import (
    MAX_ATTACHMENTS,
    MAX_TOTAL_BYTES,
    AttachmentValidationError,
    delete_pending_attachments,
    pending_attachment_inputs,
    stage_pending_attachments,
    validate_inputs,
)
from app.services.companion import (
    COMPANION_ANALYSIS_RETRY_JOB_TYPE,
    EMOTION_LABEL_ZH,
    SUPPORT_NEED_ZH,
    assessment_dict,
    get_or_create_preference,
    parse_emotion_labels_strict,
    safety_precheck,
    safety_redirect_text,
)
from app.services.confirmations import (
    create_extension_confirmation,
    is_owner,
    resolve_confirmation,
)
from app.services.context import estimate_text_tokens, load_pending_messages, pending_message_count
from app.services.jobs import (
    create_manga_download_job,
    delete_manga_artifact,
    job_worker,
    update_job_result,
)
from app.services.manga import manga_service
from app.services.memories import MemoryService
from app.services.models import model_registry
from app.services.operation_logs import operation_logs
from app.services.persona_store import get_persona_store
from app.services.qq_delivery import (
    DEFAULT_CHUNK_TARGET_CHARS,
    MAX_CHUNK_TARGET_CHARS,
    MIN_CHUNK_TARGET_CHARS,
    chunk_length_bounds,
    normalize_chunk_target,
    plan_delivery_parts,
    qq_reply_delay_seconds,
    reply_plan_from_event,
    split_qq_reply,
)
from app.services.time_context import utc_isoformat

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()
QQ_CHUNKED_OUTPUT_SETTING_KEY = "qq_chunked_output_enabled"
QQ_CHUNK_TARGET_SETTING_KEY = "qq_chunk_target_chars"
DEFAULT_QQ_CHUNKED_OUTPUT_ENABLED = True


@dataclass(frozen=True, slots=True)
class ListeningBatch:
    text: str
    message_id: str
    scope_id: str
    fragments: tuple[dict[str, object], ...]
    attachment_metadata: tuple[dict[str, object], ...]


def qq_chunked_output_enabled(session: Session) -> bool:
    item = session.get(AppSetting, QQ_CHUNKED_OUTPUT_SETTING_KEY)
    if item is None:
        return DEFAULT_QQ_CHUNKED_OUTPUT_ENABLED
    try:
        value = json.loads(item.value)
    except json.JSONDecodeError:
        value = item.value.strip().lower()
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in {"true", "1", "on", "enabled"}:
        return True
    if isinstance(value, str) and value in {"false", "0", "off", "disabled"}:
        return False
    return DEFAULT_QQ_CHUNKED_OUTPUT_ENABLED


def set_qq_chunked_output_enabled(session: Session, enabled: bool) -> None:
    item = session.get(AppSetting, QQ_CHUNKED_OUTPUT_SETTING_KEY)
    if item is None:
        item = AppSetting(key=QQ_CHUNKED_OUTPUT_SETTING_KEY, value="")
        session.add(item)
    item.value = json.dumps(bool(enabled))


def qq_chunk_target_chars(session: Session) -> int:
    item = session.get(AppSetting, QQ_CHUNK_TARGET_SETTING_KEY)
    if item is None:
        return DEFAULT_CHUNK_TARGET_CHARS
    try:
        value = json.loads(item.value)
    except json.JSONDecodeError:
        value = item.value.strip()
    return normalize_chunk_target(value)


def qq_reply_settings(session: Session) -> dict[str, object]:
    target = qq_chunk_target_chars(session)
    minimum, maximum = chunk_length_bounds(target)
    return {
        "chunked_output_enabled": qq_chunked_output_enabled(session),
        "chunk_target_chars": target,
        "chunk_min_chars": minimum,
        "chunk_max_chars": maximum,
    }


def set_qq_reply_settings(
    session: Session,
    *,
    enabled: bool | None = None,
    target_chars: int | None = None,
) -> None:
    if enabled is not None:
        set_qq_chunked_output_enabled(session, enabled)
    if target_chars is not None:
        try:
            target = int(target_chars)
        except (TypeError, ValueError) as error:
            raise ValueError("QQ 分段目标字数必须是整数") from error
        if not MIN_CHUNK_TARGET_CHARS <= target <= MAX_CHUNK_TARGET_CHARS:
            raise ValueError("QQ 分段目标字数必须在 5–30 之间")
        item = session.get(AppSetting, QQ_CHUNK_TARGET_SETTING_KEY)
        if item is None:
            item = AppSetting(key=QQ_CHUNK_TARGET_SETTING_KEY, value="")
            session.add(item)
        item.value = json.dumps(target)


class OneBotManager:
    def __init__(self) -> None:
        self._transport = OneBotTransport(settings.onebot_token)
        self.self_id: str | None = None
        self.qq_status = "unknown"
        self.qq_nickname: str | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._listening_tasks: dict[str, asyncio.Task[None]] = {}
        self._listening_locks: dict[str, asyncio.Lock] = {}
        self._listening_processing: set[str] = set()
        self._listening_processing_done: dict[str, asyncio.Event] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._restore_listening_task: asyncio.Task[None] | None = None

    @property
    def websocket(self) -> WebSocket | None:
        return self._transport.websocket

    def status(self) -> dict[str, object]:
        return {
            "connection": "connected" if self.websocket is not None else "disconnected",
            "qq": self.qq_status,
            "self_id": self.self_id,
            "nickname": self.qq_nickname,
        }

    def start_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_login(), name="onebot-login-monitor")
        if self._restore_listening_task is None or self._restore_listening_task.done():
            self._restore_listening_task = asyncio.create_task(
                self._restore_listening_buffers(), name="onebot-listening-restore"
            )

    async def stop_monitor(self) -> None:
        task, self._monitor_task = self._monitor_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        restore_task, self._restore_listening_task = self._restore_listening_task, None
        if restore_task is not None:
            restore_task.cancel()
            try:
                await restore_task
            except asyncio.CancelledError:
                pass
        tasks, self._listening_tasks = list(self._listening_tasks.values()), {}
        for listening_task in tasks:
            listening_task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _restore_listening_buffers(self) -> None:
        """Re-arm persisted listening buffers after an application restart."""

        await asyncio.sleep(0)
        with SessionLocal() as session:
            rows = session.scalars(select(CompanionListeningBuffer)).all()
            pending: list[tuple[str, str, str, int]] = []
            cleanup: list[tuple[str, list[dict[str, object]]]] = []
            now = datetime.now(UTC).replace(tzinfo=None)
            for buffer in rows:
                conversation = session.get(Conversation, buffer.conversation_id)
                preference = get_or_create_preference(session, buffer.scope_id)
                if (
                    conversation is None
                    or not preference.listening_enabled
                    or not preference.companion_enabled
                ):
                    cleanup.append((buffer.id, self._buffer_attachment_metadata(buffer)))
                    continue
                elapsed = max(0.0, (now - (buffer.updated_at or buffer.created_at)).total_seconds())
                silence = max(5, min(120, int(preference.listening_silence_seconds or 30)))
                pending.append(
                    (
                        buffer.conversation_id,
                        buffer.scope_id,
                        conversation.external_id or f"private:{buffer.scope_id}",
                        max(0, int(silence - elapsed)),
                    )
                )
            if cleanup:
                cleanup_ids = {buffer_id for buffer_id, _metadata in cleanup}
                for buffer in rows:
                    if buffer.id in cleanup_ids:
                        session.delete(buffer)
                session.commit()
        for _buffer_id, metadata in cleanup:
            delete_pending_attachments(metadata)
        for conversation_id, user_id, external_id, seconds in pending:
            self._schedule_listening_flush(conversation_id, user_id, external_id, seconds)

    def _listening_lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._listening_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._listening_locks[conversation_id] = lock
        return lock

    def _chat_lock(self, external_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(external_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[external_id] = lock
        return lock

    async def _monitor_login(self) -> None:
        while True:
            try:
                if self.websocket is None:
                    self.qq_status = "offline"
                else:
                    response = await self.action("get_login_info", {})
                    raw_data = response.get("data")
                    data: dict[str, object] = raw_data if isinstance(raw_data, dict) else {}
                    if response.get("status") == "ok" and int(str(response.get("retcode", -1))) == 0:
                        self.qq_status = "online"
                        self.self_id = str(data.get("user_id") or self.self_id or "")
                        self.qq_nickname = str(data.get("nickname") or "") or None
                    else:
                        self.qq_status = "offline"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.qq_status = "unknown" if self.websocket is not None else "offline"
                logger.debug("OneBot 登录状态检查失败：%s", type(error).__name__)
            await asyncio.sleep(30)

    async def serve(self, websocket: WebSocket) -> None:
        self.qq_status = "unknown"
        await self._transport.serve(websocket, self._dispatch_message_event)
        self.self_id = None
        self.qq_status = "offline"

    async def _dispatch_message_event(self, payload: dict[str, object]) -> None:
        if self._text_from_event(payload):
            operation_logs.emit(
                source="onebot",
                kind="message",
                title="收到 QQ 消息",
                message="收到文本消息（正文不写入连接日志）",
                details={
                    "user_id": payload.get("user_id"),
                    "group_id": payload.get("group_id"),
                    "message_id": payload.get("message_id"),
                },
            )
        await self._handle_message(payload)

    async def action(
        self,
        action: str,
        params: dict[str, object],
        *,
        timeout_seconds: float = 30,
    ) -> dict[str, object]:
        return await self._transport.action(action, params, timeout_seconds=timeout_seconds)

    async def send_text(self, user_id: str, text: str, group_id: str | None = None) -> None:
        await deliver_text(self.action, user_id, text, group_id)

    async def send_private_file(self, user_id: str, path: Path) -> None:
        await deliver_private_file(self.action, user_id, path)

    @staticmethod
    def _normalize_cq_text(value: str, *, preserve_mentions: bool = False) -> str:
        return normalize_cq_text(value, preserve_mentions=preserve_mentions)

    @classmethod
    def _text_from_event(cls, event: dict[str, object]) -> str:
        return text_from_event(event)

    async def _handle_message(self, event: dict[str, object]) -> None:
        message_id = str(event.get("message_id", ""))
        if not message_id:
            return
        with SessionLocal.begin() as session:
            if session.get(ProcessedEvent, message_id):
                return
            session.add(ProcessedEvent(message_id=message_id))
        self.self_id = str(event.get("self_id", self.self_id or ""))
        message_type = str(event.get("message_type", "private"))
        user_id = str(event.get("user_id", ""))
        group_id = str(event.get("group_id", "")) if message_type == "group" else None
        raw = self._text_from_event(event)
        text = self._extract_triggered_text(raw, message_type)
        image_refs: tuple[object, ...] = ()
        try:
            image_refs = extract_image_refs(event)
        except OneBotMediaError as error:
            if user_id and text is not None:
                try:
                    await self.send_text(user_id, f"图片处理失败：{error}", group_id)
                except Exception:
                    logger.exception("发送 QQ 图片错误回复失败")
            return
        has_images = bool(image_refs)
        if text is None or (not text.strip() and not has_images) or not user_id:
            if user_id and not text and not has_images:
                operation_logs.emit(
                    source="onebot",
                    kind="ignored",
                    title="忽略无文本 QQ 事件",
                    message="文件回执或空白消息未进入聊天流程",
                    details={"user_id": user_id, "message_id": message_id},
                )
            return
        text = self._expand_manual_extension_request(text)
        external_id = f"group:{group_id}" if group_id else f"private:{user_id}"
        try:
            attachments = await fetch_attachments(event, self.action) if has_images else ()
            if attachments:
                try:
                    validate_inputs(attachments)
                except AttachmentValidationError as error:
                    await self.send_text(user_id, f"图片处理失败：{error}", group_id)
                    return
            if group_id is None and is_owner(user_id):
                listening_result = await self._handle_listening_input(
                    text, user_id, external_id, message_id, attachments
                )
                if listening_result is not None:
                    if listening_result:
                        async with self._chat_lock(external_id):
                            await self.send_text(user_id, listening_result)
                    return
            async with self._chat_lock(external_id):
                command_response = await self._command(text, user_id, external_id)
                if command_response is not None:
                    try:
                        await self.send_text(user_id, command_response, group_id)
                    except Exception as error:
                        operation_logs.emit(
                            source="onebot",
                            kind="failed",
                            title="QQ 命令发送失败",
                            message="命令结果未送达",
                            details={"user_id": user_id, "error": type(error).__name__},
                        )
                        raise
                    return
                await self._run_chat_response_unlocked(
                    user_id, text, message_id, group_id, external_id, attachments
                )
        except OneBotMediaError as error:
            try:
                await self.send_text(user_id, f"图片处理失败：{error}", group_id)
            except Exception:
                logger.exception("发送 QQ 图片错误回复失败")
        except Exception as error:
            logger.exception("处理 QQ 消息失败")
            try:
                await self.send_text(user_id, f"处理失败：{type(error).__name__}: {error}", group_id)
            except Exception:
                logger.exception("发送 QQ 错误回复失败")

    async def _run_chat_response(
        self,
        user_id: str,
        text: str,
        message_id: str | None,
        group_id: str | None,
        external_id: str,
        attachments: tuple[ChatAttachmentInput, ...] = (),
    ) -> None:
        async with self._chat_lock(external_id):
            await self._run_chat_response_unlocked(
                user_id, text, message_id, group_id, external_id, attachments
            )

    async def _run_chat_response_unlocked(
        self,
        user_id: str,
        text: str,
        message_id: str | None,
        group_id: str | None,
        external_id: str,
        attachments: tuple[ChatAttachmentInput, ...] = (),
    ) -> None:
        with SessionLocal() as session:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.platform == "qq", Conversation.external_id == external_id
                )
            )
            if attachments:
                selected_alias = (
                    conversation.model_alias
                    if conversation is not None
                    else model_registry.default_alias()
                )
                if not model_registry.profile(selected_alias).supports_vision:
                    await self.send_text(
                        user_id,
                        "图片处理失败：当前模型未启用识图能力，请在模型管理中开启后重试。",
                        group_id,
                    )
                    return
            if conversation is None:
                conversation = Conversation(
                    platform="qq",
                    external_id=external_id,
                    conversation_type="group" if group_id else "private",
                    title=f"QQ {external_id}",
                    model_alias=model_registry.default_alias(),
                    owner_id=user_id,
                )
                session.add(conversation)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    conversation = session.scalar(
                        select(Conversation).where(
                            Conversation.platform == "qq", Conversation.external_id == external_id
                        )
                    )
                if conversation is None:
                    raise RuntimeError("无法创建 QQ 会话")
            chunked_output = qq_chunked_output_enabled(session)
            chunk_target_chars = qq_chunk_target_chars(session)
            final_text = ""
            reply_plan: ReplyPlan | None = None
            response_source = ""
            error_text = ""
            safety_intercepted = False
            mcp_functional_reply = False
            artifact_ids: list[str] = []
            stream_input: str | ChatTurnInput = text
            if attachments:
                stream_input = ChatTurnInput(
                    text=text,
                    attachments=tuple(attachments),
                    conversation_id=conversation.id,
                    sender_id=user_id,
                    platform_message_id=message_id,
                    platform="qq",
                    is_group=bool(group_id),
                    group_id=group_id,
                    trigger="onebot",
                )
            async for item in chat_service.stream(
                session,
                conversation,
                user_id,
                stream_input,
                message_id,
                platform="qq",
                is_group=bool(group_id),
                reply_max_segments=3 if chunked_output else 1,
            ):
                event_data = item["data"]
                if not isinstance(event_data, dict):
                    continue
                if item["event"] == "final":
                    final_text = str(event_data.get("text", ""))
                    reply_plan = reply_plan_from_event(event_data)
                    response_source = str(event_data.get("response_source") or "agent")
                    mcp_functional_reply = bool(event_data.get("mcp_functional_reply"))
                    final_artifacts = event_data.get("artifacts")
                    if isinstance(final_artifacts, list):
                        for artifact in final_artifacts:
                            if not isinstance(artifact, dict):
                                continue
                            artifact_id = str(
                                artifact.get("artifact_id") or artifact.get("id") or ""
                            )
                            if artifact_id and artifact_id not in artifact_ids:
                                artifact_ids.append(artifact_id)
                elif item["event"] == "companion_analysis":
                    safety_intercepted = bool(event_data.get("safety_intercepted"))
                elif item["event"] == "error":
                    error_text = str(event_data.get("message", ""))
                elif item["event"] == "artifact_created":
                    artifact_id = str(
                        event_data.get("artifact_id") or event_data.get("id") or ""
                    )
                    if artifact_id and artifact_id not in artifact_ids:
                        artifact_ids.append(artifact_id)
            reply_text = final_text or f"处理失败：{error_text}"
            chunkable_response_sources = {"agent", "rewritten", "style_rewritten"}
            policy_preserved_original = (
                reply_plan is not None
                and reply_plan.policy_status == "original_preserved"
            )
            if (
                mcp_functional_reply
                and final_text
                and not error_text
                and not safety_intercepted
            ):
                chunks = split_qq_reply(reply_text, target_chars=chunk_target_chars)
            elif (
                reply_plan is not None
                and final_text
                and not error_text
                and not safety_intercepted
                and (
                    response_source in chunkable_response_sources
                    or policy_preserved_original
                )
            ):
                chunks = plan_delivery_parts(reply_plan, target_chars=chunk_target_chars)
            elif (
                final_text
                and not error_text
                and chunked_output
                and not safety_intercepted
                and response_source in chunkable_response_sources
            ):
                # Compatibility path for older/custom ChatService producers
                # that do not yet carry a ReplyPlan in the final event.
                chunks = split_qq_reply(reply_text, target_chars=chunk_target_chars)
            else:
                chunks = [reply_text]
            chunks = [chunk for chunk in chunks if chunk.strip()] or [reply_text]
            sent_count = 0
            try:
                for index, chunk in enumerate(chunks):
                    if index:
                        await asyncio.sleep(qq_reply_delay_seconds(chunks[index - 1]))
                    await self.send_text(user_id, chunk, group_id)
                    sent_count += 1
            except Exception as error:
                operation_logs.emit(
                    source="onebot",
                    kind="failed",
                    title="QQ 回复分段发送失败",
                    message="回复已写入数据库，但部分内容未送达",
                    details={
                        "conversation_id": conversation.id,
                        "error": type(error).__name__,
                        "chunk_count": len(chunks),
                        "sent_count": sent_count,
                        "failed_index": sent_count + 1,
                    },
                )
                return
            if len(chunks) > 1:
                operation_logs.emit(
                    source="onebot",
                    kind="succeeded",
                    title="QQ 回复分段发送完成",
                    message="已按自然语义分批发送回复",
                    details={
                        "conversation_id": conversation.id,
                        "chunk_count": len(chunks),
                    },
                )
            if not group_id:
                with SessionLocal() as artifact_session:
                    artifacts = [artifact_session.get(Artifact, artifact_id) for artifact_id in artifact_ids]
                for artifact in artifacts:
                    if artifact is None:
                        operation_logs.emit(
                            source="onebot",
                            level="warn",
                            kind="failed",
                            title="QQ 文件发送失败",
                            message="Artifact 记录不存在，未确认 QQ 文件送达",
                            details={
                                "conversation_id": conversation.id,
                                "delivery_status": "failed",
                                "reason": "artifact_missing",
                            },
                        )
                        continue
                    if not Path(artifact.path).is_file():
                        operation_logs.emit(
                            source="onebot",
                            level="warn",
                            kind="failed",
                            title="QQ 文件发送失败",
                            message="Artifact 文件不存在，未确认 QQ 文件送达",
                            details={
                                "conversation_id": conversation.id,
                                "artifact_id": artifact.id,
                                "filename": artifact.filename,
                                "delivery_status": "failed",
                                "reason": "artifact_file_missing",
                            },
                        )
                        continue
                    path = Path(artifact.path)
                    if path.stat().st_size <= settings.qq_upload_limit_mb * 1024 * 1024:
                        try:
                            await self.send_private_file(user_id, path)
                            operation_logs.emit(
                                source="onebot",
                                kind="succeeded",
                                title="QQ 文件发送完成",
                                message="Artifact 已实际收到 QQ 文件发送成功回执",
                                details={
                                    "conversation_id": conversation.id,
                                    "artifact_id": artifact.id,
                                    "filename": artifact.filename,
                                    "size": artifact.size,
                                    "delivery_status": "sent",
                                },
                            )
                        except Exception as error:
                            operation_logs.emit(
                                source="onebot",
                                level="warn",
                                kind="failed",
                                title="QQ 文件发送失败",
                                message="Artifact 已保留，但 QQ 文件未确认送达",
                                details={
                                    "conversation_id": conversation.id,
                                    "artifact_id": artifact.id,
                                    "filename": artifact.filename,
                                    "size": artifact.size,
                                    "delivery_status": "failed",
                                    "error": type(error).__name__,
                                },
                            )
                            await self.send_text(
                                user_id,
                                f"文件发送失败：{error}\n本地路径：{path.resolve()}",
                            )
                    else:
                        operation_logs.emit(
                            source="onebot",
                            level="warn",
                            kind="failed",
                            title="QQ 文件发送失败",
                            message="Artifact 超过 QQ 上传阈值，未确认送达",
                            details={
                                "conversation_id": conversation.id,
                                "artifact_id": artifact.id,
                                "filename": artifact.filename,
                                "size": artifact.size,
                                "delivery_status": "failed",
                                "reason": "qq_upload_limit",
                            },
                        )
                        await self.send_text(
                            user_id,
                            f"文件超过 QQ 上传阈值，本地路径：{path.resolve()}",
                        )

    @staticmethod
    def _ensure_qq_conversation(external_id: str, user_id: str) -> str:
        with SessionLocal() as session:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.platform == "qq", Conversation.external_id == external_id
                )
            )
            if conversation is None:
                conversation = Conversation(
                    platform="qq",
                    external_id=external_id,
                    conversation_type="private",
                    title=f"QQ {external_id}",
                    model_alias=model_registry.default_alias(),
                    owner_id=user_id,
                )
                session.add(conversation)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    conversation = session.scalar(
                        select(Conversation).where(
                            Conversation.platform == "qq", Conversation.external_id == external_id
                        )
                    )
                if conversation is None:
                    raise RuntimeError("无法创建 QQ 会话")
            return conversation.id

    @staticmethod
    def _buffer_fragments(buffer: CompanionListeningBuffer | None) -> list[dict[str, object]]:
        if buffer is None:
            return []
        try:
            values = json.loads(buffer.fragments or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, dict)]

    @classmethod
    def _buffer_text(cls, buffer: CompanionListeningBuffer | None) -> str:
        return "\n".join(
            str(item.get("text", "")).strip()
            for item in cls._buffer_fragments(buffer)
            if str(item.get("text", "")).strip()
        ).strip()

    @classmethod
    def _buffer_attachment_metadata(
        cls, buffer: CompanionListeningBuffer | None
    ) -> list[dict[str, object]]:
        attachments: list[dict[str, object]] = []
        for fragment in cls._buffer_fragments(buffer):
            values = fragment.get("attachments")
            if not isinstance(values, list):
                continue
            attachments.extend(item for item in values if isinstance(item, dict))
        return attachments

    @staticmethod
    def _buffer_count(buffer: CompanionListeningBuffer | None) -> int:
        if buffer is None:
            return 0
        try:
            values = json.loads(buffer.fragments or "[]")
        except json.JSONDecodeError:
            return 0
        return len(values) if isinstance(values, list) else 0

    async def _claim_listening_batch(self, conversation_id: str) -> ListeningBatch | None:
        """Atomically claim one batch and mark the session as processing.

        A completion command may arrive at the same time as the silence timer.
        Waiting for the current processing event here prevents either caller
        from dropping a batch or invoking the main Agent twice.
        """

        while True:
            lock = self._listening_lock(conversation_id)
            wait_event: asyncio.Event | None = None
            async with lock:
                if conversation_id in self._listening_processing:
                    wait_event = self._listening_processing_done.get(conversation_id)
                else:
                    pending = self._listening_tasks.pop(conversation_id, None)
                    current_task = asyncio.current_task()
                    if pending is not None and pending is not current_task:
                        pending.cancel()
                    with SessionLocal.begin() as session:
                        buffer = session.scalar(
                            select(CompanionListeningBuffer).where(
                                CompanionListeningBuffer.conversation_id == conversation_id
                            )
                        )
                        if buffer is None:
                            return None
                        fragments = tuple(self._buffer_fragments(buffer))
                        text = self._buffer_text(buffer)
                        attachment_metadata = tuple(self._buffer_attachment_metadata(buffer))
                        session.delete(buffer)
                    if not text and not attachment_metadata:
                        return None
                    self._listening_processing.add(conversation_id)
                    self._listening_processing_done[conversation_id] = asyncio.Event()
                    return ListeningBatch(
                        text=text,
                        message_id=f"listening-{uuid.uuid4().hex}",
                        scope_id=buffer.scope_id,
                        fragments=fragments,
                        attachment_metadata=attachment_metadata,
                    )
            if wait_event is None:
                return None
            await wait_event.wait()

    @staticmethod
    def _restore_listening_batch(conversation_id: str, batch: ListeningBatch) -> None:
        with SessionLocal.begin() as session:
            buffer = session.scalar(
                select(CompanionListeningBuffer).where(
                    CompanionListeningBuffer.conversation_id == conversation_id
                )
            )
            current = OneBotManager._buffer_fragments(buffer)
            merged = [*batch.fragments, *current]
            if buffer is None:
                session.add(
                    CompanionListeningBuffer(
                        conversation_id=conversation_id,
                        scope_id=batch.scope_id,
                        fragments=json.dumps(merged, ensure_ascii=False),
                    )
                )
            else:
                buffer.fragments = json.dumps(merged, ensure_ascii=False)

    @staticmethod
    def _message_was_persisted(message_id: str) -> bool:
        with SessionLocal() as session:
            return session.scalar(
                select(Message.id).where(Message.platform_message_id == message_id)
            ) is not None

    async def _finish_listening_batch(self, conversation_id: str) -> None:
        lock = self._listening_lock(conversation_id)
        async with lock:
            self._listening_processing.discard(conversation_id)
            event = self._listening_processing_done.pop(conversation_id, None)
            if event is not None:
                event.set()

    async def _cancel_listening(self, user_id: str, external_id: str) -> None:
        with SessionLocal() as session:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.platform == "qq", Conversation.external_id == external_id
                )
            )
        conversation_id = conversation.id if conversation is not None else None
        if conversation_id is None:
            with SessionLocal.begin() as session:
                preference = get_or_create_preference(session, user_id)
                preference.listening_enabled = False
            return
        lock = self._listening_lock(conversation_id)
        async with lock:
            pending = self._listening_tasks.pop(conversation_id, None)
            current_task = asyncio.current_task()
            if pending is not None and pending is not current_task:
                pending.cancel()
            attachment_metadata: list[dict[str, object]] = []
            with SessionLocal.begin() as session:
                preference = get_or_create_preference(session, user_id)
                preference.listening_enabled = False
                buffer = session.scalar(
                    select(CompanionListeningBuffer).where(
                        CompanionListeningBuffer.conversation_id == conversation_id
                    )
                )
                if buffer is not None:
                    attachment_metadata = self._buffer_attachment_metadata(buffer)
                    session.delete(buffer)
            delete_pending_attachments(attachment_metadata)

    def _schedule_listening_flush(
        self, conversation_id: str, user_id: str, external_id: str, seconds: int
    ) -> None:
        previous = self._listening_tasks.pop(conversation_id, None)
        if previous is not None:
            previous.cancel()

        async def delayed_flush() -> None:
            try:
                await asyncio.sleep(seconds)
                batch = await self._claim_listening_batch(conversation_id)
                if batch is not None:
                    try:
                        await self._process_listening_batch(
                            user_id, external_id, conversation_id, batch
                        )
                    finally:
                        await self._finish_listening_batch(conversation_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("倾听模式自动处理失败：conversation=%s", conversation_id)
            finally:
                current_task = asyncio.current_task()
                if self._listening_tasks.get(conversation_id) is current_task:
                    self._listening_tasks.pop(conversation_id, None)

        self._listening_tasks[conversation_id] = asyncio.create_task(
            delayed_flush(), name=f"listening-flush-{conversation_id}"
        )

    async def _flush_listening_now(
        self, user_id: str, external_id: str, *, disable_after: bool = False
    ) -> str:
        with SessionLocal() as session:
            conversation = session.scalar(
                select(Conversation).where(
                    Conversation.platform == "qq", Conversation.external_id == external_id
                )
            )
        conversation_id = conversation.id if conversation is not None else None
        if disable_after:
            # Disable before waiting for an in-flight batch, so fragments sent
            # after `/listening off` cannot join the batch being awaited.
            with SessionLocal.begin() as session:
                preference = get_or_create_preference(session, user_id)
                preference.listening_enabled = False
        if conversation_id:
            batch = await self._claim_listening_batch(conversation_id)
        else:
            batch = None
        if batch is not None:
            assert conversation_id is not None
            try:
                await self._process_listening_batch(user_id, external_id, conversation_id, batch)
            finally:
                await self._finish_listening_batch(conversation_id)
            return ""
        return "当前没有待处理的连续消息。"

    async def _process_listening_batch(
        self,
        user_id: str,
        external_id: str,
        conversation_id: str,
        batch: ListeningBatch,
    ) -> None:
        try:
            attachments = pending_attachment_inputs(batch.attachment_metadata)
        except Exception as error:
            logger.warning("倾听模式图片读取失败：%s", type(error).__name__)
            self._restore_listening_batch(conversation_id, batch)
            try:
                await self.send_text(user_id, "图片处理失败：倾听模式中的图片暂时不可读取。")
            except Exception:
                logger.exception("发送 QQ 倾听模式图片错误回复失败")
            return
        try:
            await self._run_chat_response(
                user_id,
                batch.text,
                batch.message_id,
                None,
                external_id,
                attachments,
            )
        except Exception as error:
            logger.exception("倾听模式批次处理失败：%s", type(error).__name__)
            if batch.attachment_metadata:
                self._restore_listening_batch(conversation_id, batch)
            return
        if not batch.attachment_metadata or self._message_was_persisted(batch.message_id):
            delete_pending_attachments(batch.attachment_metadata)
        else:
            self._restore_listening_batch(conversation_id, batch)

    async def _handle_listening_input(
        self,
        text: str,
        user_id: str,
        external_id: str,
        message_id: str,
        attachments: tuple[ChatAttachmentInput, ...] = (),
    ) -> str | None:
        command = text.lower()
        natural_enable = any(
            phrase in text for phrase in ("你先听我说", "等我说完再回", "我说完你再回复")
        )
        natural_done = any(phrase in text for phrase in ("我说完了", "你可以说了", "现在可以回复了"))
        if command == "/listening" or command.startswith("/listening "):
            return await self._handle_listening_command(command, user_id, external_id)
        if external_id.startswith("group:") or not is_owner(user_id):
            return None

        with SessionLocal() as session:
            preference = get_or_create_preference(session, user_id)
            listening_enabled = bool(preference.listening_enabled)
            companion_enabled = bool(preference.companion_enabled)
            silence_seconds = max(5, min(120, int(preference.listening_silence_seconds or 30)))
        if natural_enable and not listening_enabled:
            with SessionLocal.begin() as session:
                preference = get_or_create_preference(session, user_id)
                preference.listening_enabled = True
            return "好，你慢慢说。我等你说完再回应。"
        if not listening_enabled or not companion_enabled:
            return None
        if natural_done:
            return await self._flush_listening_now(user_id, external_id)
        # Safety remains immediate even when normal conversational fragments are buffered.
        if text and safety_precheck(text).risk_level in {"high", "critical"}:
            return None
        conversation_id = self._ensure_qq_conversation(external_id, user_id)
        pending_batch = None
        new_attachment_metadata: list[dict[str, object]] = []
        if attachments:
            try:
                pending_batch = stage_pending_attachments(attachments)
                with SessionLocal() as session:
                    existing = session.scalar(
                        select(CompanionListeningBuffer).where(
                            CompanionListeningBuffer.conversation_id == conversation_id
                        )
                    )
                    existing_metadata = self._buffer_attachment_metadata(existing)
                if len(existing_metadata) + len(attachments) > MAX_ATTACHMENTS:
                    raise OneBotMediaError(
                        f"倾听模式每轮最多处理 {MAX_ATTACHMENTS} 张图片"
                    )
                existing_bytes = sum(
                    int(str(item.get("byte_size") or 0)) for item in existing_metadata
                )
                new_bytes = sum(item.byte_size for item in pending_batch.items)
                if existing_bytes + new_bytes > MAX_TOTAL_BYTES:
                    raise OneBotMediaError("倾听模式每轮图片总大小不能超过 32 MiB")
                new_attachment_metadata = pending_batch.persist()
            except Exception:
                if pending_batch is not None:
                    pending_batch.cleanup()
                raise
        async with self._listening_lock(conversation_id):
            try:
                with SessionLocal.begin() as session:
                    buffer = session.scalar(
                        select(CompanionListeningBuffer).where(
                            CompanionListeningBuffer.conversation_id == conversation_id
                        )
                    )
                    fragments = self._buffer_fragments(buffer)
                    locked_metadata = self._buffer_attachment_metadata(buffer)
                    if new_attachment_metadata:
                        if len(locked_metadata) + len(new_attachment_metadata) > MAX_ATTACHMENTS:
                            raise OneBotMediaError(
                                f"倾听模式每轮最多处理 {MAX_ATTACHMENTS} 张图片"
                            )
                        locked_bytes = sum(
                            int(str(item.get("byte_size") or 0)) for item in locked_metadata
                        )
                        incoming_bytes = sum(
                            int(str(item.get("byte_size") or 0))
                            for item in new_attachment_metadata
                        )
                        if locked_bytes + incoming_bytes > MAX_TOTAL_BYTES:
                            raise OneBotMediaError("倾听模式每轮图片总大小不能超过 32 MiB")
                    fragment: dict[str, object] = {
                        "message_id": message_id,
                        "text": text,
                        "created_at": utc_isoformat(datetime.now(UTC)),
                    }
                    if new_attachment_metadata:
                        fragment["attachments"] = new_attachment_metadata
                    fragments.append(fragment)
                    if buffer is None:
                        buffer = CompanionListeningBuffer(
                            conversation_id=conversation_id,
                            scope_id=user_id,
                            fragments=json.dumps(fragments, ensure_ascii=False),
                        )
                        session.add(buffer)
                    else:
                        buffer.fragments = json.dumps(fragments, ensure_ascii=False)
            except Exception:
                if pending_batch is not None:
                    pending_batch.cleanup()
                raise
        self._schedule_listening_flush(conversation_id, user_id, external_id, silence_seconds)
        operation_logs.emit(
            source="companion",
            kind="listening_buffered",
            title="QQ 消息进入连续倾听缓冲",
            message=f"已暂存 {len(fragments)} 条片段，等待用户说完",
            details={"conversation_id": conversation_id, "fragment_count": len(fragments)},
        )
        return ""

    async def _handle_listening_command(
        self, command: str, user_id: str, external_id: str
    ) -> str:
        if external_id.startswith("group:"):
            return "‘你听我说’触发的倾听模式仅支持 QQ 管理员私聊。"
        if not is_owner(user_id):
            return "只有 QQ 管理员可以使用倾听模式。"
        action = command.removeprefix("/listening").strip().lower()
        if action == "on":
            with SessionLocal.begin() as session:
                preference = get_or_create_preference(session, user_id)
                preference.listening_enabled = True
            return (
                "已开启倾听模式。你可以分段发送，30 秒没有新消息或发送 "
                "/listening done 后我再回复。"
            )
        if action == "status":
            with SessionLocal() as session:
                preference = get_or_create_preference(session, user_id)
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq",
                        Conversation.external_id == external_id,
                    )
                )
                buffer = (
                    session.scalar(
                        select(CompanionListeningBuffer).where(
                            CompanionListeningBuffer.conversation_id == conversation.id
                        )
                    )
                    if conversation is not None
                    else None
                )
                return (
                    f"倾听模式：{'已开启' if preference.listening_enabled else '已关闭'}；"
                    f"待处理片段：{self._buffer_count(buffer)}。"
                )
        if action == "cancel":
            await self._cancel_listening(user_id, external_id)
            return "已取消当前连续消息，并关闭倾听模式。"
        if action in {"done", "off"}:
            return await self._flush_listening_now(
                user_id,
                external_id,
                disable_after=action == "off",
            )
        return "用法：/listening on|done|off|cancel|status"

    def _extract_triggered_text(self, raw: str, message_type: str) -> str | None:
        if message_type != "group":
            return raw
        at_pattern = rf"\[CQ:at,qq={re.escape(self.self_id or '')}[^\]]*\]"
        mentioned = bool(self.self_id and re.search(at_pattern, raw))
        prefixed = raw.startswith(settings.group_command_prefix)
        if not mentioned and not prefixed:
            return None
        value = re.sub(at_pattern, "", raw).strip()
        if value.startswith(settings.group_command_prefix):
            value = value[len(settings.group_command_prefix) :].strip()
        return value

    @staticmethod
    def _expand_manual_extension_request(text: str) -> str:
        for prefix, label in (("/skill ", "Skill"), ("/tool ", "Tool")):
            if not text.startswith(prefix):
                continue
            rest = text[len(prefix) :].strip()
            if not rest or rest.startswith(("install ", "enable ", "disable ", "remove ")):
                return text
            name, separator, request = rest.partition(" ")
            if separator and request.strip():
                return f"请明确调用 {label} {name}，完成以下请求：{request.strip()}"
        return text

    async def _command(self, text: str, user_id: str, external_id: str) -> str | None:
        if text == "/help":
            return (
                "可用命令：\n\n"
                "【会话与模型】\n"
                "/new\n"
                "/reset-context\n"
                "/context\n"
                "/model list\n"
                "/model use <alias>\n\n"
                "【知识库与记忆】\n"
                "/kb\n"
                "/memory\n"
                "/memory delete <id>\n\n"
                "【人格】\n"
                "/persona\n"
                "/persona list\n"
                "/persona use <名称或ID>\n"
                "/persona off\n\n"
                "【情感陪伴（仅 Owner 私聊）】\n"
                "/support auto|listen|reflect|advice\n"
                "/emotion\n"
                "/emotion correct <情绪>\n"
                "/feedback helpful|unhelpful|no-advice\n"
                 "/companion pause|resume\n"
                 "/listening on|done|off|cancel|status\n"
                "/companion memory on|off\n\n"
                "/companion safety standard\n"
                "/companion safety unfiltered confirm\n"
                "/companion status\n\n"
                "【Tools 与 Skills】\n"
                "/tools\n"
                "/skills\n"
                "/skill <name> <请求>\n\n"
                "【漫画】\n"
                "/jm <关键词>\n"
                "/jm download <漫画ID>\n"
                "/jm delete <任务ID>\n\n"
                "【确认】\n"
                "/confirm <token>\n"
                 "/cancel <token>"
            )
        if text == "/listening" or text.startswith("/listening "):
            return await self._handle_listening_command(text, user_id, external_id)
        if text == "/support" or text.startswith("/support "):
            if external_id.startswith("group:"):
                return "情感陪伴仅支持 QQ 私聊，群聊不会加载私人陪伴资料。"
            if not is_owner(user_id):
                return "只有 Owner 可以使用情感陪伴。"
            mode = text.removeprefix("/support").strip()
            if mode not in {"auto", "listen", "reflect", "advice"}:
                return "用法：/support auto|listen|reflect|advice"
            with SessionLocal.begin() as session:
                preference = get_or_create_preference(session, user_id)
                preference.support_mode = mode
            mode_labels = {
                "auto": "自动判断",
                "listen": "倾听",
                "reflect": "一起梳理",
                "advice": "建议",
            }
            return f"本次及后续 QQ 私聊支持方式已设为：{mode_labels[mode]}。"
        if text == "/emotion" or text.startswith("/emotion correct"):
            if external_id.startswith("group:"):
                return "情感陪伴仅支持 QQ 私聊，群聊没有个人情绪记录。"
            if not is_owner(user_id):
                return "只有 Owner 可以查看情绪分析。"
            correcting = text.startswith("/emotion correct")
            labels: list[str] = []
            invalid_labels: list[str] = []
            if correcting:
                raw_labels = text.removeprefix("/emotion correct").strip()
                labels, invalid_labels = parse_emotion_labels_strict(raw_labels)
                if invalid_labels or len(labels) > 3 or not labels:
                    options = "、".join(EMOTION_LABEL_ZH.values())
                    return f"情绪标签无效。请选择 1-3 个合法标签：{options}"
            with SessionLocal.begin() as session:
                assessment = session.scalar(
                    select(EmotionAssessment)
                    .where(EmotionAssessment.scope_id == user_id)
                    .order_by(desc(EmotionAssessment.created_at))
                    .limit(1)
                )
                if assessment is None:
                    return "还没有可查看的情绪分析记录。"
                if correcting:
                    assessment.correction = json.dumps(
                        {"emotions": labels, "corrected_at": utc_isoformat(datetime.now(UTC))},
                        ensure_ascii=False,
                    )
                    return "已记录你的情绪纠正：" + "、".join(
                        EMOTION_LABEL_ZH.get(label, label) for label in labels
                    )
                view = assessment_dict(assessment, session)
                analysis_status = str(view["analysis_status"])
                corrected_emotions = view.get("effective_candidate_emotions")
                has_correction = bool(
                    isinstance(corrected_emotions, list)
                    and corrected_emotions
                    and view.get("correction")
                )
                if analysis_status == "retrying" and not has_correction:
                    return "最近一次情绪分析正在重试，暂时没有可靠结果。"
                if analysis_status == "safety_redirected" and not has_correction:
                    return (
                        "最近一次消息触发了安全转向，未执行情绪分类；"
                        "如需纠正请使用 /emotion correct。"
                    )
                if analysis_status == "failed" and not has_correction:
                    return (
                        "最近一次情绪分析失败，暂时无法可靠判断；"
                        "你可以使用 /emotion correct 进行纠正。"
                    )
                emotions = corrected_emotions if isinstance(corrected_emotions, list) else []
                display = "、".join(
                    EMOTION_LABEL_ZH.get(str(item), str(item)) for item in emotions
                )
                support_need_value = view.get("effective_support_need")
                support_need = (
                    SUPPORT_NEED_ZH.get(str(support_need_value), "尚不确定")
                    if isinstance(support_need_value, str)
                    else "尚不确定"
                )
                confidence = view.get("confidence")
                confidence_text = (
                    f"{confidence:.0%}"
                    if isinstance(confidence, (int, float))
                    else "—"
                )
                status_note = "；已采用你的纠正" if has_correction else ""
                return (
                    f"最近候选情绪：{display or '暂不确定'}；"
                    f"支持需要：{support_need}；"
                    f"置信度：{confidence_text}；"
                    f"当前策略：{assessment.next_action}{status_note}。"
                )
        if text == "/feedback" or text.startswith("/feedback "):
            if external_id.startswith("group:"):
                return "情感陪伴反馈仅支持 QQ 私聊。"
            if not is_owner(user_id):
                return "只有 Owner 可以提交陪伴反馈。"
            value = text.removeprefix("/feedback").strip()
            if value not in {"helpful", "unhelpful", "no-advice"}:
                return "用法：/feedback helpful|unhelpful|no-advice"
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is None:
                    return "当前还没有可反馈的 QQ 会话。"
                assistant = session.scalar(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation.id,
                        Message.role == "assistant",
                    )
                    .order_by(desc(Message.created_at))
                    .limit(1)
                )
                if assistant is None:
                    return "当前还没有可反馈的回复。"
                feedback = session.scalar(
                    select(ResponseFeedback).where(
                        ResponseFeedback.assistant_message_id == assistant.id
                    )
                )
                if feedback is None:
                    feedback = ResponseFeedback(
                        assistant_message_id=assistant.id,
                        scope_id=user_id,
                        feedback=value,
                    )
                    session.add(feedback)
                else:
                    feedback.feedback = value
                return "已记录本条回复反馈：" + value
        if text == "/companion" or text.startswith("/companion "):
            if external_id.startswith("group:"):
                return "情感陪伴仅支持 QQ 私聊，群聊不会启用。"
            if not is_owner(user_id):
                return "只有 Owner 可以管理情感陪伴。"
            action = text.removeprefix("/companion").strip()
            allowed_actions = {
                "pause",
                "resume",
                "memory on",
                "memory off",
                "safety standard",
                "safety unfiltered confirm",
                "status",
            }
            if action == "safety unfiltered":
                return "无过滤模式风险较高；如确认，请发送：/companion safety unfiltered confirm"
            if action not in allowed_actions:
                return (
                    "用法：/companion pause|resume|memory on|memory off；"
                    "/companion safety standard；"
                    "/companion safety unfiltered confirm；/companion status"
                )
            with SessionLocal.begin() as session:
                preference = get_or_create_preference(session, user_id)
                if action == "pause":
                    preference.companion_enabled = False
                    return "已暂停情感陪伴，后续私聊将回到通用 Agent。"
                if action == "resume":
                    preference.companion_enabled = True
                    return "已恢复情感陪伴。"
                if action == "status":
                    safety_mode = getattr(preference, "safety_mode", "standard") or "standard"
                    return (
                        f"情感陪伴：{'已启用' if preference.companion_enabled else '已暂停'}；"
                        f"关系记忆：{'已授权' if preference.memory_enabled else '未授权'}；"
                        "安全模式："
                        f"{'无过滤（仅关闭本机陪伴拦截）' if safety_mode == 'unfiltered' else '标准防护'}。"
                    )
                if action == "safety standard":
                    preference.safety_mode = "standard"
                    return "已切回标准防护模式。"
                if action == "safety unfiltered confirm":
                    preference.safety_mode = "unfiltered"
                    return (
                        "已启用无过滤模式（仅当前 Owner）。本机仍保留 Owner 权限、Tool 确认、"
                        "文件隔离和密钥脱敏；模型服务商仍可能自行拒答。"
                        "可用 /companion safety standard 关闭。"
                    )
                preference.memory_enabled = action == "memory on"
                return (
                    "已授权关系记忆；后续只保存低敏感、明确表达的关系事实。"
                    if preference.memory_enabled
                    else "已关闭记忆；后续陪伴私聊不召回或写入长期记忆。"
                )
        if text in {"/new", "/reset-context"}:
            if external_id.startswith("group:") and not is_owner(user_id):
                return "只有 Owner 可以轮换群聊上下文。"
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is None:
                    conversation = Conversation(
                        platform="qq",
                        external_id=external_id,
                        conversation_type="group" if external_id.startswith("group:") else "private",
                        title=f"QQ {external_id}",
                        model_alias=model_registry.default_alias(),
                        owner_id=user_id,
                    )
                    session.add(conversation)
                    return "已创建新的 QQ 会话，长期记忆保持不变。"
                conversation.external_id = f"archive:{conversation.id}"
                conversation.title = f"{conversation.title}（已归档）"
                new_conversation = Conversation(
                    platform="qq",
                    external_id=external_id,
                    conversation_type=conversation.conversation_type,
                    title=f"QQ {external_id}",
                    model_alias=conversation.model_alias,
                    persona_id=conversation.persona_id,
                    owner_id=user_id,
                )
                session.add(new_conversation)
            return "已开启新的 QQ 会话，旧消息已归档，长期记忆保持不变。"
        if text == "/context":
            with SessionLocal() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is None:
                    return "当前还没有 QQ 会话。"
                pending = load_pending_messages(session, conversation)
                history_text = "\n".join(f"{item.role}: {item.content}" for item in pending)
                history_tokens = estimate_text_tokens(history_text)
                summary_tokens = estimate_text_tokens(conversation.summary or "")
                profile = model_registry.profile(conversation.model_alias)
                return (
                    f"当前会话：{conversation.id}\n"
                    f"待摘要消息：{pending_message_count(session, conversation)} 条\n"
                    f"历史摘要：{'已生成' if conversation.summary else '无'}（约 {summary_tokens} Token）\n"
                    f"近期历史估算：约 {history_tokens} Token\n"
                    f"输入软上限：{profile.input_soft_limit} Token\n"
                    f"上下文硬上限：{profile.context_window} Token\n"
                    "长期记忆不会因会话轮换而删除。"
                )
        if text in {"/persona", "/persona list"}:
            with SessionLocal.begin() as session:
                entries = get_persona_store().sync_db(session)
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                current_id = conversation.persona_id if conversation else None
                personas = sorted(
                    (entry for entry in entries.values() if entry.is_active),
                    key=lambda entry: entry.name.casefold(),
                )
                lines = [f"当前人格：{current_id or '关闭'}"]
                lines.extend(f"- {item.name} ({item.id})" for item in personas)
                return "可用人格：\n" + ("\n".join(lines) if personas else "暂无已保存人格")
        if text == "/persona off" or text.startswith("/persona use "):
            if external_id.startswith("group:") and not is_owner(user_id):
                return "只有 Owner 可以切换群聊人格。"
            requested = None if text == "/persona off" else text.removeprefix("/persona use ").strip()
            if text != "/persona off" and not requested:
                return "用法：/persona use <人格名称或ID>，或 /persona off。"
            with SessionLocal.begin() as session:
                entries = get_persona_store().sync_db(session)
                persona = None
                if requested is not None:
                    persona = next(
                        (
                            entry
                            for entry in entries.values()
                            if entry.is_active and (entry.id == requested or entry.name == requested)
                        ),
                        None,
                    )
                    if persona is None:
                        return "找不到该人格，请先使用 /persona list 查看。"
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is None:
                    conversation = Conversation(
                        platform="qq",
                        external_id=external_id,
                        conversation_type="group" if external_id.startswith("group:") else "private",
                        title=f"QQ {external_id}",
                        model_alias=model_registry.default_alias(),
                        owner_id=user_id,
                    )
                    session.add(conversation)
                    session.flush()
                if requested is not None:
                    assert persona is not None
                    conversation.persona_id = persona.id
                    return f"当前 QQ 会话已启用人格：{persona.name}。"
                conversation.persona_id = None
                return "当前 QQ 会话已关闭人格。"
        if text == "/model list":
            return "可用模型：\n" + "\n".join(
                f"- {item['alias']} ({'已配置' if item['configured'] else '未配置'})"
                for item in model_registry.list()
            )
        if text.startswith("/model use "):
            if not is_owner(user_id):
                return "只有 Owner 可以切换模型。"
            alias = text.removeprefix("/model use ").strip()
            model_registry.profile(alias)
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is None:
                    conversation = Conversation(
                        platform="qq",
                        external_id=external_id,
                        conversation_type="group" if external_id.startswith("group:") else "private",
                        title=f"QQ {external_id}",
                        model_alias=model_registry.default_alias(),
                        owner_id=user_id,
                    )
                    session.add(conversation)
                conversation.model_alias = alias
            return f"已将当前 QQ 会话模型切换为 {alias}。"
        if text == "/kb":
            with SessionLocal() as session:
                items = session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name)).all()
                return "知识库：\n" + ("\n".join(f"- {item.name}: {item.id}" for item in items) or "暂无")
        if text == "/memory":
            scope_type = "group" if external_id.startswith("group:") else "user"
            scope_id = external_id if scope_type == "group" else user_id
            with SessionLocal() as session:
                items = session.scalars(
                    select(Memory).where(
                        Memory.scope_type == scope_type,
                        Memory.user_id == scope_id,
                        Memory.status == "active",
                    )
                ).all()
                return "当前记忆：\n" + (
                    "\n".join(f"- {item.id}: {item.content}" for item in items) or "暂无"
                )
        if text.startswith("/memory delete "):
            memory_id = text.removeprefix("/memory delete ").strip()
            scope_type = "group" if external_id.startswith("group:") else "user"
            scope_id = external_id if scope_type == "group" else user_id
            if scope_type == "group" and not is_owner(user_id):
                return "只有 Owner 可以删除群聊记忆。"
            with SessionLocal() as session:
                item = session.get(Memory, memory_id)
                if item is None or item.scope_type != scope_type or item.user_id != scope_id:
                    return "找不到属于你的这条记忆。"
                MemoryService(session).archive(item.id)
            return "已删除指定记忆。"
        if text == "/tools":
            with SessionLocal() as session:
                items = session.scalars(
                    select(ExtensionPackage).where(ExtensionPackage.kind == "tool")
                ).all()
                return "Tools：\n" + (
                    "\n".join(
                        f"- {item.name} ({'启用' if item.enabled else '停用'})：{item.description}"
                        for item in items
                    )
                    or "暂无"
                )
        if text == "/skills":
            with SessionLocal() as session:
                items = session.scalars(
                    select(ExtensionPackage).where(ExtensionPackage.kind == "skill")
                ).all()
                return "Skills：\n" + (
                    "\n".join(
                        f"- {item.name} ({'启用' if item.enabled else '停用'})：{item.description}"
                        for item in items
                    )
                    or "暂无"
                )
        management_prefixes = (
            "/skill install ",
            "/skill enable ",
            "/skill disable ",
            "/skill remove ",
        )
        if text.startswith(management_prefixes):
            if not is_owner(user_id) or external_id.startswith("group:"):
                return "只有 Owner 私聊可以管理 Skill。"
            operation, _, name_or_url = text.removeprefix("/skill ").partition(" ")
            confirmation = create_extension_confirmation(
                "skill", operation, name_or_url.strip(), user_id, None
            )
            return f"Skill 操作待确认：/confirm {confirmation.token}（十分钟内有效）"
        tool_management_prefixes = (
            "/tool install ",
            "/tool enable ",
            "/tool disable ",
            "/tool remove ",
        )
        if text.startswith(tool_management_prefixes):
            if not is_owner(user_id) or external_id.startswith("group:"):
                return "只有 Owner 私聊可以管理 Tool。"
            operation, _, name_or_url = text.removeprefix("/tool ").partition(" ")
            confirmation = create_extension_confirmation(
                "tool", operation, name_or_url.strip(), user_id, None
            )
            return f"Tool 操作待确认：/confirm {confirmation.token}（十分钟内有效）"
        if text.startswith("/jm download "):
            if external_id.startswith("group:"):
                return "群聊禁止创建漫画下载任务。"
            album_id = text.removeprefix("/jm download ").strip()
            try:
                job = create_manga_download_job(album_id, user_id, None)
            except (PermissionError, ValueError) as error:
                return str(error)
            return (
                f"已创建下载任务：{job.id}。下载成功后会自动发送文件；"
                f"发送完成后可用 /jm delete {job.id} 删除本地产物。"
            )
        if text.startswith("/jm delete "):
            if external_id.startswith("group:"):
                return "群聊禁止删除漫画产物。"
            job_id = text.removeprefix("/jm delete ").strip()
            try:
                result = delete_manga_artifact(job_id, user_id)
            except (PermissionError, ValueError, OSError) as error:
                return str(error)
            return f"已删除漫画任务 {result['job_id']} 的本地产物，任务审计记录已保留。"
        if text.startswith("/jm "):
            results = await asyncio.wait_for(
                asyncio.to_thread(manga_service.search, text.removeprefix("/jm ").strip()),
                timeout=45,
            )
            return "搜索结果：\n" + "\n".join(
                f"- JM{item['album_id']} {item['title']}" for item in results[:10]
            )
        if text.startswith(("/confirm ", "/cancel ")):
            approve = text.startswith("/confirm ")
            token = text.split(maxsplit=1)[1].strip()
            confirmation, job = resolve_confirmation(token, user_id, approve)
            if job:
                return f"已创建下载任务：{job.id}"
            if confirmation.action == "extension_manage" and confirmation.status == "approved":
                return "已执行扩展管理操作。"
            return "已取消该确认请求。"
        return None

    async def notify_job(self, job: Job) -> None:
        if job.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE:
            if job.status == "succeeded" and job.result:
                result = json.loads(job.result)
                risk_level = result.get("risk_level")
                safety_mode = result.get("safety_mode", "standard")
                next_action = result.get("next_action")
                if (
                    safety_mode == "standard"
                    and risk_level in {"high", "critical"}
                    and next_action == "safety_redirect"
                    and job.requester_id.isdigit()
                    and is_owner(job.requester_id)
                ):
                    await self.send_text(
                        job.requester_id,
                        safety_redirect_text("critical" if risk_level == "critical" else "high"),
                    )
            return
        if not job.requester_id.isdigit() or not is_owner(job.requester_id):
            return
        if job.status != "succeeded" or not job.result:
            await self.send_text(job.requester_id, f"任务 {job.id} {job.status}：{job.error or '无详细信息'}")
            return
        result = json.loads(job.result)
        path = Path(str(result.get("path", "")))
        if not path.is_file():
            await self.send_text(job.requester_id, f"任务完成，但产物不存在：{path}")
            return
        limit = settings.qq_upload_limit_mb * 1024 * 1024
        if path.stat().st_size > limit:
            update_job_result(
                job.id,
                {
                    "delivery_status": "skipped_oversize",
                    "delivery_error": "文件超过 QQ 上传阈值",
                },
            )
            await self.send_text(job.requester_id, f"任务完成，文件超过上传阈值：{path.resolve()}")
            return
        try:
            await self.send_private_file(job.requester_id, path)
            update_job_result(
                job.id,
                {
                    "delivery_status": "sent",
                    "delivered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "delivery_error": None,
                },
            )
            await self.send_text(
                job.requester_id,
                f"文件已发送。确认收到后，如需清理本地文件请发送：/jm delete {job.id}",
            )
        except Exception as error:
            error_text = f"{type(error).__name__}: {error}".rstrip()
            update_job_result(
                job.id,
                {"delivery_status": "failed", "delivery_error": error_text},
            )
            await self.send_text(
                job.requester_id,
                f"QQ 文件发送失败：{error_text}\n本地路径：{path.resolve()}",
            )


onebot_manager = OneBotManager()
job_worker.notifier = onebot_manager.notify_job


@router.websocket("/onebot/ws")
async def onebot_websocket(websocket: WebSocket) -> None:
    await onebot_manager.serve(websocket)
