from __future__ import annotations

import asyncio
import html
import ipaddress
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.domain.chat_inputs import ChatAttachmentInput
from app.services.chat_attachments import MAX_ATTACHMENTS, MAX_TOTAL_BYTES

OneBotAction = Callable[..., Awaitable[dict[str, object]]]
MAX_DOWNLOAD_BYTES = MAX_TOTAL_BYTES
DOWNLOAD_TIMEOUT_SECONDS = 20.0
_CQ_IMAGE_RE = re.compile(r"\[CQ:image(?:,([^\]]*))?\]", re.IGNORECASE)


class OneBotMediaError(ValueError):
    """A current OneBot image cannot be safely downloaded or decoded."""


@dataclass(frozen=True, slots=True)
class OneBotImageRef:
    file: str | None = None
    url: str | None = None
    filename: str = "image"


def _unescape(value: str) -> str:
    return html.unescape(value.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]"))


def _segment_values(payload: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in payload.split(","):
        key, separator, value = part.partition("=")
        if separator and key:
            values[key.strip().lower()] = _unescape(value)
    return values


def _ref_from_values(values: dict[str, str]) -> OneBotImageRef:
    file_value = values.get("file") or values.get("file_id") or values.get("path")
    url = values.get("url")
    filename = file_value or "image"
    return OneBotImageRef(file=file_value, url=url, filename=filename)


def extract_image_refs(event: dict[str, object]) -> tuple[OneBotImageRef, ...]:
    """Extract only image segments belonging to the current OneBot event."""

    refs: list[OneBotImageRef] = []
    raw_message = event.get("raw_message")
    if isinstance(raw_message, str):
        for match in _CQ_IMAGE_RE.finditer(raw_message):
            refs.append(_ref_from_values(_segment_values(match.group(1) or "")))
    message = event.get("message")
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or str(segment.get("type")) != "image":
                continue
            data = segment.get("data")
            if isinstance(data, dict):
                values = {str(key).lower(): str(value) for key, value in data.items() if value is not None}
                refs.append(_ref_from_values(values))
    unique: list[OneBotImageRef] = []
    seen: set[tuple[str | None, str | None]] = set()
    for ref in refs:
        key = (ref.file, ref.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    if len(unique) > MAX_ATTACHMENTS:
        raise OneBotMediaError(f"每条 QQ 消息最多处理 {MAX_ATTACHMENTS} 张图片")
    return tuple(unique)


def _is_allowed_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            return False
        if parsed.username or parsed.password or parsed.fragment:
            return False
        if host in {"localhost", "localhost.localdomain"}:
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return bool(address.is_private or address.is_loopback or address.is_link_local)
    except ValueError:
        return False


async def _read_local_file(value: str) -> bytes | None:
    path = Path(value)
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError as error:
        raise OneBotMediaError("QQ 图片文件不可读取") from error
    if size <= 0 or size > MAX_DOWNLOAD_BYTES:
        raise OneBotMediaError("QQ 图片超过大小限制")
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError as error:
        raise OneBotMediaError("QQ 图片文件不可读取") from error


async def _download_url(value: str) -> bytes:
    if not _is_allowed_url(value):
        raise OneBotMediaError("QQ 图片来源不是受控的 OneBot/NapCat 地址")
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("GET", value) as response:
                if response.status_code != 200:
                    raise OneBotMediaError("QQ 图片下载失败")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_DOWNLOAD_BYTES:
                            raise OneBotMediaError("QQ 图片超过大小限制")
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise OneBotMediaError("QQ 图片超过大小限制")
                    chunks.append(chunk)
                if not chunks:
                    raise OneBotMediaError("QQ 图片文件为空")
                return b"".join(chunks)
    except OneBotMediaError:
        raise
    except (httpx.HTTPError, OSError) as error:
        raise OneBotMediaError("QQ 图片下载失败") from error


def _response_candidates(response: dict[str, object]) -> list[str]:
    data = response.get("data")
    if not isinstance(data, dict):
        return []
    values: list[str] = []
    for key in ("file", "path", "url"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


async def _load_candidate(value: str) -> bytes | None:
    local = await _read_local_file(value)
    if local is not None:
        return local
    if value.lower().startswith(("http://", "https://")):
        return await _download_url(value)
    return None


async def _load_ref(ref: OneBotImageRef, action: OneBotAction) -> bytes:
    errors: list[OneBotMediaError] = []
    if ref.file:
        try:
            response = await action("get_image", {"file": ref.file}, timeout_seconds=DOWNLOAD_TIMEOUT_SECONDS)
            if response.get("status") == "ok" and int(str(response.get("retcode", 0))) == 0:
                for candidate in _response_candidates(response):
                    try:
                        content = await _load_candidate(candidate)
                    except OneBotMediaError as error:
                        errors.append(error)
                        continue
                    if content is not None:
                        return content
        except Exception as error:
            errors.append(OneBotMediaError(f"get_image {type(error).__name__}"))
    for candidate in (ref.file, ref.url):
        if not candidate:
            continue
        try:
            content = await _load_candidate(candidate)
        except OneBotMediaError as error:
            errors.append(error)
            continue
        if content is not None:
            return content
    raise errors[-1] if errors else OneBotMediaError("QQ 图片来源不可用")


async def fetch_attachments(
    event: dict[str, object], action: OneBotAction
) -> tuple[ChatAttachmentInput, ...]:
    """Download current-event images into transport-neutral chat inputs."""

    refs = extract_image_refs(event)
    attachments: list[ChatAttachmentInput] = []
    total = 0
    for ref in refs:
        content = await _load_ref(ref, action)
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise OneBotMediaError("每条 QQ 消息图片总大小不能超过 32 MiB")
        attachments.append(
            ChatAttachmentInput(
                content=content,
                original_filename=Path(ref.filename).name or "image",
                source="onebot",
            )
        )
    return tuple(attachments)
