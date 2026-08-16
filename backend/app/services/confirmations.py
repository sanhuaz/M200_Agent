from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Confirmation, Job
from app.db.session import SessionLocal


def is_owner(user_id: str) -> bool:
    return user_id == "local-owner" or user_id in get_settings().owner_qq_ids


def create_download_confirmation(
    album_id: str, requester_id: str, conversation_id: str | None
) -> Confirmation:
    if not is_owner(requester_id):
        raise PermissionError("只有 Owner 可以创建漫画下载任务")
    with SessionLocal.begin() as session:
        confirmation = Confirmation(
            token=secrets.token_urlsafe(9),
            requester_id=requester_id,
            conversation_id=conversation_id,
            action="manga_download",
            payload=json.dumps({"album_id": album_id}, ensure_ascii=False),
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
        )
        session.add(confirmation)
        session.flush()
        return confirmation


def resolve_confirmation(token: str, requester_id: str, approve: bool) -> tuple[Confirmation, Job | None]:
    with SessionLocal() as session:
        confirmation = session.scalar(select(Confirmation).where(Confirmation.token == token))
        if confirmation is None:
            raise ValueError("确认请求不存在")
        if confirmation.status != "pending":
            raise ValueError(f"确认请求状态为 {confirmation.status}")
        if confirmation.requester_id != requester_id or not is_owner(requester_id):
            raise PermissionError("无权处理该确认请求")
        if confirmation.expires_at < datetime.now(UTC).replace(tzinfo=None):
            confirmation.status = "expired"
            session.commit()
            raise ValueError("确认请求已过期")
        if not approve:
            confirmation.status = "rejected"
            session.commit()
            return confirmation, None
        confirmation.status = "approved"
        job = Job(
            type="manga_download",
            status="queued",
            requester_id=requester_id,
            conversation_id=confirmation.conversation_id,
            payload=confirmation.payload,
        )
        session.add(job)
        session.flush()
        session.commit()
        return confirmation, job
