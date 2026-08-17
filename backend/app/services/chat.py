from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import MessagesState
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Conversation, KnowledgeBase, Message, ToolRun
from app.db.session import SessionLocal
from app.services.memories import MemoryService
from app.services.models import model_registry
from app.services.personas import active_style, style_system_prompt
from app.workflows.agent import build_agent_graph, final_ai_message, skill_descriptions

logger = logging.getLogger(__name__)
SYSTEM_PROMPT = """你是 PersonalAgent，一个本地个人助理。
优先使用已经提供的用户画像和长期记忆，但不要把它们当作当前用户刚说的话。
文档问题需要使用 search_knowledge，并在回答中写明文件、标题和页码或位置。
漫画搜索可以直接执行；Owner 请求下载时必须使用 request_manga_download，任务会立即创建，无需二次确认。
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


class ChatService:
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

        recent = list(
            session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.desc())
                .limit(17)
            )
        )
        recent.reverse()
        try:
            memories = await asyncio.to_thread(MemoryService(session).recall, sender_id, text)
        except Exception as error:
            logger.warning("长期记忆召回降级: %s", error)
            memories = []
        memory_text = "\n".join(f"- {item.content}" for item in memories) or "- 无"
        knowledge_bases = list(session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name)))
        knowledge_text = (
            "\n".join(f"- {item.name}: {item.id}" for item in knowledge_bases) or "- 无"
        )
        skills_text = skill_descriptions(session)
        persona_text = style_system_prompt(active_style(session, sender_id))
        system = (
            f"{SYSTEM_PROMPT}\n\n当前用户画像和长期记忆：\n{memory_text}"
            f"\n\n可用知识库：\n{knowledge_text}"
            f"\n\n可按需加载的 Skill（只提供名称和描述）：\n{skills_text}"
            f"\n\n历史摘要：\n{conversation.summary or '无'}"
            f"\n\n{persona_text}"
        )
        messages: list[AnyMessage] = [SystemMessage(system)]
        messages.extend(
            HumanMessage(item.content) if item.role == "user" else AIMessage(item.content) for item in recent
        )
        emitted = ""
        started_tools: set[str] = set()
        try:
            graph = build_agent_graph(
                session,
                conversation.model_alias,
                sender_id,
                conversation.id,
                platform=platform,
                is_group=is_group,
            )
            config: RunnableConfig = {"configurable": {"thread_id": f"{conversation.id}:{user_message.id}"}}
            input_state: MessagesState = {"messages": messages}
            async for chunk, _metadata in graph.astream(input_state, config=config, stream_mode="messages"):
                if isinstance(chunk, AIMessageChunk) and isinstance(chunk.content, str) and chunk.content:
                    emitted += chunk.content
                    yield {"event": "token", "data": {"text": chunk.content}}
                if isinstance(chunk, AIMessageChunk):
                    for call in chunk.tool_call_chunks:
                        call_id = str(call.get("id") or "")
                        if call_id and call_id not in started_tools:
                            started_tools.add(call_id)
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
            if not emitted and answer:
                yield {"event": "token", "data": {"text": answer}}
            assistant_message = Message(
                conversation_id=conversation.id,
                sender_id="assistant",
                role="assistant",
                content=answer,
            )
            session.add(assistant_message)
            session.commit()
            yield {"event": "final", "data": {"message_id": assistant_message.id, "text": answer}}
            asyncio.create_task(
                asyncio.to_thread(
                    self._post_turn,
                    conversation.id,
                    sender_id,
                    text,
                    user_message.id,
                    conversation.model_alias,
                )
            )
        except Exception as error:
            logger.exception("聊天执行失败")
            yield {"event": "error", "data": {"message": f"{type(error).__name__}: {error}"}}

    def _post_turn(
        self,
        conversation_id: str,
        sender_id: str,
        user_text: str,
        message_id: str,
        model_alias: str,
    ) -> None:
        with SessionLocal() as session:
            try:
                MemoryService(session).extract_from_turn(sender_id, user_text, message_id, model_alias)
            except Exception as error:
                logger.warning("自动记忆提取失败: %s", error)
            messages = list(
                session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at)
                )
            )
            if len(messages) <= 18:
                return
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return
            try:
                model = model_registry.chat_model(model_alias)
                summary_source = "\n".join(f"{item.role}: {item.content}" for item in messages[:-8])
                response = model.invoke(
                    [
                        ("system", "将以下历史对话压缩为准确、简短的中文摘要，不添加新事实。"),
                        ("human", summary_source),
                    ]
                )
                if isinstance(response.content, str):
                    conversation.summary = response.content
                    session.commit()
            except Exception as error:
                logger.warning("历史摘要失败: %s", error)


chat_service = ChatService()
