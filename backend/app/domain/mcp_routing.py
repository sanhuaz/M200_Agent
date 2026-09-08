"""Immutable MCP intent decisions shared by routing, prompts and tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

McpAvailability = Literal["available", "connected_unauthorized", "unavailable"]
McpDecisionStatus = Literal["none", "selected", "ambiguous", "status_query"]
McpIntentLevel = Literal[
    "tool_exact",
    "tool_rule",
    "server_exact",
    "server_rule",
    "global_rule",
    "builtin",
    "inheritance",
]


@dataclass(frozen=True)
class McpToolSelection:
    """The exact MCP scope allowed for one agent turn.

    ``item_keys`` is empty for a whole-server selection.  A non-empty set
    restricts registration and call-time authorization to those catalog keys.
    An empty ``server_ids`` set is an explicit "no MCP tools" decision; the
    optional ``None`` value used by low-level legacy callers keeps their old
    registry behavior intact.
    """

    server_ids: frozenset[str] = frozenset()
    item_keys: frozenset[tuple[str, str]] = frozenset()

    def allows(self, server_id: str, item_key: str) -> bool:
        if server_id not in self.server_ids:
            return False
        if not self.item_keys:
            return True
        return (server_id, item_key) in self.item_keys


@dataclass(frozen=True)
class McpIntentDecision:
    """One deterministic intent result consumed by all MCP boundaries."""

    status: McpDecisionStatus
    availability: McpAvailability | None = None
    server_id: str | None = None
    server_name: str | None = None
    server_slug: str | None = None
    tool_key: str | None = None
    level: McpIntentLevel | None = None
    matched_rule_ids: tuple[str, ...] = ()
    selection: McpToolSelection = McpToolSelection()
    inherited: bool = False
    inherited_evidence: tuple[str, ...] = ()
    requires_fresh: bool = False
    state_reason: str | None = None
    anysearch_guard: bool = False

    @property
    def has_bound_tools(self) -> bool:
        return self.status == "selected" and self.availability == "available"


__all__ = [
    "McpAvailability",
    "McpDecisionStatus",
    "McpIntentDecision",
    "McpIntentLevel",
    "McpToolSelection",
]
