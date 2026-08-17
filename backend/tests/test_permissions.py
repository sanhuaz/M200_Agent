from __future__ import annotations

from datetime import datetime

import pytest
from app.db.models import Job
from app.db.session import SessionLocal
from app.services.confirmations import (
    create_download_confirmation,
    is_owner,
    resolve_confirmation,
    utc_isoformat,
)
from app.services.jobs import create_manga_download_job


def cancel_job(job_id: str) -> None:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.status = "cancelled"


def test_owner_allowlist_and_confirmation() -> None:
    assert is_owner("local-owner")
    assert is_owner("10001")
    assert not is_owner("20002")
    with pytest.raises(PermissionError):
        create_download_confirmation("123", "20002", None)
    confirmation = create_download_confirmation("123", "10001", None)
    resolved, job = resolve_confirmation(confirmation.token, "10001", True)
    assert resolved.status == "approved"
    assert job is not None
    assert job.type == "manga_download"
    assert job.status == "queued"
    cancel_job(job.id)


def test_confirmation_time_is_explicit_utc() -> None:
    assert utc_isoformat(datetime(2026, 8, 17, 13, 25, 42)) == "2026-08-17T13:25:42Z"


def test_owner_manga_download_job_does_not_require_confirmation() -> None:
    job = create_manga_download_job("123", "10001")
    assert job.status == "queued"
    cancel_job(job.id)
    with pytest.raises(PermissionError):
        create_manga_download_job("123", "20002")
