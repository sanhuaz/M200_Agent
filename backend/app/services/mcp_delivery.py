"""Normalize successful MCP evidence into the functional reply channel."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.services.chat_support import normalize_tool_result

_FAILED_CODES = {
    "realtime_search_failed",
    "vertical_domain_discovery_required",
}
_FILE_REQUEST_RE = re.compile(
    r"文件|markdown|\.md\b|附件|下载|发给我|发我|发送|送给我|导出",
    re.IGNORECASE,
)
_LIST_REQUEST_RE = re.compile(
    r"榜单|清单|列表|热搜|热榜|排行|排名|top|前[五六七八九十百千\d]+",
    re.IGNORECASE,
)
_ITEM_LINE_RE = re.compile(r"^(?:\d{1,4}[.)、．]|[-*•])\s*")
_TOP_N_RE = re.compile(r"(?:top\s*|前\s*)(\d+)", re.IGNORECASE)
_TOP_CHINESE_RE = re.compile(r"前(十|百|千|五|六|七|八|九)")
_CHINESE_DIGITS = {"五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000}


@dataclass(frozen=True, slots=True)
class McpEvidence:
    tool_name: str
    body: str


def _render_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        rendered: list[str] = []
        for item in value:
            rendered.extend(_render_value(item))
        return rendered
    if isinstance(value, dict):
        if not value:
            return []
        if value.get("text") is not None:
            return _render_value(value["text"])
        if value.get("data") is not None:
            return _render_value(value["data"])
        return [json.dumps(value, ensure_ascii=False, indent=2)]
    if value is None:
        return []
    return [str(value)]


def normalize_mcp_result(content: object, tool_name: str) -> McpEvidence | None:
    """Return user-facing evidence only for a non-empty successful MCP result."""

    if not str(tool_name or "").startswith("mcp__"):
        return None
    payload = normalize_tool_result(content)
    if payload.get("pending_confirmation"):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        if data.get("truncated") is True or data.get("code") in _FAILED_CODES or data.get("error"):
            return None
        values: list[str] = []
        if data.get("content") is not None:
            values.extend(_render_value(data.get("content")))
        elif data.get("structuredContent") is not None:
            values.extend(_render_value(data.get("structuredContent")))
        elif data.get("raw") is not None:
            values.extend(_render_value(data.get("raw")))
        else:
            values.extend(
                _render_value(
                    {
                        key: value
                        for key, value in data.items()
                        if key not in {"server_item", "artifacts"}
                    }
                )
            )
    else:
        values = _render_value(data)
    body = "\n\n".join(item.strip() for item in values if item.strip()).strip()
    return McpEvidence(str(tool_name), body) if body else None


def aggregate_mcp_evidence(evidence: Iterable[McpEvidence]) -> str:
    """Preserve ToolMessage order while making each result visibly distinct."""

    return "\n\n".join(item.body for item in evidence if item.body.strip()).strip()


def _top_n(text: str) -> int:
    numeric = [int(match.group(1)) for match in _TOP_N_RE.finditer(text)]
    chinese = [_CHINESE_DIGITS[match.group(1)] for match in _TOP_CHINESE_RE.finditer(text)]
    return max([0, *numeric, *chinese])


def should_create_markdown(user_text: str, body: str) -> bool:
    """Apply the fixed explicit/file, size and list thresholds."""

    text = str(user_text or "")
    if _FILE_REQUEST_RE.search(text):
        return True
    if len(re.findall(r"[\u4e00-\u9fff]", body)) > 600:
        return True
    if not _LIST_REQUEST_RE.search(text):
        return False
    if _top_n(text) >= 5:
        return True
    lines = [line.strip() for line in str(body or "").splitlines() if line.strip()]
    marked_items = sum(1 for line in lines if _ITEM_LINE_RE.match(line))
    return marked_items >= 5 or len(lines) >= 5


__all__ = [
    "McpEvidence",
    "aggregate_mcp_evidence",
    "normalize_mcp_result",
    "should_create_markdown",
]
