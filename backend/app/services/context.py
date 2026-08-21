from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import ModelProfile
from app.db.models import Conversation, Message

# DeepSeek 的精确 tokenizer 不是 LangChain 的通用依赖。这里使用偏保守的估算，
# 只负责本地预算和裁剪；真正请求仍由服务端按官方 tokenizer 计量。
CHARS_PER_TOKEN = 1.0
SUMMARY_TRIGGER_TOKENS = 65_536
SUMMARY_INPUT_TOKENS = 56_000
SUMMARY_MAX_TOKENS = 8_192
SUMMARY_KEEP_MESSAGES = 12
SUMMARY_BATCH_MESSAGES = 256
CONTEXT_HISTORY_SCAN = 512
TOOL_RESULT_MAX_CHARS = 12_000
TOOL_RESULT_TOTAL_CHARS = 32_000


class ContextOverflowError(ValueError):
    """当前消息或固定系统提示超过可用上下文预算。"""


@dataclass(frozen=True)
class ContextSnapshot:
    messages: list[AnyMessage]
    estimated_tokens: int
    soft_limit: int
    candidate_count: int


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return count_tokens_approximately(
        [HumanMessage(content=text)],
        chars_per_token=CHARS_PER_TOKEN,
    )


def estimate_messages_tokens(messages: Sequence[AnyMessage]) -> int:
    if not messages:
        return 0
    return count_tokens_approximately(messages, chars_per_token=CHARS_PER_TOKEN)


def clip_text(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    max_chars = max(1, int(max_tokens * CHARS_PER_TOKEN))
    if len(text) <= max_chars:
        return text
    marker = "\n[内容已按上下文预算裁剪]"
    return text[: max(1, max_chars - len(marker))] + marker


def _message_content(item: Message) -> str:
    return f"{item.role}: {item.content}"


def _boundary(session: Session, conversation: Conversation) -> Message | None:
    if not conversation.summary_up_to_message_id:
        return None
    item = session.get(Message, conversation.summary_up_to_message_id)
    if item is None or item.conversation_id != conversation.id:
        return None
    return item


def _after_boundary(boundary: Message | None):
    if boundary is None:
        return None
    return or_(
        Message.created_at > boundary.created_at,
        and_(Message.created_at == boundary.created_at, Message.id > boundary.id),
    )


def load_pending_messages(
    session: Session,
    conversation: Conversation,
    *,
    limit: int = CONTEXT_HISTORY_SCAN,
    newest: bool = True,
) -> list[Message]:
    """读取摘要边界之后的消息，不重新扫描整个会话历史。"""

    boundary = _boundary(session, conversation)
    query = select(Message).where(Message.conversation_id == conversation.id)
    condition = _after_boundary(boundary)
    if condition is not None:
        query = query.where(condition)
    if newest:
        query = query.order_by(desc(Message.created_at), desc(Message.id)).limit(limit)
        rows = list(session.scalars(query))
        rows.reverse()
        return rows
    return list(session.scalars(query.order_by(Message.created_at, Message.id).limit(limit)))


def pending_message_count(session: Session, conversation: Conversation) -> int:
    boundary = _boundary(session, conversation)
    query = select(func.count(Message.id)).where(Message.conversation_id == conversation.id)
    condition = _after_boundary(boundary)
    if condition is not None:
        query = query.where(condition)
    return int(session.scalar(query) or 0)


def pending_prefix(
    session: Session,
    conversation: Conversation,
    *,
    limit: int = SUMMARY_BATCH_MESSAGES,
) -> list[Message]:
    boundary = _boundary(session, conversation)
    query = select(Message).where(Message.conversation_id == conversation.id)
    condition = _after_boundary(boundary)
    if condition is not None:
        query = query.where(condition)
    return list(session.scalars(query.order_by(Message.created_at, Message.id).limit(limit)))


def should_compact(
    session: Session,
    conversation: Conversation,
    *,
    pending: list[Message] | None = None,
) -> bool:
    count = pending_message_count(session, conversation)
    if count >= 40:
        return True
    rows = pending if pending is not None else pending_prefix(session, conversation, limit=40)
    text = [HumanMessage(content=_message_content(item)) for item in rows]
    return estimate_messages_tokens(text) >= SUMMARY_TRIGGER_TOKENS


def choose_summary_batch(
    rows: list[Message],
    existing_summary: str,
    *,
    max_tokens: int = SUMMARY_INPUT_TOKENS,
) -> list[Message]:
    """选择最老的一段待压缩消息，限制摘要请求本身的输入大小。"""

    used = estimate_text_tokens(existing_summary)
    selected: list[Message] = []
    for item in rows:
        content = _message_content(item)
        cost = estimate_text_tokens(content)
        remaining = max_tokens - used
        if remaining <= 0:
            break
        if cost > remaining:
            if not selected:
                # 极端长单条消息仍推进边界，但让摘要看到有限前缀。
                clipped = Message(
                    id=item.id,
                    conversation_id=item.conversation_id,
                    sender_id=item.sender_id,
                    role=item.role,
                    content=clip_text(item.content, remaining),
                    platform_message_id=item.platform_message_id,
                    created_at=item.created_at,
                )
                selected.append(clipped)
            break
        selected.append(item)
        used += cost
    return selected


def build_context_messages(
    system_text: str,
    history: list[Message],
    profile: ModelProfile,
) -> ContextSnapshot:
    system = SystemMessage(content=system_text)
    system_tokens = estimate_messages_tokens([system])
    if system_tokens >= profile.input_soft_limit:
        raise ContextOverflowError("系统提示词已超过上下文软上限，请减少 Skill、知识库或记忆内容。")

    selected: list[AnyMessage] = []
    remaining = history[:]
    while remaining:
        candidate = remaining[-1]
        converted = (
            HumanMessage(content=candidate.content)
            if candidate.role == "user"
            else AIMessage(content=candidate.content)
        )
        if estimate_messages_tokens([system, *selected, converted]) > profile.input_soft_limit:
            break
        selected.insert(0, converted)
        remaining.pop()

    messages = [system, *selected]
    estimated = estimate_messages_tokens(messages)
    if history and not selected:
        raise ContextOverflowError("当前消息超过上下文软上限，请缩短本条消息。")
    return ContextSnapshot(
        messages=messages,
        estimated_tokens=estimated,
        soft_limit=profile.input_soft_limit,
        candidate_count=len(history),
    )


def limit_tool_content(content: object, max_chars: int = TOOL_RESULT_MAX_CHARS) -> str:
    """限制工具返回大小，同时尽量保留 JSON 结构和完整证据项。"""

    if max_chars < 128:
        return '{"kind":"tool_result","data":{"truncated":true}}'
    value: object = content
    if isinstance(content, str):
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            return (
                content
                if len(content) <= max_chars
                else clip_text(content, int(max_chars / CHARS_PER_TOKEN))
            )

    def preserve_evidence_items(item: object, budget: int) -> object:
        if not isinstance(item, dict):
            return item
        data = item.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("evidence"), list):
            return item
        evidence = data["evidence"]
        base = dict(item)
        base_data = dict(data)
        base_data["evidence"] = []
        base["data"] = base_data
        accepted: list[object] = []
        for candidate in evidence:
            trial_data = dict(base_data)
            trial_data["evidence"] = [*accepted, candidate]
            trial = dict(base)
            trial["data"] = trial_data
            if len(json.dumps(trial, ensure_ascii=False, default=str)) <= budget:
                accepted.append(candidate)
                continue
            if not accepted and isinstance(candidate, dict):
                # 单个 Chunk 超过预算时保留来源元数据，不截断 Chunk 正文。
                metadata = {key: value for key, value in candidate.items() if key != "content"}
                metadata["content_omitted"] = True
                accepted.append(metadata)
            break
        base_data["evidence"] = accepted
        base["data"] = base_data
        return base

    value = preserve_evidence_items(value, max_chars)

    def trim(item: object, budget: int) -> object:
        if isinstance(item, str):
            return item if len(item) <= budget else item[: max(0, budget - 20)] + "...[裁剪]"
        if isinstance(item, list):
            result: list[object] = []
            used = 2
            for child in item:
                child_value = trim(child, max(256, min(6_000, budget - used)))
                encoded = json.dumps(child_value, ensure_ascii=False, default=str)
                if used + len(encoded) > budget and result:
                    break
                result.append(child_value)
                used += len(encoded) + 1
            return result
        if isinstance(item, dict):
            result_dict: dict[str, object] = {}
            used = 2
            for key, child in item.items():
                encoded_key = json.dumps(str(key), ensure_ascii=False)
                child_value = trim(child, max(256, min(6_000, budget - used)))
                encoded = json.dumps(child_value, ensure_ascii=False, default=str)
                addition = len(encoded_key) + len(encoded) + 2
                if used + addition > budget and result_dict:
                    continue
                result_dict[str(key)] = child_value
                used += addition
            return result_dict
        return item

    trimmed = trim(value, max_chars)
    encoded = json.dumps(trimmed, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return encoded if not isinstance(content, str) or value is not content else str(content)
    preview = encoded
    marker = {"kind": "tool_result", "data": {"truncated": True, "preview": preview}}
    marker_text = json.dumps(marker, ensure_ascii=False)
    while len(marker_text) > max_chars and preview:
        preview = preview[: max(0, len(preview) - max(64, len(marker_text) - max_chars))]
        marker["data"] = {"truncated": True, "preview": preview}
        marker_text = json.dumps(marker, ensure_ascii=False)
    return marker_text


def limit_tool_message_history(messages: list[AnyMessage]) -> list[AnyMessage]:
    remaining = TOOL_RESULT_TOTAL_CHARS
    result: list[AnyMessage] = list(messages)
    for index in range(len(result) - 1, -1, -1):
        item = result[index]
        if not isinstance(item, ToolMessage):
            continue
        if remaining < 128:
            clipped = '{"kind":"tool_result","data":{"truncated":true}}'
        else:
            clipped = limit_tool_content(item.content, min(TOOL_RESULT_MAX_CHARS, remaining))
        result[index] = item.model_copy(update={"content": clipped})
        remaining -= len(clipped)
        if remaining <= 0:
            break
    return result
