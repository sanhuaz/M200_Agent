from __future__ import annotations

import base64
import logging
import os
import re
import tempfile
import uuid
import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Message, MessageAttachment, new_id
from app.domain.chat_inputs import ChatAttachmentInput
from app.services.time_context import utc_isoformat

logger = logging.getLogger(__name__)

MAX_ATTACHMENTS = 4
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 40_000_000

_FORMAT_INFO = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "GIF": ("image/gif", ".gif"),
    "WEBP": ("image/webp", ".webp"),
}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class AttachmentValidationError(ValueError):
    """Attachment input is invalid and must not create database or file state."""


@dataclass(frozen=True, slots=True)
class ValidatedAttachment:
    content: bytes
    original_filename: str
    mime_type: str
    byte_size: int
    width: int
    height: int
    extension: str
    source: str


@dataclass(slots=True)
class PreparedAttachmentBatch:
    """Validated temporary files which can be atomically persisted with a message."""

    root: Path
    items: tuple[ValidatedAttachment, ...]
    temporary_paths: list[Path] = field(default_factory=list)
    final_paths: list[Path] = field(default_factory=list)
    committed: bool = False

    def persist(self, session: Session, message: Message) -> list[MessageAttachment]:
        if self.committed:
            raise RuntimeError("附件批次已经提交")
        if len(self.temporary_paths) != len(self.items):
            raise RuntimeError("附件暂存批次不完整")
        rows: list[MessageAttachment] = []
        for item, temporary in zip(self.items, self.temporary_paths, strict=True):
            attachment_id = new_id()
            relative_path = f"{message.id}/{attachment_id}{item.extension}"
            final_path = _safe_path(self.root, relative_path)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, final_path)
            self.final_paths.append(final_path)
            row = MessageAttachment(
                id=attachment_id,
                message_id=message.id,
                original_filename=item.original_filename,
                mime_type=item.mime_type,
                byte_size=item.byte_size,
                width=item.width,
                height=item.height,
                relative_path=relative_path,
                source=item.source,
            )
            session.add(row)
            rows.append(row)
        return rows

    def mark_committed(self) -> None:
        self.committed = True
        self.temporary_paths.clear()

    def cleanup(self) -> None:
        if self.committed:
            return
        for path in [*self.temporary_paths, *self.final_paths]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("清理附件文件失败: attachment_path=%s", path.name)
        self.temporary_paths.clear()
        self.final_paths.clear()


@dataclass(slots=True)
class PendingAttachmentBatch:
    """Validated files held by the persistent QQ listening buffer."""

    root: Path
    items: tuple[ValidatedAttachment, ...]
    temporary_paths: list[Path] = field(default_factory=list)
    pending_paths: list[Path] = field(default_factory=list)

    def persist(self) -> list[dict[str, object]]:
        if len(self.temporary_paths) != len(self.items):
            raise RuntimeError("附件暂存批次不完整")
        metadata: list[dict[str, object]] = []
        for item, temporary in zip(self.items, self.temporary_paths, strict=True):
            attachment_id = new_id()
            relative_path = f"listening/{attachment_id}{item.extension}"
            final_path = _safe_path(self.root, relative_path)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, final_path)
            self.pending_paths.append(final_path)
            metadata.append(
                {
                    "relative_path": relative_path,
                    "original_filename": item.original_filename,
                    "mime_type": item.mime_type,
                    "byte_size": item.byte_size,
                    "width": item.width,
                    "height": item.height,
                    "source": item.source,
                }
            )
        self.temporary_paths.clear()
        return metadata

    def cleanup(self) -> None:
        for path in [*self.temporary_paths, *self.pending_paths]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("清理倾听模式附件失败: attachment_path=%s", path.name)
        self.temporary_paths.clear()
        self.pending_paths.clear()


def stage_pending_attachments(
    items: Iterable[ChatAttachmentInput], *, root: Path | None = None
) -> PendingAttachmentBatch:
    """Validate and stage files that belong to a listening buffer, not a message."""

    validated = validate_inputs(items)
    batch = PendingAttachmentBatch(root=_root(root), items=validated)
    try:
        for item in validated:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=batch.root, prefix=".chat-image-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(item.content)
            batch.temporary_paths.append(temporary)
    except Exception:
        batch.cleanup()
        raise
    return batch


def pending_attachment_inputs(
    metadata: Iterable[dict[str, object]], *, root: Path | None = None
) -> tuple[ChatAttachmentInput, ...]:
    """Read persisted listening files without trusting paths from the JSON buffer."""

    result: list[ChatAttachmentInput] = []
    for item in metadata:
        relative_path = str(item.get("relative_path") or "")
        path = _safe_path(_root(root), relative_path)
        try:
            content = path.read_bytes()
        except OSError as error:
            raise AttachmentValidationError("倾听模式中的图片文件已不可读取") from error
        result.append(
            ChatAttachmentInput(
                content=content,
                original_filename=str(item.get("original_filename") or "image"),
                source="onebot",
                content_type=str(item.get("mime_type") or "") or None,
            )
        )
    return tuple(result)


def delete_pending_attachments(
    metadata: Iterable[dict[str, object]], *, root: Path | None = None
) -> None:
    """Remove files referenced by a listening buffer after completion/cancel."""

    for item in metadata:
        relative_path = str(item.get("relative_path") or "")
        try:
            path = _safe_path(_root(root), relative_path)
            path.unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.warning("清理倾听模式附件失败: attachment_id=%s", item.get("relative_path"))


@dataclass(slots=True)
class AttachmentDeletionBatch:
    """Filesystem side of an attachment deletion with database compensation."""

    moved: list[tuple[Path, Path]] = field(default_factory=list)

    def prepare(self, attachments: Iterable[MessageAttachment]) -> None:
        try:
            for attachment in attachments:
                root = get_settings().chat_image_path.resolve()
                try:
                    original = _safe_path(root, attachment.relative_path)
                except ValueError:
                    logger.warning("附件路径无效，跳过文件删除: attachment_id=%s", attachment.id)
                    continue
                if not original.is_file():
                    if original.exists():
                        logger.warning("附件路径不是文件，跳过文件删除: attachment_id=%s", attachment.id)
                    else:
                        logger.warning("附件文件缺失，继续清理数据库: attachment_id=%s", attachment.id)
                    continue
                recovery = original.with_name(f".recycle-{uuid.uuid4().hex}{original.suffix}")
                os.replace(original, recovery)
                self.moved.append((original, recovery))
        except Exception:
            self.restore()
            raise

    def restore(self) -> None:
        for original, recovery in reversed(self.moved):
            if not recovery.exists():
                continue
            try:
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(recovery, original)
            except OSError:
                logger.exception("恢复待删除附件失败: attachment_path=%s", original.name)

    def finalize(self) -> None:
        for _original, recovery in self.moved:
            try:
                recovery.unlink(missing_ok=True)
            except OSError:
                logger.warning("清理附件回收文件失败: attachment_path=%s", recovery.name)
        self.moved.clear()


def _root(root: Path | None = None) -> Path:
    resolved = (root or get_settings().chat_image_path).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _safe_path(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("附件路径超出私有图片目录")
    return candidate


def sanitize_filename(filename: str | None) -> str:
    value = str(filename or "image").replace("\\", "/").rsplit("/", 1)[-1]
    value = _CONTROL_CHARS.sub("", value).strip()
    if not value or value in {".", ".."}:
        value = "image"
    return value[:120]


def validate_attachment(item: ChatAttachmentInput) -> ValidatedAttachment:
    if item.source not in {"web", "onebot"}:
        raise AttachmentValidationError("图片来源无效")
    content = bytes(item.content)
    if not content:
        raise AttachmentValidationError("图片文件为空")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                image_format = str(image.format or "").upper()
                info = _FORMAT_INFO.get(image_format)
                if info is None:
                    raise AttachmentValidationError("仅支持 JPEG、PNG、GIF 和 WebP 图片")
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                    raise AttachmentValidationError("图片像素总数不能超过 40MP")
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
    except AttachmentValidationError:
        raise
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
    ) as error:
        raise AttachmentValidationError("图片无法解码或文件已损坏") from error
    mime_type, extension = info
    return ValidatedAttachment(
        content=content,
        original_filename=sanitize_filename(item.original_filename),
        mime_type=mime_type,
        byte_size=len(content),
        width=width,
        height=height,
        extension=extension,
        source=item.source,
    )


def validate_inputs(items: Iterable[ChatAttachmentInput]) -> tuple[ValidatedAttachment, ...]:
    values = tuple(items)
    if len(values) > MAX_ATTACHMENTS:
        raise AttachmentValidationError(f"每条消息最多附带 {MAX_ATTACHMENTS} 张图片")
    validated: list[ValidatedAttachment] = []
    total = 0
    for item in values:
        current = validate_attachment(item)
        total += current.byte_size
        if total > MAX_TOTAL_BYTES:
            raise AttachmentValidationError("每条消息图片总大小不能超过 32 MiB")
        validated.append(current)
    return tuple(validated)


def stage_attachments(
    items: Iterable[ChatAttachmentInput], *, root: Path | None = None
) -> PreparedAttachmentBatch:
    validated = validate_inputs(items)
    batch = PreparedAttachmentBatch(root=_root(root), items=validated)
    try:
        for item in validated:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=batch.root, prefix=".chat-image-", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(item.content)
            batch.temporary_paths.append(temporary)
    except Exception:
        batch.cleanup()
        raise
    return batch


def attachment_dict(item: MessageAttachment) -> dict[str, object]:
    return {
        "id": item.id,
        "message_id": item.message_id,
        "original_filename": item.original_filename,
        "mime_type": item.mime_type,
        "byte_size": item.byte_size,
        "width": item.width,
        "height": item.height,
        "source": item.source,
        "created_at": utc_isoformat(item.created_at),
        "download_url": f"/api/v1/messages/{item.message_id}/attachments/{item.id}",
    }


def attachment_path(item: MessageAttachment, *, root: Path | None = None) -> Path:
    return _safe_path(_root(root), item.relative_path)


def image_data_url(item: MessageAttachment, *, root: Path | None = None) -> str:
    path = attachment_path(item, root=root)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise AttachmentValidationError("当前图片文件不可读取") from error
    return f"data:{item.mime_type};base64,{base64.b64encode(payload).decode('ascii')}"


def attachment_content_blocks(
    history: list[Message], *, max_images: int = MAX_ATTACHMENTS, root: Path | None = None
) -> dict[str, list[dict[str, object]]]:
    """Build image blocks for recent history while skipping stale files safely."""

    candidates: list[tuple[str, MessageAttachment]] = []
    for message in history:
        if message.role != "user":
            continue
        for attachment in message.attachments:
            candidates.append((message.id, attachment))
    selected = candidates[-max_images:]
    blocks: dict[str, list[dict[str, object]]] = {}
    for message_id, attachment in selected:
        try:
            url = image_data_url(attachment, root=root)
        except AttachmentValidationError:
            logger.warning("历史图片读取失败，跳过上下文: attachment_id=%s", attachment.id)
            continue
        blocks.setdefault(message_id, []).append(
            {"type": "image_url", "image_url": {"url": url, "detail": "auto"}}
        )
    return blocks


def stage_deletions(attachments: Iterable[MessageAttachment]) -> AttachmentDeletionBatch:
    batch = AttachmentDeletionBatch()
    batch.prepare(attachments)
    return batch


def delete_attachment(
    session: Session, message_id: str, attachment_id: str
) -> bool:
    item = session.scalar(
        select(MessageAttachment).where(
            MessageAttachment.id == attachment_id,
            MessageAttachment.message_id == message_id,
        )
    )
    if item is None:
        return False
    batch = stage_deletions([item])
    session.delete(item)
    try:
        session.commit()
    except Exception:
        session.rollback()
        batch.restore()
        raise
    batch.finalize()
    return True
