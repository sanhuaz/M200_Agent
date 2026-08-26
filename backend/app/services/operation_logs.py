from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT

MAX_MEMORY_EVENTS = 2_000
MAX_EVENT_BYTES = 64 * 1024
MAX_PART_BYTES = 50 * 1024 * 1024
MAX_STRING_CHARS = 16_384
QUEUE_SIZE = 500

_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|password|passwd|secret|cookie|credential)"
    r"(\s*[:=]\s*)([\"']?)([^\s,;\"']+)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_KEY_PREFIX_RE = re.compile(r"(?i)\b(?:sk|pk|ghp|github_pat)-[A-Za-z0-9_-]{12,}\b")
_SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "api-key",
    "access_token",
    "accesstoken",
    "authorization",
    "cookie",
    "credential",
    "napcat_token",
    "napcattoken",
    "password",
    "passwd",
    "refresh_token",
    "secret",
    "token",
    "webui_token",
}
_TERMINAL_KINDS = {"succeeded", "failed", "cancelled", "finished"}


def utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _truncate(value: str, limit: int = MAX_STRING_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"…[已截断，原长度 {len(value)}]"


def scrub_text(value: str) -> str:
    value = _BEARER_RE.sub(r"\1***", value)
    value = _SECRET_KEY_RE.sub(lambda match: f"{match.group(1)}=***", value)
    value = _KEY_PREFIX_RE.sub("***", value)
    return _truncate(value)


def scrub_value(value: Any, *, field_name: str = "", depth: int = 0) -> Any:
    normalized_field = field_name.casefold().replace("-", "_")
    compact_field = normalized_field.replace("_", "")
    if normalized_field in _SECRET_FIELD_NAMES or compact_field in _SECRET_FIELD_NAMES:
        return "***"
    if depth > 8:
        return "…[嵌套已截断]"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            str(key): scrub_value(item, field_name=str(key), depth=depth + 1) for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [scrub_value(item, depth=depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[二进制内容已省略]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub_text(str(value))


def _event_size(event: dict[str, Any]) -> int:
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class _Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.dropped = 0

    def offer(self, event: dict[str, Any]) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped += 1
        self.queue.put_nowait(event)


class _EventLogHandler(logging.Handler):
    def __init__(self, center: OperationLogCenter) -> None:
        super().__init__()
        self.center = center

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return
        try:
            level = record.levelname.casefold()
            if level == "warning":
                level = "warn"
            message = self.format(record)
            details: dict[str, Any] = {"logger": record.name}
            if record.exc_info:
                formatter = self.formatter or logging.Formatter()
                details["exception"] = formatter.formatException(record.exc_info)
            self.center.emit(
                source="backend",
                level=level,
                kind="message" if record.levelno < logging.ERROR else "failed",
                title=record.name,
                message=message,
                details=details,
            )
        except Exception:
            # 日志处理器不能反过来打断业务请求。
            return


class OperationLogCenter:
    def __init__(self) -> None:
        session_id = os.getenv("PERSONAL_AGENT_LOG_SESSION_ID") or self._new_session_id()
        configured_dir = os.getenv("PERSONAL_AGENT_LOG_DIR")
        session_dir = (
            Path(configured_dir) if configured_dir else PROJECT_ROOT / "logs" / "current" / session_id
        )
        self.session_id = session_id
        self.session_dir = session_dir.resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_MEMORY_EVENTS)
        self._active: dict[str, dict[str, Any]] = {}
        self._subscribers: set[_Subscriber] = set()
        self._lock = threading.RLock()
        self._sequence = 0
        self._part_index = 1
        self._part_handle = None
        self._handler: _EventLogHandler | None = None
        self._load_existing()
        self._write_metadata()

    @staticmethod
    def _new_session_id() -> str:
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]

    @property
    def metadata_path(self) -> Path:
        return self.session_dir / "session.json"

    def _load_existing(self) -> None:
        bootstrap = self.session_dir / "bootstrap.log"
        if bootstrap.is_file():
            for index, line in enumerate(bootstrap.read_text(encoding="utf-8").splitlines(), start=1):
                if line.strip():
                    self._events.append(
                        {
                            "id": f"{self.session_id}:bootstrap:{index}",
                            "timestamp": utc_iso(),
                            "session_id": self.session_id,
                            "source": "startup",
                            "level": "info",
                            "kind": "message",
                            "title": "项目启动",
                            "message": scrub_text(line),
                            "details": {},
                        }
                    )
        parts = sorted(self.session_dir.glob("events-*.jsonl"))
        for path in parts:
            try:
                for raw in path.read_text(encoding="utf-8").splitlines():
                    item = json.loads(raw)
                    if isinstance(item, dict) and item.get("id"):
                        self._events.append(item)
                        operation_id = item.get("operation_id")
                        if operation_id:
                            if item.get("kind") in {"started", "progress"}:
                                self._active[str(operation_id)] = item
                            elif item.get("kind") in _TERMINAL_KINDS:
                                self._active.pop(str(operation_id), None)
                        sequence = str(item["id"]).rsplit(":", 1)[-1]
                        if sequence.isdigit():
                            self._sequence = max(self._sequence, int(sequence))
            except (OSError, json.JSONDecodeError):
                continue
        if parts:
            try:
                self._part_index = max(int(path.stem.split("-")[-1]) for path in parts)
            except ValueError:
                self._part_index = len(parts)

    def _write_metadata(self) -> None:
        metadata = {
            "session_id": self.session_id,
            "started_at": utc_iso(),
            "pid": os.getpid(),
            "format": "m200-agent-operation-log-v1",
        }
        self.metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _open_part(self):
        if self._part_handle is None:
            path = self.session_dir / f"events-{self._part_index:04d}.jsonl"
            self._part_handle = path.open("a", encoding="utf-8", newline="\n")
        return self._part_handle

    def _write_file(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded_size = len(payload.encode("utf-8"))
        handle = self._open_part()
        current_size = handle.tell()
        if current_size and current_size + encoded_size > MAX_PART_BYTES:
            handle.flush()
            handle.close()
            self._part_handle = None
            self._part_index += 1
            handle = self._open_part()
        handle.write(payload)
        handle.flush()

    def _broadcast(self, event: dict[str, Any]) -> None:
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, event)
            except RuntimeError:
                self._subscribers.discard(subscriber)

    def emit(
        self,
        *,
        source: str,
        level: str = "info",
        kind: str = "message",
        title: str,
        message: str = "",
        details: Any = None,
        operation_id: str | None = None,
        trace_id: str | None = None,
        parent_operation_id: str | None = None,
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            event: dict[str, Any] = {
                "id": f"{self.session_id}:{self._sequence}",
                "timestamp": utc_iso(),
                "session_id": self.session_id,
                "source": scrub_text(source),
                "level": scrub_text(level),
                "kind": scrub_text(kind),
                "title": scrub_text(title),
                "message": scrub_text(message),
                "details": scrub_value(details if details is not None else {}),
            }
            if operation_id:
                event["operation_id"] = scrub_text(operation_id)
            if trace_id:
                event["trace_id"] = scrub_text(trace_id)
            if parent_operation_id:
                event["parent_operation_id"] = scrub_text(parent_operation_id)
            if progress is not None:
                event["progress"] = scrub_value(progress)
            event_size = _event_size(event)
            if event_size > MAX_EVENT_BYTES:
                event["details"] = {"truncated": True, "original_size": event_size}
                event["source"] = _truncate(str(event["source"]), 128)
                event["level"] = _truncate(str(event["level"]), 32)
                event["kind"] = _truncate(str(event["kind"]), 64)
                event["title"] = _truncate(str(event["title"]), 1_000)
                event["message"] = _truncate(str(event["message"]), 4_000)
            self._events.append(event)
            if operation_id:
                if kind == "started":
                    self._active[operation_id] = event
                elif kind in _TERMINAL_KINDS:
                    self._active.pop(operation_id, None)
                elif kind == "progress":
                    self._active[operation_id] = event
            self._write_file(event)
            self._broadcast(event)
            return event

    def start_operation(self, *, source: str, title: str, message: str = "", **kwargs: Any) -> str:
        operation_id = kwargs.pop("operation_id", None) or uuid.uuid4().hex
        self.emit(
            source=source,
            title=title,
            message=message,
            kind="started",
            operation_id=operation_id,
            **kwargs,
        )
        return operation_id

    def update_operation(
        self, operation_id: str, *, source: str, title: str, message: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        return self.emit(
            source=source,
            title=title,
            message=message,
            kind="progress",
            operation_id=operation_id,
            **kwargs,
        )

    def finish_operation(
        self,
        operation_id: str,
        *,
        source: str,
        title: str,
        message: str = "",
        success: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.emit(
            source=source,
            title=title,
            message=message,
            kind="succeeded" if success else "failed",
            level="info" if success else "error",
            operation_id=operation_id,
            **kwargs,
        )

    def snapshot(
        self,
        *,
        source: str | None = None,
        level: str | None = None,
        query: str | None = None,
        limit: int = 500,
        after_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events)
        if after_id:
            ids = [item.get("id") for item in events]
            if after_id in ids:
                events = events[ids.index(after_id) + 1 :]
        if source:
            events = [item for item in events if item.get("source") == source]
        if level:
            events = [item for item in events if item.get("level") == level]
        if query:
            folded = query.casefold()
            events = [item for item in events if folded in json.dumps(item, ensure_ascii=False).casefold()]
        return events[-max(1, min(limit, MAX_MEMORY_EVENTS)) :]

    def active_operations(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._active.values())

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> _Subscriber:
        subscriber = _Subscriber(loop)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def install_logging_handler(self) -> None:
        with self._lock:
            if self._handler is not None:
                return
            handler = _EventLogHandler(self)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logging.getLogger().addHandler(handler)
            self._handler = handler

    def flush(self) -> None:
        with self._lock:
            if self._part_handle is not None:
                self._part_handle.flush()

    def close(self) -> None:
        with self._lock:
            self.flush()
            if self._part_handle is not None:
                self._part_handle.close()
                self._part_handle = None
            if self._handler is not None:
                logging.getLogger().removeHandler(self._handler)
                self._handler = None


operation_logs = OperationLogCenter()
