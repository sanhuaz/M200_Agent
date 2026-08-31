"""Loopback-only MCP Server management endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_loopback
from app.db.models import McpServer
from app.db.session import get_db
from app.domain.mcp_types import (
    McpEnabledPayload,
    McpGrantPayload,
    McpServerPayload,
    McpServerUpdate,
)
from app.services.mcp_client import McpClientError, safe_error
from app.services.mcp_servers import (
    McpServerError,
    catalog_with_grants,
    create_server,
    delete_server,
    mcp_manager,
    replace_grants,
    server_dict,
    update_server,
)

router = APIRouter(prefix="/mcp", tags=["mcp"])
_MANAGEMENT = [Depends(require_loopback)]


def _get_server(server_id: str, session: Session) -> McpServer:
    item = session.get(McpServer, server_id)
    if item is None:
        raise HTTPException(404, "MCP Server 不存在")
    return item


def _save_connection_result(item: McpServer, info: dict[str, object], catalog: dict[str, object]) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    item.server_info_json = json.dumps(info, ensure_ascii=False, separators=(",", ":"))
    item.catalog_json = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
    item.last_connected_at = now
    item.last_refreshed_at = now
    item.last_error = None
    item.status = "ready"


def _safe_http_error(error: BaseException, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code, safe_error(error))


@router.get("/servers", dependencies=_MANAGEMENT)
def list_mcp_servers(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [server_dict(item) for item in session.scalars(select(McpServer).order_by(McpServer.created_at))]


@router.post("/servers", dependencies=_MANAGEMENT)
def create_mcp_server(
    payload: McpServerPayload, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        item = create_server(session, payload)
        session.commit()
        session.refresh(item)
        return server_dict(item)
    except McpServerError as error:
        session.rollback()
        raise _safe_http_error(error) from error


@router.put("/servers/{server_id}", dependencies=_MANAGEMENT)
async def update_mcp_server(
    server_id: str, payload: McpServerUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    item = _get_server(server_id, session)
    was_enabled = item.enabled
    try:
        item, transport_changed = update_server(session, item, payload)
        session.commit()
        session.refresh(item)
    except McpServerError as error:
        session.rollback()
        raise _safe_http_error(error) from error

    if was_enabled and transport_changed:
        try:
            item.status = "connecting"
            item.last_error = None
            session.commit()
            info, catalog = await mcp_manager.connect(item)
            _save_connection_result(item, info, catalog)
            session.commit()
            session.refresh(item)
        except Exception as error:
            await mcp_manager.disconnect(item.id)
            current = session.get(McpServer, item.id)
            if current is not None:
                current.status = "error"
                current.last_error = safe_error(error)
                session.commit()
                item = current
    return server_dict(item)


@router.delete("/servers/{server_id}", dependencies=_MANAGEMENT)
async def delete_mcp_server(server_id: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    item = _get_server(server_id, session)
    await mcp_manager.disconnect(item.id)
    try:
        delete_server(session, item)
        session.commit()
    except McpServerError as error:
        session.rollback()
        raise _safe_http_error(error) from error
    return {"deleted": True}


@router.put("/servers/{server_id}/enabled", dependencies=_MANAGEMENT)
async def set_mcp_server_enabled(
    server_id: str, payload: McpEnabledPayload, session: Session = Depends(get_db)
) -> dict[str, object]:
    item = _get_server(server_id, session)
    if not payload.enabled:
        await mcp_manager.disconnect(item.id)
        item.enabled = False
        item.status = "disabled"
        item.last_error = None
        session.commit()
        session.refresh(item)
        return server_dict(item)

    item.enabled = True
    item.status = "connecting"
    item.last_error = None
    session.commit()
    try:
        info, catalog = await mcp_manager.connect(item)
        _save_connection_result(item, info, catalog)
    except Exception as error:
        await mcp_manager.disconnect(item.id)
        current = session.get(McpServer, item.id)
        if current is None:  # pragma: no cover - deletion is serialized by the API
            raise HTTPException(404, "MCP Server 不存在") from error
        current.status = "error"
        current.last_error = safe_error(error)
        current.enabled = True
        session.commit()
        session.refresh(current)
        return server_dict(current)
    session.commit()
    session.refresh(item)
    return server_dict(item)


@router.post("/servers/{server_id}/test", dependencies=_MANAGEMENT)
async def test_mcp_server(server_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    item = _get_server(server_id, session)
    try:
        info, catalog = await mcp_manager.test(item)
    except (McpClientError, McpServerError) as error:
        raise _safe_http_error(error, 502) from error
    return {
        "ok": True,
        "server_info": info,
        "catalog": catalog,
        "catalog_summary": {
            key: len(value) for key, value in catalog.items() if isinstance(value, list)
        },
    }


@router.post("/servers/{server_id}/refresh", dependencies=_MANAGEMENT)
async def refresh_mcp_server(server_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    item = _get_server(server_id, session)
    try:
        info, catalog = await mcp_manager.refresh(item)
        _save_connection_result(item, info, catalog)
        session.commit()
        session.refresh(item)
        return server_dict(item)
    except (McpClientError, McpServerError) as error:
        item.last_error = safe_error(error)
        session.commit()
        raise _safe_http_error(error, 502) from error


@router.get("/servers/{server_id}/catalog", dependencies=_MANAGEMENT)
def get_mcp_catalog(server_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    item = _get_server(server_id, session)
    return {
        "server_id": item.id,
        "status": item.status,
        "catalog": catalog_with_grants(session, item),
    }


@router.put("/servers/{server_id}/grants/{kind}", dependencies=_MANAGEMENT)
def set_mcp_grants(
    server_id: str,
    kind: str,
    payload: McpGrantPayload,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    item = _get_server(server_id, session)
    try:
        replace_grants(session, item, kind, payload.allowed_keys)
        session.commit()
    except McpServerError as error:
        session.rollback()
        raise _safe_http_error(error) from error
    return {"kind": kind, "allowed_keys": payload.allowed_keys}
