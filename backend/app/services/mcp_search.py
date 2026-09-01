"""Intent and call-boundary rules for the AnySearch MCP preset."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

ANYSEARCH_TOOL_NAMES = frozenset({"search", "get_sub_domains", "batch_search", "extract"})
_DIRECT_SEARCH_RE = re.compile(
    r"搜索|搜一下|查一下|查询|检索|上网查|联网查|帮我找|找一下|查资料|网页来源|事实核查"
)
_CURRENT_MARKER_RE = re.compile(r"最新|目前|当前|实时|今日|本周|截至|最近发布|现在的")
_CURRENT_TOPIC_RE = re.compile(
    r"新闻|价格|价位|股价|汇率|政策|法规|规则|版本|发布|天气|赛事|比赛|航班|路线|官网|资料|来源|排名|市场|股票|软件|框架|库"
)


def search_intent(text: str) -> bool:
    """Return whether this turn explicitly asks for current/web evidence."""

    normalized = " ".join(str(text or "").split())
    return bool(_DIRECT_SEARCH_RE.search(normalized) or (
        _CURRENT_MARKER_RE.search(normalized) and _CURRENT_TOPIC_RE.search(normalized)
    ))


def search_system_instruction(enabled: bool) -> str:
    if not enabled:
        return (
            "\n\n联网搜索默认关闭。本轮没有检测到明确的搜索或当前事实核验意图，"
            "不要调用 AnySearch，也不要主动把普通问题扩展为联网搜索。"
        )
    return (
        "\n\n本轮已检测到明确的联网搜索或当前事实核验意图。"
        "只有需要当前事实时才调用 AnySearch；普通聊天不要调用。"
        "使用垂直 domain 或 sub_domain 前，必须先调用 get_sub_domains，"
        "只使用其返回的合法 sub_domain 和参数，不得自行编造。"
        "batch_search 只用于多个彼此独立的查询，extract 只在确需网页正文时使用。"
        "优先使用最多两项直接相关来源；搜索失败、超时或结果异常时，明确说明当前信息未能核实，"
        "不得把旧知识或猜测伪装成实时搜索结果。"
    )


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _domain_from_sub_domain(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text.split(".", 1)[0].strip() or None


def _domains_for_search(name: str, arguments: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    if name == "search":
        values.extend([arguments.get("domain"), arguments.get("sub_domain")])
    elif name == "batch_search":
        values.extend([arguments.get("domain"), arguments.get("sub_domain")])
        for query in _as_list(arguments.get("queries")):
            if isinstance(query, dict):
                values.extend([query.get("domain"), query.get("sub_domain")])
    domains: set[str] = set()
    for value in values:
        if isinstance(value, list):
            domains.update(str(item).strip() for item in value if str(item).strip())
        elif value:
            text = str(value).strip()
            domain = _domain_from_sub_domain(text) if "." in text else text
            if domain:
                domains.add(domain)
    return {item for item in domains if item}


def _discovery_domains(arguments: dict[str, Any]) -> set[str]:
    values = [arguments.get("domain"), arguments.get("domains")]
    domains: set[str] = set()
    for value in values:
        if isinstance(value, list):
            domains.update(str(item).strip() for item in value if str(item).strip())
        elif value:
            domains.add(str(value).strip())
    return {item for item in domains if item}


def is_error_result(result: Any) -> bool:
    """Recognize MCP application errors without exposing their raw payload."""

    if isinstance(result, dict):
        return bool(result.get("isError") or result.get("is_error") or result.get("error"))
    return bool(getattr(result, "isError", False) or getattr(result, "is_error", False))


@dataclass
class AnySearchCallGuard:
    """Enforce the AnySearch vertical-discovery ordering within one turn."""

    discovered_domains: set[str] = field(default_factory=set)

    def before_call(self, name: str, arguments: dict[str, Any]) -> str | None:
        if name == "batch_search":
            queries = _as_list(arguments.get("queries"))
            if queries and len(queries) > 5:
                return "batch_search 每次最多接收 5 个独立查询"
        if name not in {"search", "batch_search"}:
            return None
        missing = _domains_for_search(name, arguments).difference(self.discovered_domains)
        if missing:
            return (
                "使用垂直搜索前必须先调用 get_sub_domains；"
                f"尚未发现 domain：{', '.join(sorted(missing))}。"
            )
        return None

    def after_call(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        if name == "get_sub_domains" and not is_error_result(result):
            self.discovered_domains.update(_discovery_domains(arguments))


__all__ = [
    "ANYSEARCH_TOOL_NAMES",
    "AnySearchCallGuard",
    "is_error_result",
    "search_intent",
    "search_system_instruction",
]
