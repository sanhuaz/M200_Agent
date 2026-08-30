from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

Action = Callable[..., Awaitable[dict[str, object]]]


def _ensure_success(response: dict[str, object], label: str) -> None:
    if response.get("status") != "ok" or int(str(response.get("retcode", -1))) != 0:
        raise RuntimeError(f"{label}: {response.get('message') or response.get('wording')}")


async def send_text(
    action: Action,
    user_id: str,
    text: str,
    group_id: str | None = None,
) -> None:
    if group_id:
        response = await action("send_group_msg", {"group_id": int(group_id), "message": text})
    else:
        response = await action("send_private_msg", {"user_id": int(user_id), "message": text})
    _ensure_success(response, "QQ 文本发送失败")


async def send_private_file(action: Action, user_id: str, path: Path) -> None:
    response = await action(
        "upload_private_file",
        {"user_id": int(user_id), "file": str(path.resolve()), "name": path.name},
        timeout_seconds=300,
    )
    _ensure_success(response, "QQ 文件上传失败")
