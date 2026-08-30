from __future__ import annotations

import json

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import Conversation, Memory, Message, ToolRun
from app.db.session import SessionLocal
from app.services.manga_intent import MangaIntent
from app.services.memories import MemoryService


def recall_memories_in_worker_session(
    user_id: str,
    query: str,
    limit: int,
    *,
    scope_type: str,
    scope_id: str | None,
) -> list[Memory]:
    with SessionLocal() as worker_session:
        return MemoryService(worker_session).recall(
            user_id,
            query,
            limit,
            scope_type=scope_type,
            scope_id=scope_id,
        )


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
            value = decoded if metadata_keys.intersection(decoded) else {"data": decoded}
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


def has_immediate_search_results(
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


def manga_system_instruction(intent: MangaIntent) -> str:
    if not intent.actions:
        return (
            "\n\n漫画工具默认关闭。本轮没有通过明确漫画意图门禁时，不得调用任何漫画工具；"
            "不要把普通词语、话题或能力询问当成漫画搜索。"
        )
    action_names = {"search": "搜索", "download": "下载", "delete": "删除"}
    allowed = "、".join(action_names[action] for action in sorted(intent.actions))
    return (
        f"\n\n本轮程序已确认用户明确请求漫画{allowed}，只能在完成参数提取后调用对应漫画工具；"
        "不得额外调用未授权的漫画动作。"
    )
