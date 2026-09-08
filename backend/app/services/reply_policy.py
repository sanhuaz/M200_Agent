"""Reply-length budgeting, semantic-boundary validation and one repair pass."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.domain.reply_types import (
    ReplyMode,
    ReplyPart,
    ReplyPlan,
    ReplyPolicyStatus,
    ReplyRepairFailure,
)
from app.services.models import model_registry

logger = logging.getLogger(__name__)

SHORT_MAX_CHARS = 30
SHORT_MAX_SEGMENTS = 3

_LONG_REQUEST_RE = re.compile(
    r"小说|长文|长篇|详细展开|详细说明|完整代码|代码全文|写一篇|报告|教程|逐步|一步一步|"
    r"指定字数|不少于\s*\d+\s*字|至少\s*\d+\s*字|不要省略|完整回答|全文"
)
_END_MARKS = frozenset("。！？!?；;…")
_CLOSING_MARKS = frozenset("”’\"'）)]】》』」")
_BOUNDARY_PUNCTUATION = frozenset("。！？!?；;")
_INVALID_END_RE = re.compile(r"(?:[,，:：、]|但|而|所以|因为|如果|以及|并且)$")
_INVALID_START_RE = re.compile(r"^(?:的|了|但|但是|而|而且|所以|因为|以及|并且)(?:[\s，。！？：:、]|$)")
_URL_RE = re.compile(
    r"https?://[^\s<>\u3000`，。！？；：、（）【】《》]+|"
    r"www\.[^\s<>\u3000`，。！？；：、（）【】《》]+",
    re.IGNORECASE,
)
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?(?:```|\Z)")
_INLINE_CODE_RE = re.compile(r"`[^`\r\n]+`")
_COMMAND_RE = re.compile(
    r"(?<!\S)(?:PS>\s*|(?:python(?:\.exe)?|pip|pnpm|npm|git|curl|"
    r"(?:cmd|powershell)(?:\.exe)?\s+/c\s+|(?:Get|Set|New|Remove|Start|Stop|"
    r"Test|Join|Resolve|Write|Read|Copy|Move|Select|ForEach|Where|Convert|"
    r"Invoke)-[A-Za-z-]+\s+))[^\r\n]+",
    re.MULTILINE,
)
_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\|\.{1,2}[\\/])[^\s<>\u3000`，。！？；：:、]+"
)
_MASK_RE = re.compile(r"\ue000(\d+)\ue001")
_ATOMIC_RE = re.compile(
    "|".join(
        f"(?:{pattern})"
        for pattern in (
            _FENCED_CODE_RE.pattern,
            _INLINE_CODE_RE.pattern,
            _COMMAND_RE.pattern,
            _URL_RE.pattern,
            _PATH_RE.pattern,
        )
    ),
    re.IGNORECASE | re.MULTILINE,
)


def is_long_output_request(user_text: str) -> bool:
    return bool(_LONG_REQUEST_RE.search(str(user_text or "")))


def _content_text(value: Any) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            str(item.get("text")) if isinstance(item, dict) and item.get("text") is not None else str(item)
            for item in content
        ).strip()
    return str(content or "").strip()


def _trim_atom(value: str) -> str:
    return value.rstrip("。！？!?；;，,、：:）)]】》』」")


def _mask_atoms(text: str) -> tuple[str, tuple[str, ...]]:
    atoms: list[str] = []
    masked = text

    def replace_atomic(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw.startswith(("http://", "https://", "www.", "\\\\", "./", "../")) or re.match(
            r"^[A-Za-z]:[\\/]", raw
        ):
            atom = _trim_atom(raw)
        else:
            atom = raw
        suffix = raw[len(atom) :]
        index = len(atoms)
        atoms.append(atom)
        return f"\ue000{index}\ue001{suffix}"

    # A single left-to-right pass keeps atomic_parts in the same order as the
    # candidate and prevents URLs or paths inside code from being extracted
    # again.
    masked = _ATOMIC_RE.sub(replace_atomic, masked)
    return masked, tuple(atom for atom in atoms if atom)


def extract_atomic_parts(text: str) -> tuple[str, tuple[str, ...]]:
    """Replace copy-sensitive content with masks while retaining its order."""

    return _mask_atoms(str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip())


def _split_sentences(masked: str) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    for character in masked:
        current.append(character)
        if character in _END_MARKS or character == "\n":
            while current and current[-1] in _CLOSING_MARKS:
                # Closing punctuation is already appended by the loop; this
                # branch only exists for readability around the boundary.
                break
            piece = "".join(current).strip()
            if piece:
                pieces.append(piece)
            current = []
    tail = "".join(current).strip()
    if tail:
        pieces.append(tail)
    return pieces


def _expand_masks(piece: str, atoms: tuple[str, ...]) -> list[ReplyPart]:
    parts: list[ReplyPart] = []
    position = 0
    for match in _MASK_RE.finditer(piece):
        before = piece[position : match.start()].strip()
        if before:
            parts.append(ReplyPart("segment", before))
        index = int(match.group(1))
        if 0 <= index < len(atoms):
            parts.append(ReplyPart("atomic", atoms[index]))
        position = match.end()
    after = piece[position:].strip()
    if after:
        parts.append(ReplyPart("segment", after))
    return parts


def _merge_punctuation(parts: Iterable[ReplyPart]) -> tuple[ReplyPart, ...]:
    merged: list[ReplyPart] = []
    for part in parts:
        if part.kind == "segment" and part.text in _BOUNDARY_PUNCTUATION:
            if merged and merged[-1].kind == "segment":
                previous = merged.pop()
                merged.append(ReplyPart("segment", previous.text + part.text))
            else:
                merged.append(part)
            continue
        merged.append(part)
    return tuple(merged)


def _parts_from_candidate(candidate: str) -> tuple[ReplyPart, ...]:
    masked, atoms = extract_atomic_parts(candidate)
    return _merge_punctuation(
        part
        for sentence in _split_sentences(masked)
        for part in _expand_masks(sentence, atoms)
    )


def _plan_from_parts(mode: ReplyMode, parts: Iterable[ReplyPart]) -> ReplyPlan:
    return _build_plan(mode, parts)


def _build_plan(
    mode: ReplyMode,
    parts: Iterable[ReplyPart],
    *,
    policy_status: ReplyPolicyStatus = "accepted",
    policy_violations: tuple[str, ...] = (),
    repair_failure_code: ReplyRepairFailure | None = None,
    source_text: str | None = None,
) -> ReplyPlan:
    normalized = tuple(part for part in parts if part.text.strip())
    return ReplyPlan(
        mode=mode,
        segments=tuple(
            part.text
            for part in normalized
            if part.kind == "segment" and part.text not in _BOUNDARY_PUNCTUATION
        ),
        atomic_parts=tuple(part.text for part in normalized if part.kind == "atomic"),
        parts=normalized,
        policy_status=policy_status,
        policy_violations=policy_violations,
        repair_failure_code=repair_failure_code,
        source_text=source_text,
    )


def _balanced(text: str) -> bool:
    pairs = {"（": "）", "(": ")", "【": "】", "[": "]", "《": "》", "“": "”", "\"": "\""}
    stack: list[str] = []
    for character in text:
        if character in pairs:
            if character == '"' and stack and stack[-1] == '"':
                stack.pop()
            else:
                stack.append(character)
        elif character in pairs.values():
            if not stack or pairs[stack[-1]] != character:
                return False
            stack.pop()
    return not stack


def _validate_reply_plan(
    plan: ReplyPlan,
    *,
    max_segments: int = SHORT_MAX_SEGMENTS,
    include_budget: bool,
) -> tuple[str, ...]:
    violations: list[str] = []
    if not plan.parts or (not plan.segments and not plan.atomic_parts):
        violations.append("empty")
        return tuple(violations)
    if include_budget and plan.mode == "short":
        if plan.segments and not 1 <= len(plan.segments) <= max_segments:
            violations.append("segment_count")
        if any(
            not 1 <= len(item) <= SHORT_MAX_CHARS
            for item in plan.segments
        ):
            violations.append("segment_length")
    for segment in plan.segments:
        if _INVALID_END_RE.search(segment) or _INVALID_START_RE.search(segment):
            violations.append("semantic_boundary")
        if not _balanced(segment):
            violations.append("unbalanced_punctuation")
    if any(not item for item in plan.atomic_parts):
        violations.append("empty_atomic")
    if any(
        _URL_RE.search(segment)
        or _FENCED_CODE_RE.search(segment)
        or _INLINE_CODE_RE.search(segment)
        or _COMMAND_RE.search(segment)
        or _PATH_RE.search(segment)
        for segment in plan.segments
    ):
        violations.append("atomic_in_segment")
    if any(not _looks_atomic(item) for item in plan.atomic_parts):
        violations.append("invalid_atomic")
    return tuple(dict.fromkeys(violations))


def validate_reply_plan(plan: ReplyPlan, *, max_segments: int = SHORT_MAX_SEGMENTS) -> tuple[str, ...]:
    """Validate structural sendability; short-output budgets remain soft goals."""

    return _validate_reply_plan(plan, max_segments=max_segments, include_budget=False)


def _reply_policy_violations(
    plan: ReplyPlan,
    *,
    max_segments: int,
) -> tuple[str, ...]:
    """Return all issues that can trigger the single optional repair pass."""

    return _validate_reply_plan(plan, max_segments=max_segments, include_budget=True)


def _looks_atomic(value: str) -> bool:
    return bool(
        _URL_RE.fullmatch(value)
        or _FENCED_CODE_RE.fullmatch(value)
        or _INLINE_CODE_RE.fullmatch(value)
        or _COMMAND_RE.fullmatch(value)
        or _PATH_RE.fullmatch(value)
        or value.startswith(
            (
                "$ ",
                "PS> ",
                "python ",
                "python.exe ",
                "pip ",
                "pnpm ",
                "npm ",
                "git ",
                "curl ",
                "Invoke-",
            )
        )
    )


def _json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _normalize_reply_text(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _repair_reply(
    model_alias: str,
    user_text: str,
    candidate: str,
    violations: tuple[str, ...],
    *,
    mode: ReplyMode,
    max_segments: int,
    time_context: str,
) -> tuple[ReplyPlan | None, ReplyRepairFailure | None]:
    system_prompt = (
        "你是 PersonalAgent 的回复结构化修订器。只输出一个 JSON 对象，不要解释、Markdown 或代码围栏。"
        "JSON 必须只包含 text 字符串，text 是完整的修订后回复。"
        "不要改写、删除或重新排序原回复中的 URL、代码、命令或路径。"
        f"当前时间上下文：{time_context or '未提供'}。"
    )
    if mode == "short":
        system_prompt += (
            f"尽量将自然语言控制在 1-{max_segments} 个完整段落、每段 1-{SHORT_MAX_CHARS} 个 Unicode 字符；"
            "不得在逗号、冒号或承接词处结束，不得拆英文短语、数字单位或原子内容。"
        )
    else:
        system_prompt += "长输出可以超过短回复字数预算，但句子、代码、命令、路径和 URL 必须完整。"
    human_prompt = (
        f"用户请求：\n{user_text[-8_000:]}\n\n"
        f"候选回复：\n{candidate[-12_000:]}\n\n"
        f"需要修正的内部问题：{', '.join(violations) or 'format'}\n"
        '输出示例结构：{"text":"完整句子。"}'
    )
    try:
        profile = model_registry.profile(model_alias)
        model = model_registry.chat_model(model_alias)
        if "api.deepseek.com" in profile.base_url:
            model = model.model_copy(update={"extra_body": {"thinking": {"type": "disabled"}}})
        response = model.bind(max_tokens=1_200).invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
        )
        value = _json_object(_content_text(response))
        if value is None:
            logger.warning("回复策略修订失败: invalid_json")
            return None, "invalid_json"
        raw_text = value.get("text")
        if not isinstance(raw_text, str) or not _normalize_reply_text(raw_text):
            logger.warning("回复策略修订失败: invalid_structure")
            return None, "invalid_structure"
        repaired_text = _normalize_reply_text(raw_text)
        original_atoms = extract_atomic_parts(candidate)[1]
        repaired_atoms = extract_atomic_parts(repaired_text)[1]
        if repaired_atoms != original_atoms:
            logger.warning("回复策略修订失败: atomic_changed")
            return None, "atomic_changed"
        plan = _plan_from_parts(mode, _parts_from_candidate(repaired_text))
        if validate_reply_plan(plan, max_segments=max_segments):
            logger.warning("回复策略修订失败: validation_failed")
            return None, "validation_failed"
        return plan, None
    except Exception as error:
        logger.warning("回复策略修订失败: %s", type(error).__name__)
        return None, "model_error"


class EmptyModelReplyError(ValueError):
    """Raised when the model returns no usable text at all."""


def prepare_reply(
    candidate: str,
    user_text: str,
    model_alias: str,
    *,
    max_segments: int = SHORT_MAX_SEGMENTS,
    time_context: str = "",
) -> ReplyPlan:
    """Validate a candidate, repair it once, then preserve any non-empty original."""

    mode: ReplyMode = "long" if is_long_output_request(user_text) else "short"
    max_segments = max(1, min(int(max_segments), SHORT_MAX_SEGMENTS))
    normalized_candidate = _normalize_reply_text(candidate)
    if not normalized_candidate:
        raise EmptyModelReplyError("模型未返回有效内容，请重试")
    parts = _parts_from_candidate(normalized_candidate)
    plan = _plan_from_parts(mode, parts)
    violations = _reply_policy_violations(plan, max_segments=max_segments)
    if not violations:
        return plan
    repaired, failure_code = _repair_reply(
        model_alias,
        user_text,
        normalized_candidate,
        violations,
        mode=mode,
        max_segments=max_segments,
        time_context=time_context,
    )
    if repaired is not None:
        return ReplyPlan(
            mode=repaired.mode,
            segments=repaired.segments,
            atomic_parts=repaired.atomic_parts,
            parts=repaired.parts,
            policy_status="repaired",
            policy_violations=violations,
        )
    return _build_plan(
        mode,
        parts,
        policy_status="original_preserved",
        policy_violations=violations,
        repair_failure_code=failure_code,
        source_text=normalized_candidate,
    )


def functional_reply_plan(candidate: str) -> ReplyPlan:
    """Build a complete-result plan without invoking policy repair.

    MCP evidence has its own delivery contract.  It still gets atomic
    extraction for QQ-safe boundaries, while ``source_text`` preserves the
    exact normalized body used by Message, Web and Markdown.
    """

    normalized_candidate = _normalize_reply_text(candidate)
    if not normalized_candidate:
        raise EmptyModelReplyError("模型未返回有效内容，请重试")
    parts = _parts_from_candidate(normalized_candidate)
    plan = _build_plan("long", parts, source_text=normalized_candidate)
    return plan


__all__ = [
    "SHORT_MAX_CHARS",
    "SHORT_MAX_SEGMENTS",
    "EmptyModelReplyError",
    "extract_atomic_parts",
    "functional_reply_plan",
    "is_long_output_request",
    "prepare_reply",
    "validate_reply_plan",
]
