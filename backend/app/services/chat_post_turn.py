from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session

from app.db.models import Conversation
from app.db.session import SessionLocal
from app.services.companion import extract_relationship
from app.services.context import (
    SUMMARY_INPUT_TOKENS,
    SUMMARY_KEEP_MESSAGES,
    SUMMARY_MAX_TOKENS,
    choose_summary_batch,
    clip_text,
    load_pending_messages,
    pending_message_count,
    pending_prefix,
    should_compact,
)
from app.services.memories import MemoryService
from app.services.models import model_registry
from app.services.operation_logs import operation_logs
from app.services.time_context import current_time_context, format_messages_for_model

logger = logging.getLogger(__name__)


class PostTurnService:
    """Runs memory extraction and incremental summarization after a reply."""

    def __init__(self) -> None:
        self._summary_locks: dict[str, threading.Lock] = {}
        self._summary_locks_guard = threading.Lock()

    def _summary_lock(self, conversation_id: str) -> threading.Lock:
        with self._summary_locks_guard:
            return self._summary_locks.setdefault(conversation_id, threading.Lock())

    def run(
        self,
        conversation_id: str,
        sender_id: str,
        user_text: str,
        message_id: str,
        model_alias: str,
        is_group: bool,
        companion_enabled: bool = False,
        memory_enabled: bool = False,
        persona_id: str | None = None,
    ) -> None:
        with SessionLocal() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return
            context_rows = load_pending_messages(session, conversation, limit=8)
            time_context = current_time_context()
            turn_context = format_messages_for_model(context_rows[:-1])
            memory_scope_type = "group" if is_group else "user"
            memory_scope_id = conversation.external_id if is_group else sender_id
            memory_operation: str | None = None
            try:
                if companion_enabled and not memory_enabled:
                    with self._summary_lock(conversation_id):
                        self.compact(session, conversation, model_alias)
                    return
                memory_operation = operation_logs.start_operation(
                    source="memory",
                    title="自动提取记忆",
                    message="开始从对话提取长期记忆",
                    details={"conversation_id": conversation_id, "message_id": message_id},
                )
                if companion_enabled:
                    extract_relationship(
                        session,
                        sender_id,
                        persona_id,
                        user_text,
                        turn_context,
                        model_alias,
                        time_context=time_context,
                    )
                else:
                    MemoryService(session).extract_from_turn(
                        sender_id,
                        user_text,
                        message_id,
                        model_alias,
                        scope_type=memory_scope_type,
                        scope_id=memory_scope_id,
                        context=turn_context,
                        time_context=time_context,
                    )
                operation_logs.finish_operation(
                    memory_operation,
                    source="memory",
                    title="自动提取记忆完成",
                    message="长期记忆提取已完成",
                    details={"conversation_id": conversation_id},
                )
            except Exception as error:
                session.rollback()
                logger.warning("自动记忆提取失败: %s", error)
                if memory_operation is not None:
                    operation_logs.finish_operation(
                        memory_operation,
                        source="memory",
                        title="自动提取记忆失败",
                        message="长期记忆提取失败",
                        success=False,
                        details={"error": f"{type(error).__name__}: {error}"},
                    )
            with self._summary_lock(conversation_id):
                self.compact(session, conversation, model_alias)

    def compact(
        self,
        session: Session,
        conversation: Conversation,
        model_alias: str,
    ) -> None:
        pending = pending_prefix(session, conversation)
        if not should_compact(session, conversation, pending=pending):
            return
        pending_count = pending_message_count(session, conversation)
        if pending_count <= SUMMARY_KEEP_MESSAGES:
            return
        candidate_rows = pending[: min(len(pending), pending_count - SUMMARY_KEEP_MESSAGES)]
        existing_summary = clip_text(conversation.summary or "", SUMMARY_MAX_TOKENS)
        batch = choose_summary_batch(
            candidate_rows,
            existing_summary,
            max_tokens=SUMMARY_INPUT_TOKENS,
        )
        if not batch:
            return
        time_context = current_time_context()
        new_material = format_messages_for_model(batch)
        summary_source = (
            f"当前时间上下文：\n{time_context}\n\n"
            f"已有摘要：\n{existing_summary or '无'}\n\n"
            f"新增历史（按每条消息前缀的原始时间解释相对日期）：\n{new_material}"
        )
        summary_operation: str | None = None
        try:
            summary_operation = operation_logs.start_operation(
                source="memory",
                title="会话摘要压缩",
                message="开始压缩历史摘要",
                details={"conversation_id": conversation.id},
            )
            model = model_registry.chat_model(model_alias).bind(max_tokens=SUMMARY_MAX_TOKENS)
            response = model.invoke(
                [
                    (
                        "system",
                        (
                            "将已有摘要和新增历史合并为准确、简短的中文摘要。"
                            "保留用户明确事实、未完成任务和重要决定，不添加新事实。"
                            "必须依据消息时间前缀将今天、昨天、昨晚、刚才等相对时间还原为绝对日期；"
                            "前阵子、以前等模糊表达保留原文并保留记录时间锚点。"
                        ),
                    ),
                    ("human", summary_source),
                ]
            )
            if isinstance(response.content, str) and response.content.strip():
                conversation.summary = clip_text(response.content.strip(), SUMMARY_MAX_TOKENS)
                conversation.summary_up_to_message_id = batch[-1].id
                session.commit()
                operation_logs.finish_operation(
                    summary_operation,
                    source="memory",
                    title="会话摘要完成",
                    message="历史摘要已更新",
                    details={
                        "conversation_id": conversation.id,
                        "message_id": conversation.summary_up_to_message_id,
                    },
                )
            else:
                operation_logs.finish_operation(
                    summary_operation,
                    source="memory",
                    title="会话摘要跳过",
                    message="模型未返回有效摘要",
                    details={"conversation_id": conversation.id},
                )
        except Exception as error:
            session.rollback()
            logger.warning("增量历史摘要失败: %s", error)
            if summary_operation is not None:
                operation_logs.finish_operation(
                    summary_operation,
                    source="memory",
                    title="会话摘要失败",
                    message="历史摘要更新失败",
                    success=False,
                    details={"error": f"{type(error).__name__}: {error}"},
                )
