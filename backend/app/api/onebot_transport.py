from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.services.operation_logs import operation_logs

logger = logging.getLogger(__name__)
MessageHandler = Callable[[dict[str, object]], Coroutine[Any, Any, None]]


class OneBotTransport:
    """OneBot WebSocket lifecycle and echo-correlated action transport."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.websocket: WebSocket | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}

    @staticmethod
    def extract_token(websocket: WebSocket) -> str:
        query_token = websocket.query_params.get("access_token") or websocket.query_params.get("token")
        if query_token:
            return query_token
        authorization = websocket.headers.get("authorization", "")
        return authorization.removeprefix("Bearer ").strip()

    async def serve(self, websocket: WebSocket, on_message: MessageHandler) -> None:
        token = self.extract_token(websocket)
        if not self.token or self.token == "change-me" or token != self.token:
            await websocket.close(code=1008, reason="OneBot Token 未配置或不匹配")
            return
        await websocket.accept()
        self.websocket = websocket
        operation_logs.emit(
            source="onebot", kind="started", title="OneBot 连接", message="OneBot WebSocket 已连接"
        )
        try:
            while True:
                payload = await websocket.receive_json()
                echo = payload.get("echo")
                if echo is not None and str(echo) in self._pending:
                    future = self._pending.pop(str(echo))
                    if not future.done():
                        future.set_result(payload)
                    continue
                if payload.get("post_type") == "message":
                    asyncio.create_task(on_message(payload))
        except WebSocketDisconnect:
            logger.info("NapCat OneBot 已断开")
            operation_logs.emit(
                source="onebot",
                level="warn",
                kind="failed",
                title="OneBot 连接断开",
                message="OneBot WebSocket 已断开",
            )
        finally:
            if self.websocket is websocket:
                self.websocket = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("OneBot 连接已断开"))
            self._pending.clear()

    async def action(
        self,
        action: str,
        params: dict[str, object],
        *,
        timeout_seconds: float = 30,
    ) -> dict[str, object]:
        if self.websocket is None:
            raise ConnectionError("NapCat 未连接")
        echo = uuid.uuid4().hex
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        async with self._send_lock:
            await self.websocket.send_json({"action": action, "params": params, "echo": echo})
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        finally:
            self._pending.pop(echo, None)
