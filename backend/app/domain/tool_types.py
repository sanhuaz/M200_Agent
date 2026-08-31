"""Cross-layer tool contracts shared by the agent and extension services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Request identity and policy context visible to a tool."""

    requester_id: str
    conversation_id: str | None
    platform: str
    is_group: bool
    workspace_path: Path
    is_owner: bool = False
