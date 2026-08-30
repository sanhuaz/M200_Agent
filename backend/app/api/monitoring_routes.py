from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import require_loopback
from app.api.onebot import onebot_manager, qq_reply_settings, set_qq_reply_settings
from app.db.session import get_db
from app.services.napcat_logs import napcat_connector
from app.services.operation_logs import operation_logs

router = APIRouter()


class NapCatLogConfig(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    token: str | None = Field(default=None, max_length=2_000)
    clear_token: bool = False


class NapCatLogTest(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    token: str | None = Field(default=None, max_length=2_000)


class QQReplySettingsUpdate(BaseModel):
    chunked_output_enabled: bool | None = None
    chunk_target_chars: int | None = Field(default=None, ge=5, le=100)


@router.get("/logs", dependencies=[Depends(require_loopback)])
def list_operation_logs(
    source: str | None = None,
    level: str | None = None,
    query: str | None = None,
    limit: int = 500,
    after_id: str | None = None,
) -> dict[str, object]:
    return {
        "session_id": operation_logs.session_id,
        "events": operation_logs.snapshot(
            source=source, level=level, query=query, limit=limit, after_id=after_id
        ),
    }


@router.get("/onebot/reply-settings", dependencies=[Depends(require_loopback)])
def get_qq_reply_settings(session: Session = Depends(get_db)) -> dict[str, object]:
    return qq_reply_settings(session)


@router.put("/onebot/reply-settings", dependencies=[Depends(require_loopback)])
def update_qq_reply_settings(
    payload: QQReplySettingsUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    if payload.chunked_output_enabled is None and payload.chunk_target_chars is None:
        raise HTTPException(422, "至少提供一个 QQ 回复设置")
    set_qq_reply_settings(
        session,
        enabled=payload.chunked_output_enabled,
        target_chars=payload.chunk_target_chars,
    )
    session.commit()
    return qq_reply_settings(session)


@router.get("/logs/active", dependencies=[Depends(require_loopback)])
def active_operation_logs() -> dict[str, object]:
    return {
        "session_id": operation_logs.session_id,
        "operations": operation_logs.active_operations(),
        "napcat": napcat_connector.public_status(),
        "onebot": onebot_manager.status(),
    }


@router.get("/logs/config", dependencies=[Depends(require_loopback)])
def get_log_config() -> dict[str, object]:
    return {"napcat": napcat_connector.public_status()}


@router.put("/logs/config", dependencies=[Depends(require_loopback)])
async def put_log_config(payload: NapCatLogConfig) -> dict[str, object]:
    try:
        return {
            "napcat": await napcat_connector.configure(
                payload.url, payload.token, payload.clear_token
            )
        }
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/logs/test-connection", dependencies=[Depends(require_loopback)])
async def test_log_connection(payload: NapCatLogTest) -> dict[str, object]:
    try:
        return await napcat_connector.test_connection(payload.url, payload.token)
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/logs/finalize", dependencies=[Depends(require_loopback)])
def finalize_logs() -> dict[str, object]:
    operation_logs.emit(
        source="system", kind="stopped", title="项目停止", message="收到停止请求，正在刷新日志"
    )
    operation_logs.flush()
    return {"session_id": operation_logs.session_id, "flushed": True}


@router.get("/logs/stream", dependencies=[Depends(require_loopback)])
async def stream_operation_logs(
    request: Request,
    source: str | None = None,
    level: str | None = None,
    query: str | None = None,
    after_id: str | None = None,
) -> StreamingResponse:
    subscriber = operation_logs.subscribe(asyncio.get_running_loop())
    initial = operation_logs.snapshot(
        source=source, level=level, query=query, limit=500, after_id=after_id
    )

    def frame(event_name: str, data: object) -> str:
        return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def events():
        try:
            for item in initial:
                yield frame("operation" if item.get("operation_id") else "log", item)
            yield frame(
                "status",
                {"napcat": napcat_connector.public_status(), "onebot": onebot_manager.status()},
            )
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(subscriber.queue.get(), timeout=15)
                    if source and item.get("source") != source:
                        continue
                    if level and item.get("level") != level:
                        continue
                    if query and query.casefold() not in json.dumps(item, ensure_ascii=False).casefold():
                        continue
                    yield frame("operation" if item.get("operation_id") else "log", item)
                    if subscriber.dropped:
                        dropped = subscriber.dropped
                        subscriber.dropped = 0
                        yield frame(
                            "log",
                            operation_logs.emit(
                                source="system",
                                level="warn",
                                kind="message",
                                title="日志订阅过慢",
                                message=f"客户端过慢，已丢弃 {dropped} 条最旧推送",
                                details={"dropped": dropped},
                            ),
                        )
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    yield frame(
                        "status",
                        {
                            "napcat": napcat_connector.public_status(),
                            "onebot": onebot_manager.status(),
                        },
                    )
        finally:
            operation_logs.unsubscribe(subscriber)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
