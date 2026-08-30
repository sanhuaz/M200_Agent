from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CompanionPreference, RelationshipProfile


def get_or_create_preference(session: Session, scope_id: str) -> CompanionPreference:
    item = session.scalar(
        select(CompanionPreference).where(
            CompanionPreference.scope_type == "qq_user",
            CompanionPreference.scope_id == scope_id,
        )
    )
    if item is not None:
        return item
    item = CompanionPreference(scope_type="qq_user", scope_id=scope_id)
    session.add(item)
    session.flush()
    return item


def preference_dict(item: CompanionPreference) -> dict[str, object]:
    try:
        boundaries: object = json.loads(item.boundaries or "{}")
    except json.JSONDecodeError:
        boundaries = {}
    return {
        "id": item.id,
        "scope_type": item.scope_type,
        "scope_id": item.scope_id,
        "companion_enabled": item.companion_enabled,
        "support_mode": item.support_mode,
        "memory_enabled": item.memory_enabled,
        "safety_mode": getattr(item, "safety_mode", "standard") or "standard",
        "listening_enabled": getattr(item, "listening_enabled", False),
        "listening_silence_seconds": getattr(item, "listening_silence_seconds", 30),
        "analyzer_model_alias": item.analyzer_model_alias,
        "boundaries": boundaries,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def persona_key(persona_id: str | None) -> str:
    return persona_id or "default"


def get_relationship(
    session: Session, scope_id: str, persona_id: str | None, *, create: bool = False
) -> RelationshipProfile | None:
    key = persona_key(persona_id)
    item = session.scalar(
        select(RelationshipProfile).where(
            RelationshipProfile.scope_id == scope_id,
            RelationshipProfile.persona_key == key,
        )
    )
    if item is None and create:
        item = RelationshipProfile(scope_id=scope_id, persona_key=key, persona_id=persona_id)
        session.add(item)
        session.flush()
    return item


def relationship_content_items(
    nickname: str | None,
    shared_summary: str | None,
    boundaries: object,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []

    def add(kind: str, label: str, value: object) -> None:
        if value is None:
            return
        if isinstance(value, str):
            content = re.sub(r"\s+", " ", value).strip()
        elif isinstance(value, (dict, list)):
            try:
                content = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                content = str(value).strip()
        else:
            content = str(value).strip()
        if content:
            items.append({"kind": kind, "label": label, "content": content})

    def add_many(kind: str, label: str, value: object) -> None:
        if isinstance(value, list):
            for entry in value:
                add(kind, label, entry)
        else:
            add(kind, label, value)

    if isinstance(boundaries, dict):
        add_many("preference", "偏好", boundaries.get("preferences"))
        add_many("boundary", "边界", boundaries.get("items"))
        for key, value in boundaries.items():
            if key not in {"preferences", "items"}:
                add_many("detail", f"其他资料（{key}）", value)
    else:
        add("detail", "其他资料", boundaries)
    add("nickname", "称呼", nickname)
    add("shared_event", "共同经历", shared_summary)
    return items


def relationship_dict(
    item: RelationshipProfile | None, scope_id: str, persona_id: str | None
) -> dict[str, object]:
    if item is None:
        return {
            "scope_id": scope_id,
            "persona_key": persona_key(persona_id),
            "persona_id": persona_id,
            "nickname": None,
            "shared_summary": "",
            "boundaries": {},
            "content_items": [],
            "version": 0,
        }
    try:
        boundaries: object = json.loads(item.boundaries or "{}")
    except json.JSONDecodeError:
        boundaries = {}
    return {
        "id": item.id,
        "scope_id": item.scope_id,
        "persona_key": item.persona_key,
        "persona_id": item.persona_id,
        "nickname": item.nickname,
        "shared_summary": item.shared_summary,
        "boundaries": boundaries,
        "content_items": relationship_content_items(item.nickname, item.shared_summary, boundaries),
        "version": item.version,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def relationship_context(item: RelationshipProfile | None) -> str:
    if item is None:
        return "- 暂无已授权的角色关系资料"
    try:
        boundaries = json.loads(item.boundaries or "{}")
    except json.JSONDecodeError:
        boundaries = {}
    content_items = relationship_content_items(item.nickname, item.shared_summary, boundaries)
    parts = [f"{entry['label']}：{entry['content']}" for entry in content_items]
    return "- " + ("；".join(parts) if parts else "暂无已授权的角色关系资料")


def safe_relation_value(value: str) -> str | None:
    value = re.sub(r"\s+", " ", value.strip())
    if not value or len(value) > 240:
        return None
    sensitive = re.compile(
        r"(?i)(api[_ -]?key|token|password|passwd|secret|密码|密钥|身份证|银行卡|"
        r"住址|自杀|自残|杀人|抑郁症|精神疾病)"
    )
    return None if sensitive.search(value) else value
