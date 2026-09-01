from __future__ import annotations

import json

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.domain.tool_types import ToolContext
from app.services.builtin_tools import (
    MANGA_TOOL_ACTIONS,
    KnowledgeSearchGuard,
    tool_envelope,
)
from app.services.context import (
    TOOL_RESULT_MAX_CHARS,
    TOOL_RESULT_TOTAL_CHARS,
    limit_tool_content,
)
from app.services.extensions import list_packages
from app.services.jobs import create_manga_download_job
from app.services.mcp_search import AnySearchCallGuard
from app.services.models import model_registry
from app.services.runtime import is_owner
from app.services.tool_registry import build_registered_tools

__all__ = [
    "KnowledgeSearchGuard",
    "build_agent_graph",
    "final_ai_message",
    "reject_unauthorized_manga_calls",
    "skill_descriptions",
    "tool_envelope",
]


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


def build_agent_graph(
    session: Session,
    model_alias: str,
    requester_id: str,
    conversation_id: str,
    *,
    platform: str = "web",
    is_group: bool = False,
    allowed_manga_actions: frozenset[str] = frozenset(),
    allow_anysearch_tools: bool = False,
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
    search_guard = AnySearchCallGuard() if allow_anysearch_tools else None
    tools = build_registered_tools(
        session,
        context,
        allowed_manga_actions=allowed_manga_actions,
        knowledge_search_guard=knowledge_search_guard,
        manga_download_job_factory=create_manga_download_job,
        allow_anysearch_tools=allow_anysearch_tools,
        search_guard=search_guard,
    )
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
    return builder.compile()


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
