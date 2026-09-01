"""Shared UTC storage and local-time semantics for model and API contexts."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
_ROLE_LABELS = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}
_FUZZY_RELATIVE_TERMS = ("前阵子", "以前", "最近")


def resolve_timezone(name: str | None = None) -> ZoneInfo:
    timezone_name = DEFAULT_TIMEZONE if name is None else name.strip()
    if not timezone_name:
        raise ValueError("PERSONAL_AGENT_TIMEZONE 无效：不能为空")
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise ValueError(f"PERSONAL_AGENT_TIMEZONE 无效：{timezone_name}") from error


def configured_timezone() -> ZoneInfo:
    from app.core.config import get_settings

    return resolve_timezone(get_settings().personal_agent_timezone)


def _selected_timezone(name: str | None = None) -> ZoneInfo:
    return configured_timezone() if name is None else resolve_timezone(name)


def as_utc(value: datetime) -> datetime:
    """Normalize a database timestamp; naive values follow the UTC DB contract."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_local(value: datetime, timezone_name: str | None = None) -> datetime:
    return as_utc(value).astimezone(_selected_timezone(timezone_name))


def utc_isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return as_utc(value).isoformat().replace("+00:00", "Z")


def _utc_offset_text(value: datetime) -> str:
    offset = value.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def current_time_context(
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> str:
    timezone = _selected_timezone(timezone_name)
    local = as_utc(now or datetime.now(UTC)).astimezone(timezone)
    return (
        f"当前本地时间：{local:%Y-%m-%d %H:%M:%S}\n"
        f"当前时区：{timezone.key}（{_utc_offset_text(local)}）"
    )


def _relative_date_hint(text: str, local_date: date) -> str:
    terms: list[str] = []
    for term in ("上周", "前天", "昨天", "昨晚", "今天", "刚才", "明天", *_FUZZY_RELATIVE_TERMS):
        if term in text and term not in terms:
            terms.append(term)
    if not terms:
        return ""
    hints: list[str] = []
    for term in terms:
        if term in {"昨天", "昨晚"}:
            hints.append(f"“{term}”按 {local_date - timedelta(days=1):%Y-%m-%d} 解释")
        elif term == "前天":
            hints.append(f"“前天”按 {local_date - timedelta(days=2):%Y-%m-%d} 解释")
        elif term in {"今天", "刚才"}:
            hints.append(f"“{term}”按 {local_date:%Y-%m-%d} 解释")
        elif term == "明天":
            hints.append(f"“明天”按 {local_date + timedelta(days=1):%Y-%m-%d} 解释")
        elif term == "上周":
            week_start = local_date - timedelta(days=local_date.weekday() + 7)
            hints.append(
                f"“上周”按 {week_start:%Y-%m-%d} 至 {week_start + timedelta(days=6):%Y-%m-%d} 解释"
            )
        else:
            hints.append(f"“{term}”不精确化，仅保留本条消息的记录时间锚点 {local_date:%Y-%m-%d}")
    return "；".join(hints)


def format_message_for_model(
    role: str,
    content: str,
    created_at: datetime,
    timezone_name: str | None = None,
) -> str:
    local = to_local(created_at, timezone_name)
    label = _ROLE_LABELS.get(role, role)
    hint = _relative_date_hint(content, local.date())
    suffix = f"（时间锚点：{hint}）" if hint else ""
    return f"[{local:%Y-%m-%d %H:%M}] {label}：{content}{suffix}"


def format_messages_for_model(
    messages: Iterable[object],
    timezone_name: str | None = None,
) -> str:
    rendered: list[str] = []
    for item in messages:
        created_at = getattr(item, "created_at", None)
        if not isinstance(created_at, datetime):
            continue
        rendered.append(
            format_message_for_model(
                str(getattr(item, "role", "unknown")),
                str(getattr(item, "content", "")),
                created_at,
                timezone_name,
            )
        )
    return "\n".join(rendered)


def format_summary_for_model(summary: str, format_version: int | None) -> str:
    text = summary.strip()
    if not text:
        return "无"
    version = format_version if isinstance(format_version, int) and format_version >= 1 else 1
    if version >= 2:
        return f"[时间化摘要 v{version}]\n{text}"
    return f"[旧摘要 v{version}：时间不可靠，仅作背景，不据此判断当前状态]\n{text}"


def format_memory_for_model(item: object, timezone_name: str | None = None) -> str:
    created_at = getattr(item, "created_at", None)
    last_seen_at = getattr(item, "last_seen_at", None)
    created = to_local(created_at, timezone_name).strftime("%Y-%m-%d %H:%M") if created_at else "未知"
    last_seen = to_local(last_seen_at, timezone_name).strftime("%Y-%m-%d %H:%M") if last_seen_at else created
    kind_value = str(getattr(item, "memory_kind", "fact") or "fact")
    kind = kind_value if kind_value in {"fact", "event"} else "fact"
    event_date = getattr(item, "event_date", None)
    event_label = (
        f"事件日期 {event_date or '未精确记录'}；" if kind == "event" else ""
    )
    history_label = f"记忆类型 {kind}；{event_label}首次记录 {created}；最近确认 {last_seen}"
    historical_note = "（仅作历史背景，除非本轮重新确认，不视为当前状态）" if kind == "event" else ""
    return f"- [{history_label}] {getattr(item, 'content', '')}{historical_note}"


__all__ = [
    "DEFAULT_TIMEZONE",
    "as_utc",
    "configured_timezone",
    "current_time_context",
    "format_memory_for_model",
    "format_message_for_model",
    "format_messages_for_model",
    "format_summary_for_model",
    "resolve_timezone",
    "to_local",
    "utc_isoformat",
]
