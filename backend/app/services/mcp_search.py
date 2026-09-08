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
_REFRESH_WEB_RE = re.compile(
    r"(?:刷新|更新|重新查|再查|重新获取|再获取).{0,24}"
    r"(?:新闻|价格|价位|股价|汇率|政策|法规|规则|版本|发布|天气|赛事|比赛|航班|路线|"
    r"官网|资料|来源|排名|市场|股票|软件|框架|库|热搜|热榜|榜单|排行榜|舆情|趋势|动态|"
    r"网页|网站|页面|数据)",
)
_IMPLICIT_WEB_ACTION_RE = re.compile(
    r"告诉我|列出|获取|抓取|访问|打开|浏览|查看|查阅|帮我看看|看看|看一下|了解一下|"
    r"核实一下|确认一下|盘点一下|整理一下"
)
_STATUS_QUERY_RE = re.compile(
    r"(?:联网|上网|网页|搜索|查资料|anysearch).{0,16}"
    r"(?:功能|能力|可用|能用|可以|支持|权限|授权|连接|状态|吗|么|呢)"
    r"|(?:有没有|是否有|能否|能不能|可以|能).{0,12}"
    r"(?:联网|上网|搜索|查资料|外部(?:搜索|信息))",
    re.IGNORECASE,
)
_CURRENT_MARKER_RE = re.compile(r"最新|目前|当前|实时|今日|今天|本周|截至|最近发布|现在的|近期")
_CURRENT_TOPIC_RE = re.compile(
    r"新闻|价格|价位|股价|汇率|政策|法规|规则|版本|发布|天气|赛事|比赛|航班|路线|官网|资料|来源|排名|市场|股票|软件|框架|库|热搜|热榜|榜单|排行榜|舆情|趋势|动态|网页|网站|页面|数据"
)
_RANKING_REQUEST_RE = re.compile(
    r"(?:热搜|热榜|榜单|排行榜|排名|排行).{0,12}(?:前[一二三四五六七八九十百千\d]+|top\s*\d+)",
    re.IGNORECASE,
)
_DEFINITION_REQUEST_RE = re.compile(r"什么是|什么意思|解释|定义")


def search_intent(text: str) -> bool:
    """Return whether this turn explicitly asks for current/web evidence."""

    normalized = " ".join(str(text or "").split())
    if _DIRECT_SEARCH_RE.search(normalized):
        return True
    if _REFRESH_WEB_RE.search(normalized):
        return True
    if _RANKING_REQUEST_RE.search(normalized) and not _DEFINITION_REQUEST_RE.search(normalized):
        return True
    if _STATUS_QUERY_RE.search(normalized):
        return True
    has_current_marker = _CURRENT_MARKER_RE.search(normalized) is not None
    has_current_topic = _CURRENT_TOPIC_RE.search(normalized) is not None
    return bool(
        has_current_marker and has_current_topic
        or _IMPLICIT_WEB_ACTION_RE.search(normalized) and (has_current_marker or has_current_topic)
    )


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
