from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import require_loopback
from app.db.models import AdminIdentity, Memory, Persona, RelationshipProfile
from app.db.session import get_db
from app.services.companion import relationship_content_items
from app.services.memories import MemoryService
from app.services.persona_store import get_persona_store
from app.services.time_context import utc_isoformat

router = APIRouter()


class MemoryUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=5_000)


class MemoryCreate(BaseModel):
    fact_key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=5_000)
    scope_type: str = "global"
    user_id: str | None = None


@router.get("/memories")
def list_memories(
    scope: str = "all",
    user_id: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    query = select(Memory).order_by(Memory.last_seen_at.desc())
    if scope == "global":
        query = query.where(Memory.scope_type == "global", Memory.user_id.is_(None))
    elif scope == "user":
        query = query.where(Memory.scope_type == "user")
        if user_id:
            query = query.where(Memory.user_id == user_id)
    elif user_id:
        query = query.where(Memory.user_id == user_id)
    if status:
        query = query.where(Memory.status == status)
    if keyword:
        query = query.where(Memory.content.contains(keyword))
    return [
        {
            "id": item.id,
            "scope_type": item.scope_type,
            "user_id": item.user_id,
            "fact_key": item.fact_key,
            "content": item.content,
            "status": item.status,
            "source_message_id": item.source_message_id,
            "created_at": utc_isoformat(item.created_at),
            "last_seen_at": utc_isoformat(item.last_seen_at),
        }
        for item in session.scalars(query)
    ]


@router.get("/memory-center", dependencies=[Depends(require_loopback)])
def list_memory_center(
    memory_type: Literal["all", "fact", "relationship"] = "all",
    scope_type: Literal["all", "global", "user", "group"] = "all",
    scope_id: str | None = None,
    persona_key: str | None = None,
    status: Literal["active", "archived", "all"] = "active",
    keyword: str | None = None,
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    """Expose both internal memory stores through one management surface."""
    get_persona_store().sync_db(session)
    session.flush()
    rows: list[dict[str, object]] = []
    keyword_text = (keyword or "").strip()
    normalized_keyword = keyword_text.casefold()

    if memory_type in {"all", "fact"}:
        fact_query = select(Memory).order_by(Memory.last_seen_at.desc())
        if scope_type == "global":
            fact_query = fact_query.where(
                Memory.scope_type == "global", Memory.user_id.is_(None)
            )
            if scope_id:
                fact_query = fact_query.where(Memory.user_id == scope_id)
        elif scope_type in {"user", "group"}:
            fact_query = fact_query.where(Memory.scope_type == scope_type)
            if scope_id:
                fact_query = fact_query.where(Memory.user_id == scope_id)
        elif scope_id:
            fact_query = fact_query.where(Memory.user_id == scope_id)
        if status != "all":
            fact_query = fact_query.where(Memory.status == status)
        if keyword_text:
            fact_query = fact_query.where(
                or_(
                    Memory.fact_key.contains(keyword_text),
                    Memory.content.contains(keyword_text),
                )
            )
        for item in session.scalars(fact_query):
            updated_at = utc_isoformat(item.last_seen_at)
            rows.append(
                {
                    "memory_type": "fact",
                    "id": item.id,
                    "scope_type": item.scope_type,
                    "scope_id": item.user_id,
                    "user_id": item.user_id,
                    "fact_key": item.fact_key,
                    "content": item.content,
                    "status": item.status,
                    "source_message_id": item.source_message_id,
                    "created_at": utc_isoformat(item.created_at),
                    "last_seen_at": updated_at,
                    "updated_at": updated_at,
                }
            )

    if (
        memory_type in {"all", "relationship"}
        and status != "archived"
        and scope_type in {"all", "user"}
    ):
        owner_ids = set(
            session.scalars(
                select(AdminIdentity.external_id).where(
                    AdminIdentity.platform == "qq",
                    AdminIdentity.enabled.is_(True),
                )
            )
        )
        if scope_id:
            owner_ids &= {scope_id}
        if owner_ids:
            relationship_query = select(RelationshipProfile).where(
                RelationshipProfile.scope_id.in_(owner_ids)
            )
            if persona_key:
                relationship_query = relationship_query.where(
                    RelationshipProfile.persona_key == persona_key
                )
            relationships = list(session.scalars(relationship_query))
            persona_ids = {item.persona_id for item in relationships if item.persona_id}
            persona_names = (
                {
                    item.id: item.name
                    for item in session.scalars(select(Persona).where(Persona.id.in_(persona_ids)))
                }
                if persona_ids
                else {}
            )
            for item in relationships:
                try:
                    boundaries: object = json.loads(item.boundaries or "{}")
                except json.JSONDecodeError:
                    boundaries = {}
                content_items = relationship_content_items(
                    item.nickname, item.shared_summary, boundaries
                )
                persona_name = persona_names.get(item.persona_id or "", item.persona_key)
                if normalized_keyword:
                    searchable = " ".join(
                        (
                            item.persona_key,
                            persona_name,
                            item.nickname or "",
                            item.shared_summary or "",
                            json.dumps(boundaries, ensure_ascii=False),
                            " ".join(
                                f"{entry['label']} {entry['content']}"
                                for entry in content_items
                            ),
                        )
                    ).casefold()
                    if normalized_keyword not in searchable:
                        continue
                updated_at = utc_isoformat(item.updated_at)
                rows.append(
                    {
                        "memory_type": "relationship",
                        "id": item.id,
                        "scope_type": "user",
                        "scope_id": item.scope_id,
                        "user_id": item.scope_id,
                        "persona_key": item.persona_key,
                        "persona_id": item.persona_id,
                        "persona_name": persona_name,
                        "nickname": item.nickname,
                        "shared_summary": item.shared_summary,
                        "boundaries": boundaries,
                        "content_items": content_items,
                        "version": item.version,
                        "status": "active",
                        "created_at": utc_isoformat(item.created_at),
                        "last_seen_at": None,
                        "updated_at": updated_at,
                    }
                )

    rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return rows


@router.post("/memories", dependencies=[Depends(require_loopback)])
def create_memory(payload: MemoryCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    if payload.scope_type not in {"global", "user"}:
        raise HTTPException(400, "scope_type 只能是 global 或 user")
    if payload.scope_type == "global" and payload.user_id is not None:
        raise HTTPException(400, "全局记忆不能绑定 user_id")
    if payload.scope_type == "user" and not payload.user_id:
        raise HTTPException(400, "用户记忆必须提供 user_id")
    try:
        item = (
            MemoryService(session).upsert_global(payload.fact_key, payload.content)
            if payload.scope_type == "global"
            else MemoryService(session).upsert(
                payload.user_id or "", payload.fact_key, payload.content
            )
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {
        "id": item.id,
        "scope_type": item.scope_type,
        "user_id": item.user_id,
        "fact_key": item.fact_key,
        "content": item.content,
        "status": item.status,
    }


@router.put("/memories/{memory_id}", dependencies=[Depends(require_loopback)])
def update_memory(
    memory_id: str, payload: MemoryUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        item = MemoryService(session).update(memory_id, payload.content)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"id": item.id, "content": item.content, "status": item.status}


@router.post("/memories/{memory_id}/archive", dependencies=[Depends(require_loopback)])
def archive_memory(memory_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        item = MemoryService(session).archive(memory_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return {"id": item.id, "status": item.status}


@router.delete("/memories/{memory_id}", dependencies=[Depends(require_loopback)])
def delete_memory(memory_id: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    try:
        MemoryService(session).delete(memory_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return {"deleted": True}


@router.post("/memories/{memory_id}/restore", dependencies=[Depends(require_loopback)])
def restore_memory(memory_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        item = MemoryService(session).restore(memory_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return {"id": item.id, "status": item.status}
