from __future__ import annotations

import html
import re


def normalize_cq_text(value: str, *, preserve_mentions: bool = False) -> str:
    """Keep CQ text segments and remove attachments/receipts."""

    value = value.strip()
    if "[CQ:" not in value:
        return value

    def replace_segment(match: re.Match[str]) -> str:
        segment_type, payload = match.group(1), match.group(2) or ""
        if segment_type == "at" and preserve_mentions:
            return match.group(0)
        if segment_type != "text":
            return ""
        text_value = ""
        for part in payload.split(","):
            if part.startswith("text="):
                text_value = part[5:]
                break
        return html.unescape(
            text_value.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]")
        )

    return re.sub(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]", replace_segment, value).strip()


def text_from_event(event: dict[str, object]) -> str:
    raw_message = event.get("raw_message")
    if isinstance(raw_message, str) and raw_message.strip():
        return normalize_cq_text(
            raw_message, preserve_mentions=event.get("message_type") == "group"
        )
    message = event.get("message")
    if isinstance(message, str):
        return normalize_cq_text(message, preserve_mentions=event.get("message_type") == "group")
    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            segment_type = segment.get("type")
            data = segment.get("data")
            if isinstance(data, dict):
                value = data.get("text")
                if segment_type == "text" and isinstance(value, str):
                    parts.append(value)
                elif event.get("message_type") == "group" and segment_type == "at":
                    qq = str(data.get("qq") or "")
                    parts.append(f"[CQ:at,qq={qq}]")
        return "".join(parts).strip()
    return ""
