from __future__ import annotations

import math
import re
from typing import cast

from app.domain.reply_types import ReplyMode, ReplyPart, ReplyPlan, ReplyPolicyStatus

DEFAULT_CHUNK_TARGET_CHARS = 20
MIN_CHUNK_TARGET_CHARS = 5
MAX_CHUNK_TARGET_CHARS = 30
MAX_REPLY_CHUNKS = 12
MIN_DELAY_SECONDS = 0.3
MAX_DELAY_SECONDS = 0.8

_END_PUNCTUATION = frozenset("。！？!?；;….")
_CLOSING_PUNCTUATION = frozenset("”’\"'）)]】》』」")
_PROTECTED_PATTERN = re.compile(
    r"```[\s\S]*?(?:```|\Z)|`[^`\r\n]+`|"
    r"(?<!\S)(?:PS>\s*|(?:python(?:\.exe)?|pip|pnpm|npm|git|curl|"
    r"(?:cmd|powershell)(?:\.exe)?\s+/c\s+|(?:Get|Set|New|Remove|Start|Stop|"
    r"Test|Join|Resolve|Write|Read|Copy|Move|Select|ForEach|Where|Convert|"
    r"Invoke)-[A-Za-z-]+\s+))[^\r\n]+|"
    r"https?://[^\s<>\u3000`，。！？；：、（）【】《》]+|"
    r"www\.[^\s<>\u3000`，。！？；：、（）【】《》]+|"
    r"(?<![\w])[A-Za-z]:[\\/][^\s<>\u3000`，。！？；：:、]+|"
    r"(?<![\w])(?:\\\\|\.{1,2}[\\/])[^\s<>\u3000`，。！？；：:、]+",
    re.IGNORECASE | re.MULTILINE,
)
_MASK_PATTERN = re.compile("\\ue000\\d+\\ue001")
_PLAN_BOUNDARY_PUNCTUATION = frozenset("。！？!?；;")


def normalize_chunk_target(value: object) -> int:
    """Return a safe target length for user-configured QQ chunks."""

    try:
        target = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_TARGET_CHARS
    if not MIN_CHUNK_TARGET_CHARS <= target <= MAX_CHUNK_TARGET_CHARS:
        return DEFAULT_CHUNK_TARGET_CHARS
    return target


def chunk_length_bounds(target_chars: object) -> tuple[int, int]:
    """Calculate the soft semantic range around a target character count."""

    target = normalize_chunk_target(target_chars)
    minimum = max(1, math.floor(target * 0.7))
    maximum = max(minimum, math.ceil(target * 1.3))
    return minimum, maximum


def _mask_protected(text: str) -> tuple[str, list[str]]:
    protected: list[str] = []

    def replace(match: re.Match[str]) -> str:
        index = len(protected)
        protected.append(match.group(0))
        return f"\ue000{index}\ue001"

    return _PROTECTED_PATTERN.sub(replace, text), protected


def _restore(text: str, protected: list[str]) -> str:
    return _MASK_PATTERN.sub(lambda match: protected[int(match.group(0)[1:-1])], text)


def _atoms(text: str) -> list[tuple[str, bool]]:
    atoms: list[tuple[str, bool]] = []
    position = 0
    for match in _MASK_PATTERN.finditer(text):
        atoms.extend((character, False) for character in text[position : match.start()])
        atoms.append((match.group(0), True))
        position = match.end()
    atoms.extend((character, False) for character in text[position:])
    return atoms


def _split_sentences(masked: str) -> list[str]:
    atoms = _atoms(masked)
    pieces: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(atoms):
        atom, is_protected = atoms[index]
        current.append(atom)
        if not is_protected and atom in _END_PUNCTUATION:
            index += 1
            while index < len(atoms):
                closing, closing_is_protected = atoms[index]
                if closing_is_protected or closing not in _CLOSING_PUNCTUATION:
                    break
                current.append(closing)
                index += 1
            pieces.append("".join(current))
            current = []
            continue
        if not is_protected and atom == "\n":
            pieces.append("".join(current))
            current = []
        index += 1
    if current:
        pieces.append("".join(current))
    return [piece for piece in pieces if piece.strip()]


def _limit_chunks(pieces: list[str]) -> list[str]:
    if len(pieces) <= MAX_REPLY_CHUNKS:
        return pieces
    grouped: list[str] = []
    total = len(pieces)
    for index in range(MAX_REPLY_CHUNKS):
        start = round(index * total / MAX_REPLY_CHUNKS)
        end = round((index + 1) * total / MAX_REPLY_CHUNKS)
        if start < end:
            grouped.append("".join(pieces[start:end]))
    return grouped


def split_qq_reply(text: str, target_chars: object = DEFAULT_CHUNK_TARGET_CHARS) -> list[str]:
    """Compatibility splitter that only honors complete sentences or paragraphs."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    normalize_chunk_target(target_chars)
    masked, protected = _mask_protected(normalized)
    pieces = _limit_chunks(_split_sentences(masked))
    restored = [_restore(piece, protected) for piece in pieces if piece.strip()]
    return restored or [normalized]


def plan_delivery_parts(
    plan: ReplyPlan,
    target_chars: object = DEFAULT_CHUNK_TARGET_CHARS,
) -> list[str]:
    """Return reviewed parts, or semantically split a preserved original."""

    if plan.policy_status == "original_preserved":
        return split_qq_reply(plan.text, target_chars=target_chars)

    chunks: list[str] = []
    for part in plan.parts:
        text = part.text
        if not text.strip():
            continue
        if part.kind == "segment" and text in _PLAN_BOUNDARY_PUNCTUATION and chunks:
            chunks[-1] += text
        else:
            chunks.append(text)
    return chunks or ([plan.text] if plan.text else [])


def reply_plan_from_event(data: dict[str, object]) -> ReplyPlan | None:
    """Rebuild the validated plan carried by a ChatService final event."""

    mode = data.get("response_mode")
    if mode == "short":
        reply_mode: ReplyMode = "short"
    elif mode == "long":
        reply_mode = "long"
    else:
        return None
    raw_policy_status = data.get("reply_policy_status", "accepted")
    if raw_policy_status not in {"accepted", "repaired", "original_preserved"}:
        return None
    policy_status = cast(ReplyPolicyStatus, raw_policy_status)
    raw_parts = data.get("parts")
    if not isinstance(raw_parts, list):
        return None
    parts: list[ReplyPart] = []
    for item in raw_parts:
        if not isinstance(item, dict):
            return None
        kind = item.get("kind")
        text = item.get("text")
        if kind not in {"segment", "atomic"} or not isinstance(text, str) or not text.strip():
            return None
        parts.append(
            ReplyPart("segment" if kind == "segment" else "atomic", text)
        )
    segments = data.get("segments")
    atomic_parts = data.get("atomic_parts")
    if not isinstance(segments, list) or not all(isinstance(item, str) for item in segments):
        return None
    if not isinstance(atomic_parts, list) or not all(
        isinstance(item, str) for item in atomic_parts
    ):
        return None
    expected_text = data.get("text")
    source_text = (
        expected_text
        if policy_status == "original_preserved" and isinstance(expected_text, str)
        else None
    )
    plan = ReplyPlan(
        reply_mode,
        tuple(segments),
        tuple(atomic_parts),
        tuple(parts),
        policy_status=policy_status,
        source_text=source_text,
    )
    if isinstance(expected_text, str) and expected_text != plan.text:
        return None
    return plan


def qq_reply_delay_seconds(text: str) -> float:
    """Return a deterministic pause before the next QQ message."""

    return min(MAX_DELAY_SECONDS, max(MIN_DELAY_SECONDS, MIN_DELAY_SECONDS + min(len(text), 50) * 0.01))
