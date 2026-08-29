from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncGenerator
from typing import cast

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import MessagesState
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import (
    CompanionPreference,
    Conversation,
    EmotionAssessment,
    KnowledgeBase,
    Message,
    SafetyEvent,
    ToolRun,
)
from app.db.session import SessionLocal
from app.services.companion import (
    EMOTION_LABEL_ZH,
    SAFETY_RESPONSE_PROMPT_VERSION,
    SAFETY_REWRITE_PROMPT_VERSION,
    STRATEGY_ZH,
    SUPPORT_NEED_ZH,
    CompanionAnalysis,
    SafetyAssessment,
    SafetyMode,
    SupportMode,
    analyze_message,
    detect_support_mode,
    extract_relationship,
    generate_safety_response,
    get_or_create_preference,
    get_relationship,
    output_safety_violations,
    relationship_context,
    resolve_analyzer_alias,
    rewrite_blocked_response,
    safety_precheck,
    safety_redirect_text,
)
from app.services.context import (
    SUMMARY_INPUT_TOKENS,
    SUMMARY_KEEP_MESSAGES,
    SUMMARY_MAX_TOKENS,
    ContextOverflowError,
    build_context_messages,
    choose_summary_batch,
    clip_text,
    load_pending_messages,
    pending_message_count,
    pending_prefix,
    should_compact,
)
from app.services.extensions import list_packages
from app.services.jobs import create_companion_analysis_retry_job
from app.services.manga_intent import MangaIntent, detect_manga_intent
from app.services.memories import MemoryService
from app.services.models import model_registry
from app.services.operation_logs import operation_logs
from app.services.personas import active_persona, persona_system_prompt
from app.services.runtime import is_owner
from app.workflows.agent import build_agent_graph, final_ai_message, skill_descriptions

logger = logging.getLogger(__name__)
SYSTEM_PROMPT = """你是 PersonalAgent，一个本地个人助理。
优先使用已经提供的用户画像和长期记忆，但不要把它们当作当前用户刚说的话。
文档问题需要使用 search_knowledge，并在回答中写明文件、标题和页码或位置。
QQ 私聊发起的漫画任务成功后，系统会自动把文件发送给 Owner；不要声称没有文件发送能力。
只有 Owner 明确要求删除某个已发送任务时，才能调用 delete_manga_download；不要自行清理产物。
不要声称工具成功，除非工具结果明确表示成功。"""


def normalize_tool_result(content: object) -> dict[str, object]:
    """兼容 ToolMessage 的对象、数组、JSON 字符串和非 JSON 内容。"""

    metadata_keys = {"kind", "data", "pending_confirmation", "artifact_ids"}
    if isinstance(content, dict):
        value: object = content if metadata_keys.intersection(content) else {"data": content}
    elif isinstance(content, list):
        value = {"data": {"items": content}}
    elif isinstance(content, str):
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            decoded = {"raw": content}
        if isinstance(decoded, dict):
            value = (
                decoded
                if metadata_keys.intersection(decoded)
                else {"data": decoded}
            )
        elif isinstance(decoded, list):
            value = {"data": {"items": decoded}}
        else:
            value = {"data": {"value": decoded}}
    else:
        value = {"data": {"value": content}}
    result: dict[str, object] = dict(value) if isinstance(value, dict) else {"data": value}
    result.setdefault("kind", "tool_result")
    result.setdefault("data", {})
    result.setdefault("pending_confirmation", False)
    result.setdefault("artifact_ids", [])
    return result


def _has_immediate_search_results(
    session: Session,
    conversation: Conversation,
    current_message_id: str,
) -> bool:
    """只认可上一轮自然语言聊天产生的非空漫画搜索结果。"""

    current_message = session.get(Message, current_message_id)
    if current_message is None:
        return False
    prior_user = session.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.role == "user",
            Message.id != current_message_id,
            Message.created_at < current_message.created_at,
        )
        .order_by(desc(Message.created_at))
        .limit(1)
    )
    if prior_user is None:
        return False
    prior_assistant = session.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.role == "assistant",
            Message.created_at >= prior_user.created_at,
            Message.created_at < current_message.created_at,
        )
        .order_by(desc(Message.created_at))
        .limit(1)
    )
    if prior_assistant is None:
        return False
    runs = session.scalars(
        select(ToolRun)
        .where(
            ToolRun.conversation_id == conversation.id,
            ToolRun.tool_name == "search_manga",
            ToolRun.status == "succeeded",
            ToolRun.created_at >= prior_user.created_at,
            ToolRun.created_at <= prior_assistant.created_at,
        )
        .order_by(desc(ToolRun.created_at))
    )
    for run in runs:
        payload = normalize_tool_result(run.result)
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("results"), list) and data["results"]:
            return True
    return False


def _manga_system_instruction(intent: MangaIntent) -> str:
    if not intent.actions:
        return (
            "\n\n漫画工具默认关闭。本轮没有通过明确漫画意图门禁时，不得调用任何漫画工具；"
            "不要把普通词语、话题或能力询问当成漫画搜索。"
        )
    action_names = {
        "search": "搜索",
        "download": "下载",
        "delete": "删除",
    }
    allowed = "、".join(action_names[action] for action in sorted(intent.actions))
    return (
        f"\n\n本轮程序已确认用户明确请求漫画{allowed}，只能在完成参数提取后调用对应漫画工具；"
        "不得额外调用未授权的漫画动作。"
    )


class ChatService:
    def __init__(self) -> None:
        self._summary_locks: dict[str, threading.Lock] = {}
        self._summary_locks_guard = threading.Lock()

    def _summary_lock(self, conversation_id: str) -> threading.Lock:
        with self._summary_locks_guard:
            return self._summary_locks.setdefault(conversation_id, threading.Lock())

    @staticmethod
    def _companion_scope(
        session: Session,
        sender_id: str,
        platform: str,
        is_group: bool,
    ) -> tuple[CompanionPreference | None, bool]:
        """Return the Owner-only private companion preference for this turn."""

        if platform != "qq" or is_group or not is_owner(sender_id):
            return None, False
        preference = get_or_create_preference(session, sender_id)
        return preference, bool(preference.companion_enabled)

    @staticmethod
    def _save_companion_assessment(
        session: Session,
        user_message_id: str,
        scope_id: str,
        analysis: CompanionAnalysis,
    ) -> EmotionAssessment:
        item = EmotionAssessment(
            user_message_id=user_message_id,
            scope_id=scope_id,
            candidate_emotions=json.dumps(analysis.candidate_emotions, ensure_ascii=False),
            primary_emotion=analysis.primary_emotion,
            intensity=analysis.intensity,
            support_need=analysis.support_need,
            confidence=analysis.confidence,
            risk_level=analysis.risk_level,
            next_action=analysis.next_action,
            model_alias=analysis.model_alias,
            prompt_version=analysis.prompt_version,
            classifier_version=analysis.classifier_version,
            schema_valid=analysis.schema_valid,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        analysis.assessment_id = item.id
        return item

    @staticmethod
    def _companion_analysis_event(
        analysis: CompanionAnalysis, safety_mode: SafetyMode = "standard"
    ) -> dict[str, object]:
        safety_intercepted = safety_mode == "standard" and analysis.next_action == "safety_redirect"
        visible_status: str = "safety_redirected" if safety_intercepted else analysis.analysis_status
        analysis_valid = (
            visible_status == "valid"
            and analysis.schema_valid
            and not safety_intercepted
        )
        return {
            "assessment_id": analysis.assessment_id,
            "candidate_emotions": analysis.candidate_emotions if analysis_valid else [],
            "candidate_emotions_display": [
                EMOTION_LABEL_ZH.get(label, label) for label in analysis.candidate_emotions
            ] if analysis_valid else [],
            "primary_emotion": analysis.primary_emotion if analysis_valid else None,
            "primary_emotion_display": EMOTION_LABEL_ZH.get(
                analysis.primary_emotion, analysis.primary_emotion
            ) if analysis_valid else None,
            "intensity": analysis.intensity if analysis_valid else None,
            "support_need": analysis.support_need if analysis_valid else None,
            "support_need_display": SUPPORT_NEED_ZH.get(analysis.support_need, analysis.support_need)
            if analysis_valid
            else None,
            "confidence": analysis.confidence if analysis_valid else None,
            "risk_level": analysis.risk_level,
            "next_action": analysis.next_action,
            "next_action_display": STRATEGY_ZH.get(analysis.next_action, analysis.next_action),
            "requires_confirmation": analysis.next_action == "clarify" or not analysis_valid,
            "schema_valid": analysis.schema_valid,
            "analysis_status": visible_status,
            "safety_mode": safety_mode,
            "safety_intercepted": safety_intercepted,
        }

    async def stream(
        self,
        session: Session,
        conversation: Conversation,
        sender_id: str,
        text: str,
        platform_message_id: str | None = None,
        platform: str = "web",
        is_group: bool = False,
    ) -> AsyncGenerator[dict[str, object]]:
        user_message = Message(
            conversation_id=conversation.id,
            sender_id=sender_id,
            role="user",
            content=text,
            platform_message_id=platform_message_id,
        )
        session.add(user_message)
        session.commit()
        session.refresh(user_message)

        started_at = time.perf_counter()
        operation_id = operation_logs.start_operation(
            source="chat",
            title="对话请求",
            message=f"收到{platform.upper()}消息",
            details={
                "conversation_id": conversation.id,
                "platform": platform,
                "sender_id": sender_id,
                "message_id": user_message.id,
                "text": text,
            },
        )

        profile = model_registry.profile(conversation.model_alias)
        recent = load_pending_messages(session, conversation)
        prior_recent = [item for item in recent if item.id != user_message.id][-6:]
        companion_preference, companion_enabled = self._companion_scope(
            session, sender_id, platform, is_group
        )
        companion_analysis: CompanionAnalysis | None = None
        companion_safety = None
        companion_safety_mode: SafetyMode = "standard"
        if companion_enabled and companion_preference is not None:
            companion_safety_mode = cast(
                SafetyMode, getattr(companion_preference, "safety_mode", "standard") or "standard"
            )
            if companion_safety_mode not in {"standard", "unfiltered"}:
                companion_safety_mode = "standard"
            companion_safety = safety_precheck(text)
            forced_mode = detect_support_mode(text)
            if forced_mode is None and companion_preference.support_mode != "auto":
                forced_mode = cast(SupportMode, companion_preference.support_mode)
            analyzer_alias = resolve_analyzer_alias(
                companion_preference.analyzer_model_alias,
                conversation.model_alias,
            )
            companion_analysis = await asyncio.to_thread(
                analyze_message,
                text,
                "\n".join(f"{item.role}: {item.content}" for item in prior_recent),
                analyzer_alias,
                forced_mode=forced_mode,
                safety=companion_safety,
                safety_mode=companion_safety_mode,
            )
            assessment = self._save_companion_assessment(
                session, user_message.id, sender_id, companion_analysis
            )
            if not companion_analysis.schema_valid:
                try:
                    retry_job = create_companion_analysis_retry_job(
                        assessment_id=assessment.id,
                        user_message_id=user_message.id,
                        requester_id=sender_id,
                        conversation_id=conversation.id,
                        forced_mode=forced_mode,
                        analyzer_model_alias=analyzer_alias,
                        safety_mode=companion_safety_mode,
                    )
                    if retry_job is not None and retry_job.status in {"queued", "running"}:
                        companion_analysis.analysis_status = "retrying"
                except Exception:
                    logger.exception("创建陪伴分析后台重试任务失败: assessment=%s", assessment.id)
            if companion_analysis.risk_level != "low":
                audit_action = (
                    "unfiltered_passthrough"
                    if companion_safety_mode == "unfiltered"
                    else companion_analysis.next_action
                )
                session.add(
                    SafetyEvent(
                        message_id=user_message.id,
                        scope_id=sender_id,
                        risk_level=companion_analysis.risk_level,
                        action=audit_action,
                        details=json.dumps(
                            {
                                "rules": list(companion_safety.rules)
                                if companion_safety is not None
                                else [],
                                "source": "precheck"
                                if companion_safety is not None
                                and companion_safety.risk_level != "low"
                                else "analysis",
                                "safety_mode": companion_safety_mode,
                                "model_alias": analyzer_alias,
                                "prompt_version": companion_analysis.prompt_version,
                                "classifier_version": companion_analysis.classifier_version,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                session.commit()
            operation_logs.emit(
                source="companion",
                kind="analysis",
                title="陪伴情绪分析",
                message=f"策略：{companion_analysis.next_action}",
                parent_operation_id=operation_id,
                trace_id=assessment.id,
                details={
                    "assessment_id": assessment.id,
                    "risk_level": companion_analysis.risk_level,
                    "support_need": companion_analysis.support_need,
                    "confidence": companion_analysis.confidence,
                    "schema_valid": companion_analysis.schema_valid,
                    "analysis_status": (
                        "safety_redirected"
                        if companion_safety_mode == "standard"
                        and companion_analysis.next_action == "safety_redirect"
                        else companion_analysis.analysis_status
                    ),
                    "model_alias": companion_analysis.model_alias,
                },
            )
            yield {
                "event": "companion_analysis",
                "data": self._companion_analysis_event(companion_analysis, companion_safety_mode),
            }

            standard_safety_redirect = (
                companion_safety_mode == "standard"
                and companion_analysis.next_action == "safety_redirect"
            )
            if standard_safety_redirect:
                safety_analysis = companion_analysis
                context_text = "\n".join(f"{item.role}: {item.content}" for item in prior_recent)
                persona_text = clip_text(
                    persona_system_prompt(active_persona(session, conversation.persona_id)),
                    8_192,
                )
                safety_answer = await asyncio.to_thread(
                    generate_safety_response,
                    conversation.model_alias,
                    text,
                    context_text,
                    persona_text,
                    SafetyAssessment(
                        safety_analysis.risk_level,
                        companion_safety.rules if companion_safety is not None else (),
                    ),
                )
                response_source = "safety_llm" if safety_answer else "template"
                answer = safety_answer or safety_redirect_text(safety_analysis.risk_level)
                session.add(
                    SafetyEvent(
                        message_id=user_message.id,
                        scope_id=sender_id,
                        risk_level=safety_analysis.risk_level,
                        action=(
                            "safety_response_llm"
                            if safety_answer
                            else "safety_template_fallback"
                        ),
                        details=json.dumps(
                            {
                                "source": response_source,
                                "response_source": response_source,
                                "safety_mode": companion_safety_mode,
                                "prompt_version": SAFETY_RESPONSE_PROMPT_VERSION
                                if safety_answer
                                else None,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                assistant_message = Message(
                    conversation_id=conversation.id,
                    sender_id="assistant",
                    role="assistant",
                    content=answer,
                )
                session.add(assistant_message)
                session.commit()
                operation_logs.finish_operation(
                    operation_id,
                    source="chat",
                    title="安全流程完成",
                    message="已发送安全支持提示",
                    details={
                        "message_id": assistant_message.id,
                        "risk_level": safety_analysis.risk_level,
                        "response_source": response_source,
                        "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
                    },
                )
                yield {"event": "token", "data": {"text": answer}}
                yield {
                    "event": "final",
                    "data": {
                        "message_id": assistant_message.id,
                        "text": answer,
                        "assessment_id": safety_analysis.assessment_id,
                        "response_source": response_source,
                    },
                }
                return

        previous_search_results = _has_immediate_search_results(
            session, conversation, user_message.id
        )
        requested_manga_intent = detect_manga_intent(
            text, previous_search_results=previous_search_results
        )
        manga_enabled = any(
            item.name == "manga" and item.builtin and item.enabled
            for item in list_packages(session, "tool")
        )
        allowed_manga_actions = (
            requested_manga_intent.actions if manga_enabled else frozenset()
        )
        manga_intent = MangaIntent(allowed_manga_actions)
        memory_scope_type = "group" if is_group else "user"
        memory_scope_id = conversation.external_id if is_group else sender_id
        memory_operation: str | None = None
        memory_allowed = not companion_enabled or bool(
            companion_preference is not None and companion_preference.memory_enabled
        )
        if not memory_allowed:
            memories = []
        else:
            try:
                memory_operation = operation_logs.start_operation(
                    source="memory",
                    title="长期记忆召回",
                    message="开始召回相关记忆",
                    parent_operation_id=operation_id,
                    details={"conversation_id": conversation.id, "sender_id": sender_id, "query": text},
                )
                memories = await asyncio.to_thread(
                    MemoryService(session).recall,
                    sender_id,
                    text,
                    5,
                    scope_type=memory_scope_type,
                    scope_id=memory_scope_id,
                )
                operation_logs.finish_operation(
                    memory_operation,
                    source="memory",
                    title="长期记忆召回完成",
                    message=f"命中 {len(memories)} 条记忆",
                    details={"hits": len(memories), "scope_type": memory_scope_type},
                )
            except Exception as error:
                logger.warning("长期记忆召回降级: %s", error)
                if memory_operation is not None:
                    operation_logs.finish_operation(
                        memory_operation,
                        source="memory",
                        title="长期记忆召回失败",
                        message="记忆召回降级为空",
                        success=False,
                        details={"error": f"{type(error).__name__}: {error}"},
                    )
                memories = []
        if memory_allowed:
            memory_text = clip_text(
                "\n".join(f"- {item.content}" for item in memories) or "- 无",
                4_096,
            )
        else:
            memory_text = "- 未获得记忆授权，本轮不召回长期记忆"
        relationship = (
            get_relationship(session, sender_id, conversation.persona_id)
            if companion_enabled and memory_allowed
            else None
        )
        knowledge_bases = list(session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name)))
        knowledge_text = clip_text(
            "\n".join(f"- {item.name}: {item.id}" for item in knowledge_bases) or "- 无",
            4_096,
        )
        skills_text = clip_text(skill_descriptions(session), 4_096)
        persona_text = clip_text(
            persona_system_prompt(active_persona(session, conversation.persona_id)),
            8_192,
        )
        companion_instruction = ""
        if companion_enabled and companion_analysis is not None:
            companion_instruction = (
                "\n\n当前是 QQ 私聊情感陪伴流程。候选情绪仅是可纠正的推断，不要把它说成诊断。"
                f"当前策略：{companion_analysis.next_action}；支持需要：{companion_analysis.support_need}。"
                f"角色关系资料：{relationship_context(relationship)}。"
                "先回应用户明确表达，不要为了展示能力主动调用工具。"
                "如果策略是 clarify，先用一句简短问题确认用户希望倾听、梳理、安慰还是建议。"
            )
        system = (
            f"{SYSTEM_PROMPT}\n\n当前用户画像和长期记忆：\n{memory_text}"
            f"\n\n可用知识库：\n{knowledge_text}"
            f"\n\n可按需加载的 Skill（只提供名称和描述）：\n{skills_text}"
            f"\n\n历史摘要：\n{clip_text(conversation.summary or '无', SUMMARY_MAX_TOKENS)}"
            f"\n\n{persona_text}"
            f"{companion_instruction}"
            f"{_manga_system_instruction(manga_intent)}"
        )
        try:
            snapshot = build_context_messages(system, recent, profile)
        except ContextOverflowError as error:
            operation_logs.finish_operation(
                operation_id,
                source="chat",
                title="对话失败",
                message="上下文超过模型限制",
                success=False,
                details={"error": str(error)},
            )
            yield {"event": "error", "data": {"message": str(error)}}
            return
        messages = snapshot.messages
        emitted = ""
        started_tools: set[str] = set()
        model_operation: str | None = None
        try:
            graph = build_agent_graph(
                session,
                conversation.model_alias,
                sender_id,
                conversation.id,
                platform=platform,
                is_group=is_group,
                allowed_manga_actions=allowed_manga_actions,
            )
            config: RunnableConfig = {"configurable": {"thread_id": f"{conversation.id}:{user_message.id}"}}
            input_state: MessagesState = {"messages": messages}
            model_operation = operation_logs.start_operation(
                source="model",
                title="模型调用",
                message=f"开始调用主聊天模型 {profile.model}",
                parent_operation_id=operation_id,
                trace_id=user_message.id,
                details={
                    "alias": profile.alias,
                    "model": profile.model,
                    "streaming": profile.streaming,
                    "reasoning_effort": profile.reasoning_effort,
                    "conversation_id": conversation.id,
                },
            )
            async for chunk, _metadata in graph.astream(input_state, config=config, stream_mode="messages"):
                if isinstance(chunk, (AIMessage, AIMessageChunk)):
                    if isinstance(chunk.content, str) and chunk.content:
                        emitted += chunk.content
                        if profile.streaming and not companion_enabled:
                            yield {"event": "token", "data": {"text": chunk.content}}
                    tool_calls = (
                        chunk.tool_call_chunks if isinstance(chunk, AIMessageChunk) else chunk.tool_calls
                    )
                    for call in tool_calls:
                        call_id = str(call.get("id") or "")
                        if call_id and call_id not in started_tools:
                            started_tools.add(call_id)
                            operation_logs.emit(
                                source="tool",
                                kind="started",
                                title="工具调用开始",
                                message=str(call.get("name") or "未知工具"),
                                operation_id=call_id,
                                parent_operation_id=operation_id,
                                details={
                                    "tool_call_id": call_id,
                                    "name": call.get("name"),
                                    "arguments": call.get("args") or call.get("arguments") or {},
                                },
                            )
                            yield {
                                "event": "tool_started",
                                "data": {"tool_call_id": call_id, "name": call.get("name")},
                            }
                if isinstance(chunk, ToolMessage):
                    tool_run = ToolRun(
                        conversation_id=conversation.id,
                        tool_name=str(chunk.name or "unknown"),
                        arguments="{}",
                        result=str(chunk.content),
                        status="succeeded",
                    )
                    session.add(tool_run)
                    session.commit()
                    data = {
                        "tool_call_id": chunk.tool_call_id,
                        "name": chunk.name,
                        "result": chunk.content,
                    }
                    operation_logs.finish_operation(
                        str(chunk.tool_call_id or "tool-result"),
                        source="tool",
                        title="工具调用完成",
                        message=str(chunk.name or "未知工具"),
                        parent_operation_id=operation_id,
                        details={
                            "tool_call_id": chunk.tool_call_id,
                            "name": chunk.name,
                            "result": chunk.content,
                        },
                    )
                    yield {"event": "tool_finished", "data": data}
                    tool_result = normalize_tool_result(chunk.content)
                    if tool_result.get("pending_confirmation"):
                        yield {"event": "pending_confirmation", "data": tool_result}
                    result_data = tool_result.get("data")
                    if isinstance(result_data, dict) and isinstance(result_data.get("task"), dict):
                        yield {"event": "task_created", "data": result_data["task"]}
                    artifact_ids = tool_result.get("artifact_ids", [])
                    if isinstance(artifact_ids, list):
                        for artifact_id in artifact_ids:
                            yield {
                                "event": "artifact_created",
                                "data": {"artifact_id": str(artifact_id)},
                            }
            snapshot = await graph.aget_state(config)
            final = final_ai_message(snapshot.values["messages"])
            answer = (
                final.content
                if isinstance(final.content, str)
                else json.dumps(final.content, ensure_ascii=False)
            )
            response_source = "agent"
            if companion_enabled and companion_safety_mode == "standard":
                violations = output_safety_violations(answer)
                if violations:
                    rewritten = await asyncio.to_thread(
                        rewrite_blocked_response,
                        conversation.model_alias,
                        text,
                        answer,
                        violations,
                    )
                    response_source = "rewritten" if rewritten else "template"
                    answer = rewritten or safety_redirect_text("medium")
                    session.add(
                        SafetyEvent(
                            message_id=user_message.id,
                            scope_id=sender_id,
                            risk_level="medium",
                            action=(
                                "output_llm_rewrite"
                                if rewritten
                                else "output_template_fallback"
                            ),
                            details=json.dumps(
                                {
                                    "source": response_source,
                                    "response_source": response_source,
                                    "violation_codes": violations,
                                    "prompt_version": SAFETY_REWRITE_PROMPT_VERSION
                                    if rewritten
                                    else None,
                                    "safety_mode": companion_safety_mode,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    )
                    session.commit()
            if (not profile.streaming or companion_enabled) and answer:
                yield {"event": "token", "data": {"text": answer}}
            elif not emitted and answer:
                yield {"event": "token", "data": {"text": answer}}
            assistant_message = Message(
                conversation_id=conversation.id,
                sender_id="assistant",
                role="assistant",
                content=answer,
            )
            session.add(assistant_message)
            session.commit()
            operation_logs.finish_operation(
                model_operation,
                source="model",
                title="模型调用完成",
                message="模型回复已生成",
                details={
                    "output_chars": len(answer),
                    "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
                },
            )
            operation_logs.finish_operation(
                operation_id,
                source="chat",
                title="对话完成",
                message="回复已写入会话",
                details={
                    "message_id": assistant_message.id,
                    "output_chars": len(answer),
                    "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
                    "tools": len(started_tools),
                },
            )
            yield {
                "event": "final",
                "data": {
                    "message_id": assistant_message.id,
                    "text": answer,
                    "assessment_id": companion_analysis.assessment_id
                    if companion_analysis is not None
                    else None,
                    "response_source": response_source,
                },
            }
            asyncio.create_task(
                asyncio.to_thread(
                    self._post_turn,
                    conversation.id,
                    sender_id,
                    text,
                    user_message.id,
                    conversation.model_alias,
                    is_group,
                    companion_enabled,
                    bool(companion_preference and companion_preference.memory_enabled),
                    conversation.persona_id,
                )
            )
        except Exception as error:
            logger.exception("聊天执行失败")
            if model_operation is not None:
                operation_logs.finish_operation(
                    model_operation,
                    source="model",
                    title="模型调用失败",
                    message="模型请求失败",
                    success=False,
                    details={"error": f"{type(error).__name__}: {error}"},
                )
            operation_logs.finish_operation(
                operation_id,
                source="chat",
                title="对话失败",
                message="请求未完成",
                success=False,
                details={
                    "error": f"{type(error).__name__}: {error}",
                    "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
                },
            )
            yield {"event": "error", "data": {"message": f"{type(error).__name__}: {error}"}}

    def _post_turn(
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
            turn_context = "\n".join(f"{item.role}: {item.content}" for item in context_rows[:-1])
            memory_scope_type = "group" if is_group else "user"
            memory_scope_id = conversation.external_id if is_group else sender_id
            memory_operation: str | None = None
            try:
                if companion_enabled and not memory_enabled:
                    with self._summary_lock(conversation_id):
                        self._compact_conversation(session, conversation, model_alias)
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
                self._compact_conversation(session, conversation, model_alias)

    def _compact_conversation(
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
        new_material = "\n".join(f"{item.role}: {item.content}" for item in batch)
        summary_source = (
            f"已有摘要：\n{existing_summary or '无'}\n\n新增历史：\n{new_material}"
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


chat_service = ChatService()
