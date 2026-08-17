from __future__ import annotations

import hashlib
import re
import uuid
import zipfile
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Artifact

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 100
MAX_TOTAL_BYTES = 50 * 1024 * 1024
USER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class ArtifactError(ValueError):
    pass


def _safe_filename(filename: str) -> str:
    value = str(filename or "").replace("\\", "/")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or ":" in path.parts[0]
        or len(path.parts) != 1
        or path.name in {"", ".", ".."}
    ):
        raise ArtifactError(f"文件名无效: {filename}")
    return path.name


def _safe_user_id(user_id: str) -> str:
    value = str(user_id or "")
    if USER_PATTERN.fullmatch(value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def create_artifact(
    session: Session,
    *,
    owner_id: str,
    conversation_id: str | None,
    files: list[tuple[str, bytes]],
) -> Artifact:
    if not files or len(files) > MAX_FILES:
        raise ArtifactError(f"单次只能创建 1-{MAX_FILES} 个文件")
    cleaned: list[tuple[str, bytes]] = []
    seen_names: set[str] = set()
    total = 0
    for filename, content in files:
        name = _safe_filename(filename)
        if "\x00" in name or name in seen_names:
            raise ArtifactError(f"文件名重复或包含非法字符: {name}")
        seen_names.add(name)
        if len(content) > MAX_FILE_BYTES:
            raise ArtifactError(f"单文件不能超过 {MAX_FILE_BYTES // 1024 // 1024} MiB: {name}")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ArtifactError("单次文件总大小不能超过 50 MiB")
        cleaned.append((name, content))

    settings = get_settings()
    artifact_id = uuid.uuid4().hex
    destination_dir = settings.workspace_path / "generated" / _safe_user_id(owner_id) / artifact_id
    destination_dir.mkdir(parents=True, exist_ok=False)
    if len(cleaned) == 1:
        filename, content = cleaned[0]
        destination = destination_dir / filename
        destination.write_bytes(content)
        text_suffixes = (".txt", ".md", ".py", ".js", ".ts", ".json")
        content_type = (
            "text/plain" if filename.endswith(text_suffixes) else "application/octet-stream"
        )
    else:
        filename = f"generated-{artifact_id}.zip"
        destination = destination_dir / filename
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in cleaned:
                archive.writestr(name, content)
        content_type = "application/zip"
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    item = Artifact(
        id=artifact_id,
        owner_id=owner_id,
        conversation_id=conversation_id,
        filename=filename,
        path=str(destination.resolve()),
        sha256=digest,
        size=destination.stat().st_size,
        content_type=content_type,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def artifact_envelope(item: Artifact) -> dict[str, object]:
    return {
        "id": item.id,
        "filename": item.filename,
        "path": item.path,
        "size": item.size,
        "sha256": item.sha256,
        "content_type": item.content_type,
    }
