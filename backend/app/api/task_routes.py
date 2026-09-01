from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_loopback
from app.core.config import get_settings
from app.db.models import Confirmation, Job
from app.db.session import get_db
from app.services.confirmations import resolve_confirmation
from app.services.jobs import (
    COMPANION_ANALYSIS_RETRY_JOB_TYPE,
    create_manga_download_job,
    delete_manga_artifact,
)
from app.services.manga import manga_service
from app.services.time_context import utc_isoformat

router = APIRouter()
settings = get_settings()


class MangaSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class MangaDownloadRequest(BaseModel):
    album_id: str = Field(pattern=r"^\d+$")
    requester_id: str = "local-owner"
    conversation_id: str | None = None


class ConfirmationResolve(BaseModel):
    requester_id: str = "local-owner"
    approve: bool = True


class BulkDeleteTokens(BaseModel):
    tokens: list[str] = Field(min_length=1)


class BulkDeleteJobIds(BaseModel):
    ids: list[str] = Field(min_length=1)


def job_dict(item: Job) -> dict[str, object]:
    return {
        "id": item.id,
        "type": item.type,
        "status": item.status,
        "requester_id": item.requester_id,
        "conversation_id": item.conversation_id,
        "payload": json.loads(item.payload),
        "result": json.loads(item.result) if item.result else None,
        "error": item.error,
        "retry_count": item.retry_count,
        "cancel_requested": item.cancel_requested,
        "created_at": utc_isoformat(item.created_at),
        "updated_at": utc_isoformat(item.updated_at),
    }


@router.post("/manga/search")
async def search_manga(payload: MangaSearchRequest) -> dict[str, object]:
    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(manga_service.search, payload.query), timeout=45
        )
        return {"results": results}
    except TimeoutError as error:
        raise HTTPException(504, "漫画搜索超时") from error
    except Exception as error:
        raise HTTPException(502, f"漫画搜索失败: {type(error).__name__}: {error}") from error


@router.post("/manga/download")
def request_manga_download(payload: MangaDownloadRequest) -> dict[str, object]:
    try:
        job = create_manga_download_job(
            payload.album_id,
            payload.requester_id,
            payload.conversation_id,
        )
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"task": job_dict(job), "message": "任务已创建，无需二次确认。"}


@router.post("/confirmations/bulk-delete", dependencies=[Depends(require_loopback)])
def bulk_delete_confirmations(
    payload: BulkDeleteTokens, session: Session = Depends(get_db)
) -> dict[str, int]:
    tokens = list(dict.fromkeys(payload.tokens))
    items = session.scalars(select(Confirmation).where(Confirmation.token.in_(tokens))).all()
    if len(items) != len(tokens):
        raise HTTPException(404, "部分确认记录不存在，未删除任何记录")
    for item in items:
        session.delete(item)
    session.commit()
    return {"deleted": len(items)}


@router.post("/confirmations/{token}")
def confirm(token: str, payload: ConfirmationResolve) -> dict[str, object]:
    try:
        item, job = resolve_confirmation(token, payload.requester_id, payload.approve)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    payload_data = json.loads(item.payload)
    return {
        "confirmation_status": item.status,
        "task": job_dict(job) if job else None,
        "result": payload_data.get("result"),
        "error": payload_data.get("error"),
    }


@router.get("/confirmations")
def list_confirmations(
    status: str = "pending", session: Session = Depends(get_db)
) -> list[dict[str, object]]:
    query = select(Confirmation).order_by(Confirmation.created_at.desc())
    if status != "all":
        query = query.where(Confirmation.status == status)
    items = session.scalars(query).all()
    return [
        {
            "token": item.token,
            "requester_id": item.requester_id,
            "action": item.action,
            "payload": json.loads(item.payload),
            "status": item.status,
            "expires_at": utc_isoformat(item.expires_at),
        }
        for item in items
    ]


@router.get("/tasks")
def list_tasks(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [
        job_dict(item)
        for item in session.scalars(
            select(Job)
            .where(Job.type != COMPANION_ANALYSIS_RETRY_JOB_TYPE)
            .order_by(Job.created_at.desc())
        )
    ]


@router.post("/tasks/{job_id}/cancel")
def cancel_task(job_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    item = session.get(Job, job_id)
    if item is None:
        raise HTTPException(404, "任务不存在")
    if item.status == "queued":
        item.status = "cancelled"
    elif item.status == "running":
        item.cancel_requested = True
    session.commit()
    return job_dict(item)


@router.post("/tasks/bulk-delete", dependencies=[Depends(require_loopback)])
def bulk_delete_tasks(
    payload: BulkDeleteJobIds, session: Session = Depends(get_db)
) -> dict[str, int]:
    ids = list(dict.fromkeys(payload.ids))
    items = session.scalars(select(Job).where(Job.id.in_(ids))).all()
    if len(items) != len(ids):
        raise HTTPException(404, "部分任务记录不存在，未删除任何记录")
    active = [
        item.id for item in items if item.status not in {"succeeded", "failed", "cancelled"}
    ]
    if active:
        raise HTTPException(409, "排队中或运行中的任务不能删除，未删除任何记录")
    for item in items:
        session.delete(item)
    session.commit()
    return {"deleted": len(items)}


@router.get("/tasks/{job_id}/artifact")
def download_artifact(job_id: str, session: Session = Depends(get_db)) -> FileResponse:
    item = session.get(Job, job_id)
    if item is None or item.status != "succeeded" or not item.result:
        raise HTTPException(404, "任务产物不存在")
    result = json.loads(item.result)
    path = Path(result.get("path", "")).resolve()
    if settings.download_path.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "任务产物路径无效")
    return FileResponse(path, filename=path.name)


@router.delete("/tasks/{job_id}/artifact", dependencies=[Depends(require_loopback)])
def delete_task_artifact(job_id: str) -> dict[str, object]:
    try:
        return delete_manga_artifact(job_id, "local-owner")
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
