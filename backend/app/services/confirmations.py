from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import Confirmation, ExtensionPackage, Job
from app.db.session import SessionLocal
from app.services.runtime import is_owner


def utc_isoformat(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


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
    import shutil
    from pathlib import Path

    from app.core.config import get_settings
    from app.services.extensions import import_github

    kind = str(payload.get("kind"))
    operation = str(payload.get("operation"))
    name_or_url = str(payload.get("name_or_url"))
    if operation == "install":
        item = import_github(session, kind, name_or_url)
        return {"kind": kind, "name": item.name, "operation": operation, "status": item.status}
    item = session.scalar(
        select(ExtensionPackage).where(
            ExtensionPackage.kind == kind, ExtensionPackage.name == name_or_url
        )
    )
    if item is None:
        raise ValueError("扩展不存在")
    if operation == "remove":
        if item.builtin:
            raise ValueError("内置扩展不能删除")
        root = (get_settings().tools_path if kind == "tool" else get_settings().skills_path).resolve()
        path = Path(item.install_path).resolve()
        if path != root and root not in path.parents:
            raise ValueError("扩展路径无效")
        session.delete(item)
        session.flush()
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        return {"kind": kind, "name": name_or_url, "operation": operation, "status": "removed"}
    item.enabled = operation == "enable"
    item.status = "ready" if item.enabled else "installed_disabled"
    session.flush()
    return {
        "kind": kind,
        "name": name_or_url,
        "operation": operation,
        "status": item.status,
    }


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
