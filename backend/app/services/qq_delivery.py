from __future__ import annotations

import math
import re

DEFAULT_CHUNK_TARGET_CHARS = 20
MIN_CHUNK_TARGET_CHARS = 5
MAX_CHUNK_TARGET_CHARS = 100
MAX_REPLY_CHUNKS = 12
MIN_DELAY_SECONDS = 0.3
MAX_DELAY_SECONDS = 0.8

_END_PUNCTUATION = frozenset("。！？!?；;….")
_SECONDARY_PUNCTUATION = frozenset("，,、：:")
_CLOSING_PUNCTUATION = frozenset("”’\"'）)]】》』」")
_PROTECTED_PATTERN = re.compile(
    r"```[\s\S]*?(?:```|$)|https?://[^\s<>\u3000`。！？；，、：]+|www\.[^\s<>\u3000`。！？；，、：]+"
)
_MASK_PATTERN = re.compile("\\ue000\\d+\\ue001")


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


def _visible_length(masked: str, protected: list[str]) -> int:
    return len(_restore(masked, protected))


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


def _split_secondary(masked: str, protected: list[str], minimum: int) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    for atom, is_protected in _atoms(masked):
        current.append(atom)
        if not is_protected and (atom in _SECONDARY_PUNCTUATION or atom.isspace()):
            if _visible_length("".join(current), protected) >= minimum:
                pieces.append("".join(current))
                current = []
    if current:
        pieces.append("".join(current))
    return pieces or [masked]


def _hard_split(masked: str, protected: list[str], maximum: int) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    for atom, is_protected in _atoms(masked):
        if is_protected and _visible_length(atom, protected) > maximum:
            if current:
                pieces.append("".join(current))
                current = []
            pieces.append(atom)
            continue
        candidate = "".join(current) + atom
        if current and _visible_length(candidate, protected) > maximum:
            pieces.append("".join(current))
            current = [atom]
        else:
            current.append(atom)
    if current:
        pieces.append("".join(current))
    return pieces


def _merge_short_pieces(
    pieces: list[str], protected: list[str], minimum: int, maximum: int
) -> list[str]:
    merged: list[str] = []
    for piece in pieces:
        if not piece.strip():
            continue
        if merged and _visible_length(merged[-1] + piece, protected) <= maximum:
            merged[-1] += piece
        else:
            merged.append(piece)
    if len(merged) > 1 and _visible_length(merged[0], protected) < minimum:
        if _visible_length(merged[0] + merged[1], protected) <= maximum:
            merged[1] = merged[0] + merged[1]
            merged.pop(0)
    return merged


def _limit_chunks(pieces: list[str], protected: list[str]) -> list[str]:
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
    """Split a reviewed model reply into a small number of natural QQ messages."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    minimum, maximum = chunk_length_bounds(target_chars)
    masked, protected = _mask_protected(normalized)
    if _visible_length(masked, protected) <= maximum:
        return [normalized]

    pieces: list[str] = []
    for sentence in _split_sentences(masked):
        if _visible_length(sentence, protected) <= maximum:
            pieces.append(sentence)
            continue
        for secondary in _split_secondary(sentence, protected, minimum):
            if _visible_length(secondary, protected) <= maximum:
                pieces.append(secondary)
            else:
                pieces.extend(_hard_split(secondary, protected, maximum))

    pieces = _merge_short_pieces(pieces, protected, minimum, maximum)
    pieces = _limit_chunks(pieces, protected)
    # Keep whitespace at internal chunk boundaries.  Trimming every chunk would
    # silently concatenate words (for example, ``"hello world"`` becoming
    # ``"helloworld"`` when the split falls on a space).  The whole input was
    # already normalized at the boundary above, so only whitespace-only pieces
    # are discarded here.
    restored = [_restore(piece, protected) for piece in pieces if piece.strip()]
    return restored or [normalized]


def qq_reply_delay_seconds(text: str) -> float:
    """Return a deterministic pause before the next QQ message."""

    return min(MAX_DELAY_SECONDS, max(MIN_DELAY_SECONDS, MIN_DELAY_SECONDS + min(len(text), 50) * 0.01))
