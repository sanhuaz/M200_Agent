from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MANGA_ACTIONS = frozenset({"search", "download", "delete"})

_SEARCH_RE = re.compile(r"搜索|搜|查找|查询|检索|找")
_DOWNLOAD_RE = re.compile(r"下载|保存(?:到本地)?")
_DELETE_RE = re.compile(r"删除|清理|移除")
_MANGA_ANCHOR_RE = re.compile(r"漫画|本子|jm(?:编号|号码|号|id)?")
_TASK_ID_RE = re.compile(r"(?:任务|下载任务|漫画任务|产物|文件)[^，。！？!?\s]{0,12}[a-z0-9][a-z0-9_-]{2,}")
_REQUEST_RE = re.compile(r"帮我|请|给我|我要|我想(?:要)?|麻烦|直接")
_META_RE = re.compile(
    r"^(?:你会|你能|能否|是否|怎么|如何|为什么|什么是|支持)"
    r"|(?:是什么|是什么意思|怎么(?:搜索|找|下载)|如何(?:搜索|找|下载)|"
    r"这个词|词义|含义|定义|意思)"
)
_NEGATION_RE = re.compile(
    r"(?:不要|别|不用|无需|不需要|不想|禁止).{0,12}"
    r"(?:搜索|搜|查找|查询|检索|找|下载|保存|删除|清理|移除)"
)
_FOLLOWUP_DOWNLOAD_RE = re.compile(
    r"(?:下载|保存)(?:刚才|上面|上一个|上面那个|这个|那个|它|这部|该部|第[一二三四五六七八九十0-9]+个)"
)


@dataclass(frozen=True, slots=True)
class MangaIntent:
    """当前消息可以临时开放的漫画动作。"""

    actions: frozenset[str]

    @property
    def has_manga_action(self) -> bool:
        return bool(self.actions)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def _is_explicit_request(
    text: str,
    action_match: re.Match[str] | None,
    *,
    action_context: bool = False,
) -> bool:
    if action_match is None:
        return False
    if _NEGATION_RE.search(text) or _META_RE.search(text):
        return False
    has_request_marker = bool(_REQUEST_RE.search(text))
    starts_with_action = action_match.start() <= 2
    if not (has_request_marker or starts_with_action or action_context):
        return False
    if text.endswith(("吗", "么", "？", "?")) and not has_request_marker:
        return False
    return True


def detect_manga_intent(
    text: str,
    *,
    previous_search_results: bool = False,
) -> MangaIntent:
    """用当前消息和上一轮搜索结果确定漫画工具的临时授权。"""

    normalized = _normalize(text)
    if not normalized:
        return MangaIntent(frozenset())

    actions: set[str] = set()
    anchor = _MANGA_ANCHOR_RE.search(normalized)
    search_match = _SEARCH_RE.search(normalized)
    download_match = _DOWNLOAD_RE.search(normalized)
    delete_match = _DELETE_RE.search(normalized)
    primary_match = min(
        (match for match in (search_match, download_match, delete_match) if match),
        key=lambda match: match.start(),
        default=None,
    )
    explicit_request = _is_explicit_request(normalized, primary_match)

    if anchor and _is_explicit_request(
        normalized, search_match, action_context=explicit_request
    ):
        actions.add("search")

    if anchor and _is_explicit_request(
        normalized, download_match, action_context=explicit_request
    ):
        actions.add("download")
    elif (
        previous_search_results
        and _FOLLOWUP_DOWNLOAD_RE.search(normalized)
        and _is_explicit_request(normalized, download_match)
    ):
        actions.add("download")

    if (
        anchor
        and _TASK_ID_RE.search(normalized)
        and _is_explicit_request(
            normalized, delete_match, action_context=explicit_request
        )
    ):
        actions.add("delete")

    return MangaIntent(frozenset(actions.intersection(MANGA_ACTIONS)))
