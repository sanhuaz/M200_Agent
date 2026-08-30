from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_loopback
from app.db.models import ExtensionPackage, ToolRun
from app.db.session import get_db
from app.services.extensions import (
    ExtensionError,
    delete_package,
    import_github,
    import_zip,
    list_packages,
    package_dict,
    set_package_state,
)

router = APIRouter()


class ExtensionGithubImport(BaseModel):
    url: str = Field(min_length=1, max_length=500)


class ExtensionState(BaseModel):
    enabled: bool
    access_policy: str | None = None


def _extension_response(item: ExtensionPackage) -> dict[str, object]:
    return package_dict(item)


@router.get("/tools")
def list_tools(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in list_packages(session, "tool"):
        row = _extension_response(item)
        row["recent_runs"] = [
            {
                "id": run.id,
                "conversation_id": run.conversation_id,
                "status": run.status,
                "created_at": run.created_at.isoformat(),
            }
            for run in session.scalars(
                select(ToolRun)
                .where(ToolRun.tool_name == item.name)
                .order_by(ToolRun.created_at.desc())
                .limit(10)
            )
        ]
        result.append(row)
    return result


async def _import_package(
    kind: str, label: str, file: UploadFile, session: Session
) -> dict[str, object]:
    content = await file.read(20 * 1024 * 1024 + 1)
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, f"{label} 包不能超过 20 MiB")
    try:
        item = import_zip(
            session,
            kind,
            content,
            Path(file.filename or f"{kind}.zip").name,
        )
    except (ExtensionError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    return _extension_response(item)


@router.post("/tools/import", dependencies=[Depends(require_loopback)])
async def import_tool(
    file: UploadFile = File(...), session: Session = Depends(get_db)
) -> dict[str, object]:
    return await _import_package("tool", "Tool", file, session)


@router.post("/tools/import/github", dependencies=[Depends(require_loopback)])
def import_tool_github(
    payload: ExtensionGithubImport, session: Session = Depends(get_db)
) -> dict[str, object]:
    return _import_github_package("tool", "Tool", payload.url, session)


@router.get("/skills")
def list_skills(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [_extension_response(item) for item in list_packages(session, "skill")]


@router.post("/skills/import", dependencies=[Depends(require_loopback)])
async def import_skill(
    file: UploadFile = File(...), session: Session = Depends(get_db)
) -> dict[str, object]:
    return await _import_package("skill", "Skill", file, session)


@router.post("/skills/import/github", dependencies=[Depends(require_loopback)])
def import_skill_github(
    payload: ExtensionGithubImport, session: Session = Depends(get_db)
) -> dict[str, object]:
    return _import_github_package("skill", "Skill", payload.url, session)


def _import_github_package(
    kind: str, label: str, url: str, session: Session
) -> dict[str, object]:
    try:
        item = import_github(session, kind, url)
    except Exception as error:
        raise HTTPException(
            400, f"GitHub {label} 导入失败: {type(error).__name__}: {error}"
        ) from error
    return _extension_response(item)


def _set_extension_state(
    kind: str, name: str, payload: ExtensionState, session: Session
) -> dict[str, object]:
    try:
        item = set_package_state(
            session,
            kind,
            name,
            enabled=payload.enabled,
            access_policy=payload.access_policy,
        )
    except ExtensionError as error:
        status_code = 404 if str(error) == "扩展不存在" else 400
        raise HTTPException(status_code, str(error)) from error
    session.commit()
    session.refresh(item)
    return _extension_response(item)


def _get_extension(kind: str, label: str, name: str, session: Session) -> dict[str, object]:
    item = session.scalar(
        select(ExtensionPackage).where(
            ExtensionPackage.kind == kind, ExtensionPackage.name == name
        )
    )
    if item is None:
        raise HTTPException(404, f"{label} 不存在")
    return _extension_response(item)


@router.get("/tools/{name}")
def get_tool(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _get_extension("tool", "Tool", name, session)


@router.post("/tools/{name}/enable", dependencies=[Depends(require_loopback)])
def enable_tool(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _set_extension_state("tool", name, ExtensionState(enabled=True), session)


@router.post("/tools/{name}/disable", dependencies=[Depends(require_loopback)])
def disable_tool(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _set_extension_state("tool", name, ExtensionState(enabled=False), session)


@router.delete("/tools/{name}", dependencies=[Depends(require_loopback)])
def delete_tool(name: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    return _delete_extension("tool", name, session)


@router.post("/skills/{name}/enable", dependencies=[Depends(require_loopback)])
def enable_skill(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _set_extension_state("skill", name, ExtensionState(enabled=True), session)


@router.get("/skills/{name}")
def get_skill(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _get_extension("skill", "Skill", name, session)


@router.post("/skills/{name}/disable", dependencies=[Depends(require_loopback)])
def disable_skill(name: str, session: Session = Depends(get_db)) -> dict[str, object]:
    return _set_extension_state("skill", name, ExtensionState(enabled=False), session)


@router.delete("/skills/{name}", dependencies=[Depends(require_loopback)])
def delete_skill(name: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    return _delete_extension("skill", name, session)


def _delete_extension(kind: str, name: str, session: Session) -> dict[str, bool]:
    try:
        delete_package(session, kind, name)
    except ExtensionError as error:
        status_code = 404 if str(error) == "扩展不存在" else 400
        raise HTTPException(status_code, str(error)) from error
    session.commit()
    return {"deleted": True}
