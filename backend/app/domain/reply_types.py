"""Stable reply-plan contracts shared by Web and QQ delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReplyMode = Literal["short", "long"]
ReplyPolicyStatus = Literal["accepted", "repaired", "original_preserved"]
ReplyRepairFailure = Literal[
    "invalid_json",
    "invalid_structure",
    "validation_failed",
    "atomic_changed",
    "model_error",
]
_BOUNDARY_PUNCTUATION = frozenset("。！？!?；;")


@dataclass(frozen=True, slots=True)
class ReplyPart:
    kind: Literal["segment", "atomic"]
    text: str


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    mode: ReplyMode
    segments: tuple[str, ...]
    atomic_parts: tuple[str, ...]
    parts: tuple[ReplyPart, ...]
    policy_status: ReplyPolicyStatus = "accepted"
    policy_violations: tuple[str, ...] = ()
    repair_failure_code: ReplyRepairFailure | None = None
    source_text: str | None = None

    @property
    def text(self) -> str:
        if self.source_text is not None:
            return self.source_text
        rendered: list[str] = []
        for part in self.parts:
            if not part.text:
                continue
            if part.kind == "segment" and part.text in _BOUNDARY_PUNCTUATION and rendered:
                rendered[-1] += part.text
            else:
                rendered.append(part.text)
        return "\n".join(rendered)

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "response_mode": self.mode,
            "segments": list(self.segments),
            "atomic_parts": list(self.atomic_parts),
            "parts": [{"kind": part.kind, "text": part.text} for part in self.parts],
            "reply_policy_status": self.policy_status,
        }


__all__ = [
    "ReplyMode",
    "ReplyPart",
    "ReplyPlan",
    "ReplyPolicyStatus",
    "ReplyRepairFailure",
]
