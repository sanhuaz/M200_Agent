from __future__ import annotations

import json

import aiosqlite
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import KnowledgeBase
from app.services.confirmations import create_download_confirmation
from app.services.manga import manga_service
from app.services.models import model_registry
from app.services.retrieval import HybridRetriever

_checkpoint_connection: aiosqlite.Connection | None = None
_checkpointer: AsyncSqliteSaver | None = None


async def initialize_checkpointer() -> None:
    global _checkpoint_connection, _checkpointer
    if _checkpointer is None:
        path = get_settings().database_path.with_name("langgraph_checkpoints.db")
        _checkpoint_connection = await aiosqlite.connect(path)
        _checkpointer = AsyncSqliteSaver(_checkpoint_connection)


def get_checkpointer() -> AsyncSqliteSaver:
    if _checkpointer is None:
        raise RuntimeError("LangGraph Checkpointer 尚未初始化")
    return _checkpointer


async def close_checkpointer() -> None:
    global _checkpoint_connection, _checkpointer
    if _checkpoint_connection is not None:
        await _checkpoint_connection.close()
    _checkpoint_connection = None
    _checkpointer = None


def build_agent_graph(
    session: Session,
    model_alias: str,
    requester_id: str,
    conversation_id: str,
):
    @tool
    def search_knowledge(query: str, knowledge_base_id: str = "") -> str:
        """在个人知识库中检索证据。knowledge_base_id 可从系统提供的清单选择；留空使用首个知识库。"""
        if not knowledge_base_id:
            first = session.scalar(select(KnowledgeBase).order_by(KnowledgeBase.created_at))
            if first is None:
                return json.dumps({"evidence": [], "message": "尚未创建知识库"}, ensure_ascii=False)
            knowledge_base_id = first.id
        retriever = HybridRetriever(session)
        hits = retriever.search(knowledge_base_id, query)
        return json.dumps(
            {
                "retrieval": {"reranker": retriever.reranker_status},
                "evidence": [
                    {
                        "content": hit.content,
                        "filename": hit.filename,
                        "heading_path": hit.heading_path,
                        "page_number": hit.page_number,
                        "vector_score": hit.vector_score,
                        "keyword_score": hit.keyword_score,
                        "rrf_score": hit.rrf_score,
                        "rerank_score": hit.rerank_score,
                    }
                    for hit in hits
                ]
            },
            ensure_ascii=False,
        )

    @tool
    def search_manga(query: str) -> str:
        """根据关键词搜索漫画，只返回候选，不会开始下载。"""
        return json.dumps(manga_service.search(query), ensure_ascii=False)

    @tool
    def request_manga_download(album_id: str) -> str:
        """请求下载指定漫画 ID。该操作只创建待确认请求，不会立即下载。"""
        confirmation = create_download_confirmation(album_id, requester_id, conversation_id)
        return json.dumps(
            {
                "pending_confirmation": True,
                "token": confirmation.token,
                "expires_at": confirmation.expires_at.isoformat(),
                "message": f"请使用 /confirm {confirmation.token} 确认下载",
            },
            ensure_ascii=False,
        )

    tools = [search_knowledge, search_manga, request_manga_download]
    model = model_registry.chat_model(model_alias).bind_tools(tools)

    async def call_model(state: MessagesState) -> dict[str, list[BaseMessage]]:
        response = await model.ainvoke(state["messages"])
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=get_checkpointer())


def final_ai_message(messages: list[BaseMessage]) -> AIMessage:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return message
    raise RuntimeError("Agent 未生成最终回答")
