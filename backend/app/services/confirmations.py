from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import Confirmation, Job
from app.db.session import SessionLocal
from app.services.runtime import is_owner


def utc_isoformat(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def create_extension_confirmation(
    kind: str,
    operation: str,
    name_or_url: str,
    requester_id: str,
    conversation_id: str | None,
) -> Confirmation:
    if kind not in {"tool", "skill"} or operation not in {"install", "enable", "disable", "remove"}:
        raise ValueError("扩展确认参数无效")
    if not is_owner(requester_id):
        raise PermissionError("只有 Owner 可以管理 Tool/Skill")
    with SessionLocal.begin() as session:
        confirmation = Confirmation(
            token=secrets.token_urlsafe(9),
            requester_id=requester_id,
            conversation_id=conversation_id,
            action="extension_manage",
            payload=json.dumps(
                {"kind": kind, "operation": operation, "name_or_url": name_or_url},
                ensure_ascii=False,
            ),
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=10),
        )
        session.add(confirmation)
        session.flush()
        return confirmation


def _execute_extension_change(session, payload: dict[str, object]) -> dict[str, object]:
    from app.services.extensions import apply_package_operation

    kind = str(payload.get("kind"))
    operation = str(payload.get("operation"))
    name_or_url = str(payload.get("name_or_url"))
    return apply_package_operation(
        session,
        kind=kind,
        operation=operation,
        name_or_url=name_or_url,
    )


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
        if confirmation.action == "extension_manage":
            try:
                result = _execute_extension_change(session, json.loads(confirmation.payload))
                confirmation.payload = json.dumps(
                    {**json.loads(confirmation.payload), "result": result}, ensure_ascii=False
                )
                session.commit()
                return confirmation, None
            except Exception as error:
                confirmation.status = "failed"
                confirmation.payload = json.dumps(
                    {**json.loads(confirmation.payload), "error": str(error)},
                    ensure_ascii=False,
                )
                session.commit()
                raise ValueError(f"扩展操作失败: {type(error).__name__}: {error}") from error
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
