from __future__ import annotations

from fastapi import HTTPException, Request


def require_loopback(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(403, "本机管理接口只允许回环地址访问")
