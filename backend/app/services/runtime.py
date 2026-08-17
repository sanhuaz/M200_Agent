from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AdminIdentity, AppSetting
from app.db.session import SessionLocal
from app.services.extensions import ensure_builtin_packages

OWNER_BOOTSTRAP_KEY = "owner_qq_ids_bootstrapped"


def bootstrap_runtime(session: Session) -> None:
    """初始化只应由配置提供的一次性运行状态。

    `.env` 中的 OWNER_QQ_IDS 只在首次启动时导入，之后 Owner 以数据库为准。
    """

    ensure_builtin_packages(session)
    marker = session.get(AppSetting, OWNER_BOOTSTRAP_KEY)
    if marker is not None:
        return
    settings = get_settings()
    for external_id in settings.owner_qq_ids:
        if not external_id:
            continue
        exists = session.scalar(
            select(AdminIdentity).where(AdminIdentity.external_id == str(external_id))
        )
        if exists is None:
            session.add(
                AdminIdentity(
                    platform="qq",
                    external_id=str(external_id),
                    display_name=".env owner",
                    enabled=True,
                    created_by="local-owner",
                )
            )
    session.add(AppSetting(key=OWNER_BOOTSTRAP_KEY, value="true"))
    session.commit()


def is_owner(user_id: str) -> bool:
    if user_id == "local-owner":
        return True
    with SessionLocal() as session:
        identity = session.scalar(
            select(AdminIdentity).where(
                AdminIdentity.external_id == str(user_id),
                AdminIdentity.platform == "qq",
                AdminIdentity.enabled.is_(True),
            )
        )
        return identity is not None


def admin_dict(item: AdminIdentity) -> dict[str, object]:
    return {
        "id": item.id,
        "platform": item.platform,
        "external_id": item.external_id,
        "display_name": item.display_name,
        "enabled": item.enabled,
        "created_by": item.created_by,
        "created_at": item.created_at.isoformat(),
    }


def setting_json(session: Session, key: str, default: object) -> object:
    item = session.get(AppSetting, key)
    if item is None:
        return default
    try:
        return json.loads(item.value)
    except json.JSONDecodeError:
        return item.value
