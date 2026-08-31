from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ChatInputSource = Literal["web", "onebot"]


@dataclass(frozen=True, slots=True)
class ChatAttachmentInput:
    """Raw attachment received from an adapter, before persistence."""

    content: bytes
    original_filename: str = "image"
    source: ChatInputSource = "web"
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class ChatTurnInput:
    """Transport-neutral chat input shared by Web and OneBot adapters."""

    text: str = ""
    attachments: tuple[ChatAttachmentInput, ...] = field(default_factory=tuple)
    conversation_id: str | None = None
    sender_id: str | None = None
    platform_message_id: str | None = None
    platform: str = "web"
    is_group: bool = False
    group_id: str | None = None
    trigger: str | None = None
    model_alias: str | None = None
    persona_id: str | None = None
