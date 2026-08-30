from __future__ import annotations

from datetime import datetime

import pytest
from app.db.models import Job
from app.db.session import SessionLocal
from app.services.confirmations import utc_isoformat
from app.services.jobs import create_manga_download_job
from app.services.runtime import is_owner


def cancel_job(job_id: str) -> None:
    with SessionLocal.begin() as db:
        job = db.get(Job, job_id)
        assert job is not None
        job.status = "cancelled"


def test_owner_allowlist() -> None:
    assert is_owner("local-owner")
    assert is_owner("10001")
    assert not is_owner("20002")


def test_confirmation_time_is_explicit_utc() -> None:
    assert utc_isoformat(datetime(2026, 8, 17, 13, 25, 42)) == "2026-08-17T13:25:42Z"


def test_owner_manga_download_job_does_not_require_confirmation() -> None:
    job = create_manga_download_job("123", "10001")
    assert job.status == "queued"
    cancel_job(job.id)
    with pytest.raises(PermissionError):
        create_manga_download_job("123", "20002")
