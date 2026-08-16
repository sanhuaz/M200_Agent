from __future__ import annotations

import pytest
from app.services.confirmations import (
    create_download_confirmation,
    is_owner,
    resolve_confirmation,
)


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
