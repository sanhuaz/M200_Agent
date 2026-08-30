from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Conversation, Persona
from app.domain.persona_types import (
    INJECTION_PATTERNS,
    PERSONA_ID_PATTERN,
    PersonaCard,
    PersonaDocument,
    PersonaFileEnvelope,
    PersonaSource,
    string_values,
    validate_persona_card,
)

MAX_PERSONA_FILE_BYTES = 64 * 1024


def _file_time(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).replace(tzinfo=None)
    except OSError:
        return datetime.now(UTC).replace(tzinfo=None)


def _error_text(error: Exception) -> str:
    if isinstance(error, ValidationError):
        first = error.errors()[0] if error.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", "文件结构无效"))
        return f"{location}: {message}" if location else message
    return str(error) or "人格文件无效"


def _validate_provenance(envelope: PersonaFileEnvelope) -> None:
    provenance = {
        "sources": [source.model_dump() for source in envelope.sources],
        "adaptation": envelope.adaptation,
    }
    if any(INJECTION_PATTERNS.search(value) for value in string_values(provenance)):
        raise ValueError("来源或适配说明包含可能改变系统规则或权限的内容")


class PersonaStore:
    """File-authoritative persona storage with a small database index."""

    def __init__(self, root: Path | None = None) -> None:
        configured = root if root is not None else get_settings().persona_path
        self.root = Path(configured).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, persona_id: str) -> Path:
        if not PERSONA_ID_PATTERN.fullmatch(persona_id):
            raise ValueError("人格文件 ID 无效")
        path = (self.root / f"{persona_id}.json").resolve()
        if path.parent != self.root:
            raise ValueError("人格文件路径无效")
        return path

    def new_id(self, name: str, existing_ids: set[str] | None = None) -> str:
        existing = existing_ids or set()
        ascii_name = re.sub(r"[^a-z0-9_-]+", "-", name.casefold()).strip("-_")
        base = (ascii_name or "persona")[:24]
        candidate = base
        if candidate not in existing and PERSONA_ID_PATTERN.fullmatch(candidate):
            return candidate
        while True:
            candidate = f"{base[:19]}-{uuid4().hex[:16]}"
            if candidate not in existing:
                return candidate

    def _invalid(self, path: Path, error: str, *, name: str | None = None) -> PersonaDocument:
        timestamp = _file_time(path)
        return PersonaDocument(
            id=path.stem,
            name=name or path.stem,
            card_version=0,
            card=None,
            status="invalid",
            file_name=path.name,
            validation_error=error,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def _read_one(self, path: Path) -> PersonaDocument:
        file_id = path.stem
        timestamp = _file_time(path)
        if path.is_symlink():
            return self._invalid(path, "人格文件不能是软链接")
        if not PERSONA_ID_PATTERN.fullmatch(file_id):
            return self._invalid(path, "文件名不符合人格 ID 规则")
        try:
            raw_bytes = path.read_bytes()
            if len(raw_bytes) > MAX_PERSONA_FILE_BYTES:
                raise ValueError(f"人格文件不能超过 {MAX_PERSONA_FILE_BYTES:,} 字节")
            raw_text = raw_bytes.decode("utf-8")
            payload = json.loads(raw_text)
            envelope = PersonaFileEnvelope.model_validate(payload)
            if envelope.id != file_id:
                raise ValueError("文件名必须与 JSON 的 id 完全一致")
            validate_persona_card(envelope.card)
            _validate_provenance(envelope)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
            return self._invalid(path, _error_text(error))
        return PersonaDocument(
            id=envelope.id,
            name=envelope.name,
            card_version=envelope.card_version,
            card=envelope.card,
            status="active",
            file_name=path.name,
            created_at=timestamp,
            updated_at=timestamp,
            envelope=envelope,
        )

    def scan(self) -> dict[str, PersonaDocument]:
        entries: dict[str, PersonaDocument] = {}
        try:
            paths = sorted(self.root.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            return entries
        for path in paths:
            if path.suffix.casefold() != ".json" or not path.is_file() and not path.is_symlink():
                continue
            entry = self._read_one(path)
            entries[path.stem] = entry
        names: dict[str, list[str]] = {}
        for entry in entries.values():
            if entry.status == "active":
                names.setdefault(entry.name.casefold(), []).append(entry.id)
        for ids in names.values():
            if len(ids) < 2:
                continue
            for persona_id in ids:
                entry = entries[persona_id]
                entries[persona_id] = PersonaDocument(
                    id=entry.id,
                    name=entry.name,
                    card_version=entry.card_version,
                    card=None,
                    status="invalid",
                    file_name=entry.file_name,
                    validation_error="人格名称重复，需保留唯一名称",
                    created_at=entry.created_at,
                    updated_at=entry.updated_at,
                )
        return entries

    def load(self, persona_id: str | None) -> PersonaDocument | None:
        if not persona_id:
            return None
        return self.scan().get(persona_id)

    def envelope_for(
        self,
        persona_id: str,
        name: str,
        card: PersonaCard,
        *,
        card_version: int = 1,
        sources: list[PersonaSource] | None = None,
        adaptation: str = "",
    ) -> PersonaFileEnvelope:
        envelope = PersonaFileEnvelope(
            id=persona_id,
            name=name,
            card_version=card_version,
            sources=sources or [],
            adaptation=adaptation,
            card=card,
        )
        validate_persona_card(envelope.card)
        _validate_provenance(envelope)
        return envelope

    def write(self, envelope: PersonaFileEnvelope) -> bytes | None:
        path = self._path(envelope.id)
        validate_persona_card(envelope.card)
        _validate_provenance(envelope)
        raw = json.dumps(
            envelope.model_dump(mode="json"), ensure_ascii=False, indent=2
        ).encode("utf-8")
        if len(raw) > MAX_PERSONA_FILE_BYTES:
            raise ValueError(f"人格文件不能超过 {MAX_PERSONA_FILE_BYTES:,} 字节")
        previous = None if path.is_symlink() else (path.read_bytes() if path.exists() else None)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.root, prefix=f".{envelope.id}.", suffix=".tmp", delete=False
            ) as handle:
                temporary = handle.name
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass
        return previous

    def restore(self, persona_id: str, previous: bytes | None) -> None:
        path = self._path(persona_id)
        if previous is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.root, prefix=f".{persona_id}.restore.", suffix=".tmp", delete=False
            ) as handle:
                temporary = handle.name
                handle.write(previous)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary:
                try:
                    Path(temporary).unlink()
                except OSError:
                    pass

    def delete(self, persona_id: str) -> bytes | None:
        path = self._path(persona_id)
        if path.is_symlink():
            path.unlink()
            return None
        previous = path.read_bytes() if path.exists() else None
        if path.exists():
            path.unlink()
        return previous

    def sync_db(self, session: Session, *, prune_missing: bool = True) -> dict[str, PersonaDocument]:
        entries = self.scan()
        rows = {item.id: item for item in session.scalars(select(Persona)).all()}
        if prune_missing:
            for persona_id, item in list(rows.items()):
                if persona_id in entries:
                    continue
                session.query(Conversation).filter(
                    Conversation.persona_id == persona_id
                ).update({Conversation.persona_id: None}, synchronize_session="fetch")
                session.delete(item)
                rows.pop(persona_id, None)

        indexed_names = {
            item.name.casefold(): item.id for item in rows.values() if item.id in entries
        }
        for persona_id, entry in entries.items():
            if not PERSONA_ID_PATTERN.fullmatch(persona_id):
                continue
            item = rows.get(persona_id)
            existing_id = indexed_names.get(entry.name.casefold())
            if existing_id is not None and existing_id != persona_id:
                # Keep the file visible as invalid, but do not violate the DB unique index.
                if item is not None:
                    item.raw_prompt = ""
                    item.card_json = "{}"
                    item.status = "invalid"
                continue
            if item is None:
                item = Persona(
                    id=persona_id,
                    name=entry.name,
                    raw_prompt="",
                    card_json="{}",
                    status=entry.status,
                    card_version=entry.card_version if entry.card_version > 0 else 1,
                )
                session.add(item)
                rows[persona_id] = item
            else:
                item.name = entry.name
                item.raw_prompt = ""
                item.card_json = "{}"
                item.status = entry.status
                item.card_version = entry.card_version if entry.card_version > 0 else item.card_version or 1
            indexed_names[entry.name.casefold()] = persona_id
        return entries


def get_persona_store() -> PersonaStore:
    return PersonaStore()


def persona_file_json(envelope: PersonaFileEnvelope) -> str:
    return json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, indent=2)


__all__ = [
    "MAX_PERSONA_FILE_BYTES",
    "PERSONA_ID_PATTERN",
    "PersonaDocument",
    "PersonaFileEnvelope",
    "PersonaSource",
    "PersonaStore",
    "get_persona_store",
    "persona_file_json",
]
