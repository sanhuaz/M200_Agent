from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

import aiosqlite
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import KnowledgeBase
from app.services.artifacts import ArtifactError, artifact_envelope, create_artifact
from app.services.context import (
    TOOL_RESULT_MAX_CHARS,
    TOOL_RESULT_TOTAL_CHARS,
    limit_tool_content,
)
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
MAX_KNOWLEDGE_SEARCHES_PER_TURN = 4
MANGA_TOOL_ACTIONS = {
    "search_manga": "search",
    "request_manga_download": "download",
    "delete_manga_download": "delete",
}


@dataclass
class KnowledgeSearchGuard:
    max_unique_searches: int = MAX_KNOWLEDGE_SEARCHES_PER_TURN
    _seen: set[tuple[str, str]] = field(default_factory=set)
    _force_final: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _key(query: str, knowledge_base_id: str) -> tuple[str, str]:
        return (" ".join(query.casefold().split()), knowledge_base_id)

    def reserve(self, query: str, knowledge_base_id: str) -> str:
        with self._lock:
            key = self._key(query, knowledge_base_id)
            if key in self._seen:
                self._force_final = True
                return "duplicate"
            if len(self._seen) >= self.max_unique_searches:
                self._force_final = True
                return "limit"
            self._seen.add(key)
            if len(self._seen) >= self.max_unique_searches:
                self._force_final = True
            return "allowed"

    @property
    def force_final(self) -> bool:
        with self._lock:
            return self._force_final


def tool_envelope(
    data: object,
    *,
    pending_confirmation: bool = False,
    artifact_ids: list[str] | None = None,
) -> str:
    payload = json.dumps(
        {
            "kind": "tool_result",
            "data": data,
            "pending_confirmation": pending_confirmation,
            "artifact_ids": artifact_ids or [],
        },
        ensure_ascii=False,
    )
    return limit_tool_content(payload, TOOL_RESULT_MAX_CHARS)


def reject_unauthorized_manga_calls(
    response: BaseMessage,
    allowed_manga_actions: frozenset[str],
) -> BaseMessage:
    """即使模型伪造未注册工具调用，也只能安全结束当前轮。"""

    calls = getattr(response, "tool_calls", None)
    if not isinstance(calls, list):
        return response
    for call in calls:
        if not isinstance(call, dict):
            continue
        action = MANGA_TOOL_ACTIONS.get(str(call.get("name") or ""))
        if action is not None and action not in allowed_manga_actions:
            return AIMessage(content="本轮未检测到明确的漫画操作意图，未执行漫画工具。")
    return response


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
    allowed_manga_actions: frozenset[str] = frozenset(),
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
    knowledge_search_guard = KnowledgeSearchGuard()

    @tool
    def search_knowledge(query: str, knowledge_base_id: str = "") -> str:
        """在个人知识库中检索证据。每轮最多四个不同查询；重复查询会停止继续检索。"""
        if not knowledge_base_id:
            first = session.scalar(select(KnowledgeBase).order_by(KnowledgeBase.created_at))
            if first is None:
                return tool_envelope({"evidence": [], "message": "尚未创建知识库"})
            knowledge_base_id = first.id
        decision = knowledge_search_guard.reserve(query, knowledge_base_id)
        if decision == "duplicate":
            return tool_envelope(
                {
                    "evidence": [],
                    "duplicate": True,
                    "message": "相同查询和知识库已检索过，请使用已有证据直接回答。",
                }
            )
        if decision == "limit":
            return tool_envelope(
                {
                    "evidence": [],
                    "limit_reached": True,
                    "message": "本轮知识检索已达到上限，请使用已有证据直接回答。",
                }
            )
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
        """在程序确认用户明确请求搜索漫画后，根据关键词搜索并只返回候选。"""
        if "search" not in allowed_manga_actions:
            return tool_envelope({"error": "本轮没有明确的漫画搜索意图"})
        return tool_envelope({"results": manga_service.search(query)})

    @tool
    def request_manga_download(album_id: str) -> str:
        """在程序确认用户明确请求下载后，Owner 立即创建指定漫画 ID 的任务。"""
        if "download" not in allowed_manga_actions:
            return tool_envelope({"error": "本轮没有明确的漫画下载意图"})
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
        """在程序确认明确删除意图后，删除已成功发送的漫画任务产物。"""
        if "delete" not in allowed_manga_actions:
            return tool_envelope({"error": "本轮没有明确的漫画删除意图"})
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
        if "search" in allowed_manga_actions:
            tools.append(search_manga)
        if "download" in allowed_manga_actions:
            tools.append(request_manga_download)
        if "delete" in allowed_manga_actions:
            tools.append(delete_manga_download)
    if "create-files" in enabled_builtins:
        tools.append(create_files)
    tools.extend(load_python_tools(session, context))
    base_model = model_registry.chat_model(model_alias)
    model = base_model.bind_tools(tools)
    tool_node = ToolNode(tools, handle_tool_errors=True)

    async def call_model(state: MessagesState) -> dict[str, list[BaseMessage]]:
        messages = state["messages"]
        if knowledge_search_guard.force_final:
            messages = [
                *messages,
                SystemMessage(
                    content=(
                        "知识库检索已经完成或停止。请仅依据已有工具证据立即给出最终回答，"
                        "不要再调用任何工具；证据不足时明确说明。"
                    )
                ),
            ]
            response = await base_model.ainvoke(messages)
        else:
            response = await model.ainvoke(messages)
        return {"messages": [reject_unauthorized_manga_calls(response, allowed_manga_actions)]}

    async def call_tools(state: MessagesState) -> dict[str, list[BaseMessage]]:
        result = await tool_node.ainvoke(state)
        output = result.get("messages", [])
        if not isinstance(output, list):
            return {"messages": []}
        used = sum(
            len(str(item.content))
            for item in state.get("messages", [])
            if isinstance(item, ToolMessage)
        )
        sanitized: list[BaseMessage] = []
        for item in output:
            if not isinstance(item, ToolMessage):
                sanitized.append(item)
                continue
            available = TOOL_RESULT_TOTAL_CHARS - used
            if available <= 0:
                content = json.dumps(
                    {
                        "kind": "tool_result",
                        "data": {"truncated": True, "reason": "本轮工具上下文预算已用尽"},
                    },
                    ensure_ascii=False,
                )
            else:
                content = limit_tool_content(item.content, min(TOOL_RESULT_MAX_CHARS, available))
            sanitized.append(item.model_copy(update={"content": content}))
            used += len(content)
        return {"messages": sanitized}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", call_tools)
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
