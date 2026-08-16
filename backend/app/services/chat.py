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

from app.db.models import Conversation, KnowledgeBase, Message
from app.db.session import SessionLocal
from app.services.memories import MemoryService
from app.services.models import model_registry
from app.workflows.agent import build_agent_graph, final_ai_message

logger = logging.getLogger(__name__)
SYSTEM_PROMPT = """你是 PersonalAgent，一个本地个人助理。
优先使用已经提供的用户画像和长期记忆，但不要把它们当作当前用户刚说的话。
文档问题需要使用 search_knowledge，并在回答中写明文件、标题和页码或位置。
漫画搜索可以直接执行；下载必须使用 request_manga_download 并等待 Owner 明确确认。
不要声称工具成功，除非工具结果明确表示成功。"""


class ChatService:
    async def stream(
        self,
        session: Session,
        conversation: Conversation,
        sender_id: str,
        text: str,
        platform_message_id: str | None = None,
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
        system = (
            f"{SYSTEM_PROMPT}\n\n当前用户画像和长期记忆：\n{memory_text}"
            f"\n\n可用知识库：\n{knowledge_text}"
            f"\n\n历史摘要：\n{conversation.summary or '无'}"
        )
        messages: list[AnyMessage] = [SystemMessage(system)]
        messages.extend(
            HumanMessage(item.content) if item.role == "user" else AIMessage(item.content) for item in recent
        )
        emitted = ""
        started_tools: set[str] = set()
        try:
            graph = build_agent_graph(session, conversation.model_alias, sender_id, conversation.id)
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
                    data = {
                        "tool_call_id": chunk.tool_call_id,
                        "name": chunk.name,
                        "result": chunk.content,
                    }
                    yield {"event": "tool_finished", "data": data}
                    try:
                        tool_result = json.loads(str(chunk.content))
                    except json.JSONDecodeError:
                        tool_result = {}
                    if tool_result.get("pending_confirmation"):
                        yield {"event": "pending_confirmation", "data": tool_result}
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
