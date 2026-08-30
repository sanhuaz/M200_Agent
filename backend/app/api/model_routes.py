from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import require_loopback
from app.db.session import get_db
from app.services.model_profiles import (
    create_profile,
    delete_profile,
    list_profiles,
    set_default_profile,
    test_connection,
    update_profile,
)
from app.services.models import model_registry

router = APIRouter()


class ModelProfileCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
    model: str = Field(min_length=1, max_length=240)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    streaming: bool = True
    temperature: float | None = Field(default=None, ge=0, le=2)
    context_window: int = Field(default=1_000_000, gt=0)
    input_soft_limit: int = Field(default=131_072, gt=0)
    max_output_tokens: int = Field(default=16_384, gt=0)
    timeout_seconds: float = Field(default=120, gt=0)


class ModelProfileUpdate(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=240)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    clear_api_key: bool = False
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    streaming: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    context_window: int | None = Field(default=None, gt=0)
    input_soft_limit: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)


class ModelConnectionTest(ModelProfileCreate):
    pass


@router.get("/models", dependencies=[Depends(require_loopback)])
def list_models(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return list_profiles(session)


@router.post("/models", dependencies=[Depends(require_loopback)])
def create_model_profile(
    payload: ModelProfileCreate, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        profile = create_profile(session, payload.model_dump(exclude={"api_key"}), payload.api_key)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return next(item for item in list_profiles(session) if item["alias"] == profile.alias)


@router.post("/models/test-connection", dependencies=[Depends(require_loopback)])
def test_model_connection(payload: ModelConnectionTest) -> dict[str, object]:
    existing_alias = None
    try:
        model_registry.profile(payload.alias)
        existing_alias = payload.alias
    except ValueError:
        pass
    try:
        return test_connection(
            payload.model_dump(exclude={"api_key"}), payload.api_key, existing_alias=existing_alias
        )
    except (RuntimeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


@router.put("/models/{alias}", dependencies=[Depends(require_loopback)])
def edit_model_profile(
    alias: str, payload: ModelProfileUpdate, session: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        profile = update_profile(
            session,
            alias,
            payload.model_dump(exclude_unset=True, exclude={"api_key", "clear_api_key"}),
            payload.api_key,
            payload.clear_api_key,
        )
    except KeyError as error:
        raise HTTPException(404, f"未知模型配置: {error.args[0]}") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return next(item for item in list_profiles(session) if item["alias"] == profile.alias)


@router.put("/models/{alias}/default", dependencies=[Depends(require_loopback)])
def make_default_model(alias: str, session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        profile = set_default_profile(session, alias)
    except KeyError as error:
        raise HTTPException(404, f"未知模型配置: {error.args[0]}") from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return next(item for item in list_profiles(session) if item["alias"] == profile.alias)


@router.delete("/models/{alias}", dependencies=[Depends(require_loopback)])
def remove_model_profile(alias: str, session: Session = Depends(get_db)) -> dict[str, bool]:
    try:
        delete_profile(session, alias)
    except KeyError as error:
        raise HTTPException(404, f"未知模型配置: {error.args[0]}") from error
    except PermissionError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {"deleted": True}
