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
from app.services.artifacts import ArtifactError, artifact_envelope, create_artifact
from app.services.extensions import (
    ExtensionError,
    ToolContext,
    list_packages,
    load_python_tools,
    load_skill_text,
)
from app.services.extensions import (
    read_skill_resource as read_skill_resource_file,
)
from app.services.jobs import create_manga_download_job, delete_manga_artifact
from app.services.manga import manga_service
from app.services.models import model_registry
from app.services.retrieval import HybridRetriever
from app.services.runtime import is_owner

_checkpoint_connection: aiosqlite.Connection | None = None
_checkpointer: AsyncSqliteSaver | None = None


def tool_envelope(
    data: object,
    *,
    pending_confirmation: bool = False,
    artifact_ids: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "kind": "tool_result",
            "data": data,
            "pending_confirmation": pending_confirmation,
            "artifact_ids": artifact_ids or [],
        },
        ensure_ascii=False,
    )


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
    *,
    platform: str = "web",
    is_group: bool = False,
):
    settings = get_settings()
    owner = is_owner(requester_id)
    context = ToolContext(
        requester_id=requester_id,
        conversation_id=conversation_id,
        platform=platform,
        is_group=is_group,
        workspace_path=settings.workspace_path,
        is_owner=owner,
    )

    @tool
    def search_knowledge(query: str, knowledge_base_id: str = "") -> str:
        """在个人知识库中检索证据。knowledge_base_id 可从系统提供的清单选择；留空使用首个知识库。"""
        if not knowledge_base_id:
            first = session.scalar(select(KnowledgeBase).order_by(KnowledgeBase.created_at))
            if first is None:
                return tool_envelope({"evidence": [], "message": "尚未创建知识库"})
            knowledge_base_id = first.id
        retriever = HybridRetriever(session)
        hits = retriever.search(knowledge_base_id, query)
        return tool_envelope(
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
                ],
            }
        )

    @tool
    def search_manga(query: str) -> str:
        """根据关键词搜索漫画，只返回候选，不会开始下载。"""
        return tool_envelope({"results": manga_service.search(query)})

    @tool
    def request_manga_download(album_id: str) -> str:
        """Owner 立即创建指定漫画 ID 的下载任务，无需二次确认；非 Owner 无权下载。"""
        try:
            job = create_manga_download_job(album_id, requester_id, conversation_id)
        except (PermissionError, ValueError) as error:
            return tool_envelope({"error": str(error)})
        delivery_message = (
            "下载成功后会自动发送到当前 Owner QQ 私聊。发送完成后如需清理，请明确要求删除并提供任务 ID。"
            if platform == "qq" and requester_id.isdigit()
            else "任务完成后可在 Web 任务页面下载产物。"
        )
        return tool_envelope(
            {
                "task": {"id": job.id, "type": job.type, "status": job.status},
                "message": delivery_message,
            }
        )

    @tool
    def delete_manga_download(job_id: str) -> str:
        """删除已成功发送的漫画任务产物。仅限 Owner 私聊明确要求删除指定任务时调用。"""
        if is_group:
            return tool_envelope({"error": "群聊禁止删除漫画产物"})
        try:
            result = delete_manga_artifact(job_id, requester_id)
        except (PermissionError, ValueError, OSError) as error:
            return tool_envelope({"error": str(error)})
        return tool_envelope(result)

    @tool
    def create_files(files: list[dict[str, str]]) -> str:
        """创建代码或文本文件。files 为 filename/content 对象数组；只能在私聊使用，不会执行文件。"""
        if is_group or platform == "group":
            return tool_envelope({"error": "群聊禁止创建文件"})
        try:
            pairs = [(str(item["filename"]), str(item["content"]).encode("utf-8")) for item in files]
            artifact = create_artifact(
                session,
                owner_id=requester_id,
                conversation_id=conversation_id,
                files=pairs,
            )
            return tool_envelope(
                {"artifact": artifact_envelope(artifact)}, artifact_ids=[artifact.id]
            )
        except (KeyError, TypeError, ArtifactError) as error:
            return tool_envelope({"error": str(error)})

    loaded_skills: set[str] = set()

    @tool
    def load_skill(name: str) -> str:
        """按需加载一个 Skill 的完整 SKILL.md；每轮最多加载三个。"""
        if name in loaded_skills:
            return tool_envelope({"name": name, "content": "该 Skill 已加载"})
        if len(loaded_skills) >= 3:
            return tool_envelope({"error": "每轮最多加载 3 个 Skill"})
        try:
            content = load_skill_text(session, name)
        except ExtensionError as error:
            return tool_envelope({"error": str(error)})
        loaded_skills.add(name)
        return tool_envelope({"name": name, "content": content})

    @tool
    def read_skill_resource(name: str, relative_path: str) -> str:
        """读取已启用 Skill 根目录内的 references/assets 文件，不执行 scripts。"""
        if name not in loaded_skills:
            return tool_envelope({"error": "请先调用 load_skill 加载该 Skill"})
        try:
            content = read_skill_resource_file(session, name, relative_path)
        except ExtensionError as error:
            return tool_envelope({"error": str(error)})
        return tool_envelope({"name": name, "path": relative_path, "content": content})

    enabled_builtins = {
        item.name for item in list_packages(session, "tool") if item.builtin and item.enabled
    }
    tools = [load_skill, read_skill_resource]
    if "knowledge-search" in enabled_builtins:
        tools.append(search_knowledge)
    if "manga" in enabled_builtins:
        tools.extend([search_manga, request_manga_download, delete_manga_download])
    if "create-files" in enabled_builtins:
        tools.append(create_files)
    tools.extend(load_python_tools(session, context))
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


def skill_descriptions(session: Session) -> str:
    rows = [item for item in list_packages(session, "skill") if item.enabled]
    if not rows:
        return "- 无"
    return "\n".join(f"- {item.name}: {item.description}" for item in rows)


def final_ai_message(messages: list[BaseMessage]) -> AIMessage:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return message
    raise RuntimeError("Agent 未生成最终回答")
