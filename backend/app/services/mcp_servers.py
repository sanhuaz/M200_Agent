"""MCP Server persistence, secret references and lifecycle management."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT
from app.db.models import McpGrant, McpServer
from app.db.session import SessionLocal
from app.domain.mcp_types import McpServerPayload, McpServerUpdate
from app.services.mcp_client import McpClientError, McpConnection, safe_error
from app.services.time_context import utc_isoformat

SECRET_PREFIX = "PERSONAL_AGENT_MCP_"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,78}[a-z0-9]$|^[a-z0-9]$")
_KEY_RE = re.compile(r"[^A-Za-z0-9]+")
_SENSITIVE_HEADER_RE = re.compile(r"(?i)(authorization|token|secret|password|cookie|credential|api[-_]?key)")
_ALLOWED_KINDS = {"tool", "resource", "prompt"}


class McpServerError(ValueError):
    """A safe, user-facing configuration or lifecycle error."""


class McpServerConflictError(McpServerError):
    """A create/update operation conflicts with an existing server."""


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed


def _json_object(value: str) -> dict[str, Any]:
    parsed = _json(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str) -> list[str]:
    parsed = _json(value, [])
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not normalized:
        normalized = "server-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return normalized[:80].rstrip("-") or "server"


def _validate_slug(value: str) -> str:
    slug = value.strip().casefold()
    if not _SLUG_RE.fullmatch(slug):
        raise McpServerError("slug 只能包含小写字母、数字、下划线和短横线")
    return slug


def _normalize_secret_key(key: str, transport: str) -> str:
    value = key.strip()
    if not value or len(value) > 120:
        raise McpServerError("密钥字段名不能为空且不能超过 120 个字符")
    if value.startswith(("header:", "env:")):
        return value
    return f"header:{value}" if transport in {"sse", "streamable_http"} else f"env:{value}"


def _secret_env_name(slug: str, logical_key: str) -> str:
    slug_label = _KEY_RE.sub("_", slug).strip("_").upper() or "SERVER"
    label = _KEY_RE.sub("_", logical_key.removeprefix("header:").removeprefix("env:")).strip("_")
    label = (label or "VALUE").upper()[:48]
    return f"{SECRET_PREFIX}{slug_label}_{label}"


def _validate_config(transport: str, raw_config: dict[str, Any]) -> dict[str, Any]:
    config = dict(raw_config)
    if transport == "stdio":
        command = str(config.get("command") or "").strip()
        args = config.get("args", [])
        if not command:
            raise McpServerError("stdio 必须配置 command")
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise McpServerError("stdio args 必须是字符串数组")
        config = {"command": command, "args": list(args)}
        if config.get("cwd"):
            config["cwd"] = str(config["cwd"])
        if raw_config.get("cwd"):
            cwd = Path(str(raw_config["cwd"])).expanduser().resolve()
            if not cwd.is_dir():
                raise McpServerError("stdio 工作目录不存在")
            config["cwd"] = str(cwd)
        return config

    url = str(config.get("url") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise McpServerError("SSE/Streamable HTTP 必须配置有效的 http(s) URL")
    if parsed.username or parsed.password:
        raise McpServerError("MCP URL 不允许内嵌用户名或密码")
    headers = config.get("headers", {})
    if not isinstance(headers, dict):
        raise McpServerError("HTTP headers 必须是对象")
    safe_headers: dict[str, str] = {}
    for key, value in headers.items():
        if _SENSITIVE_HEADER_RE.search(str(key)):
            raise McpServerError(f"敏感 Header 必须通过 secret_values 配置：{key}")
        safe_headers[str(key)] = str(value)
    return {"url": url, "headers": safe_headers}


def _write_env_values(values: dict[str, str]) -> None:
    if not values:
        return
    env_path = PROJECT_ROOT / ".env"
    original = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    lines = original.splitlines()
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = env_path.with_suffix(".mcp.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(env_path)
    for key, value in values.items():
        os.environ[key] = value


def _remove_env_values(keys: list[str]) -> None:
    if not keys:
        return
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        for key in keys:
            os.environ.pop(key, None)
        return
    wanted = set(keys)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    output = [
        line
        for line in lines
        if not (
            (match := re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line))
            and match.group(1) in wanted
        )
    ]
    temporary = env_path.with_suffix(".mcp.tmp")
    temporary.write_text("\n".join(output) + ("\n" if output else ""), encoding="utf-8")
    temporary.replace(env_path)
    for key in keys:
        os.environ.pop(key, None)


def _secret_refs_from_payload(
    slug: str,
    transport: str,
    values: dict[str, str],
    existing: dict[str, str] | None = None,
    clear: list[str] | None = None,
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    refs = dict(existing or {})
    env_values: dict[str, str] = {}
    removed: list[str] = []
    for raw_key in clear or []:
        key = _normalize_secret_key(raw_key, transport)
        env_name = refs.pop(key, None)
        if env_name:
            removed.append(env_name)
    for raw_key, value in values.items():
        key = _normalize_secret_key(raw_key, transport)
        if "\r" in value or "\n" in value:
            raise McpServerError("密钥值不能包含换行")
        env_name = refs.get(key) or _secret_env_name(slug, key)
        refs[key] = env_name
        env_values[env_name] = str(value)
    return refs, env_values, removed


def _server_config(item: McpServer) -> dict[str, Any]:
    return _json_object(item.config_json)


def server_dict(item: McpServer) -> dict[str, Any]:
    config = _server_config(item)
    refs = _json_object(item.secret_refs_json)
    catalog = _json_object(item.catalog_json)
    secret_status = {key: bool(os.getenv(str(value))) for key, value in refs.items()}
    return {
        "id": item.id,
        "name": item.name,
        "slug": item.slug,
        "transport": item.transport,
        "config": config,
        "secret_refs": refs,
        "secrets_configured": secret_status,
        "access_policy": item.access_policy,
        "private_users": _json_list(item.private_users_json),
        "enabled": item.enabled,
        "status": item.status,
        "last_error": item.last_error,
        "server_info": _json_object(item.server_info_json),
        "catalog_summary": {
            "tools": len(catalog.get("tools", [])) if isinstance(catalog.get("tools"), list) else 0,
            "resources": len(catalog.get("resources", []))
            if isinstance(catalog.get("resources"), list)
            else 0,
            "resource_templates": len(catalog.get("resource_templates", []))
            if isinstance(catalog.get("resource_templates"), list)
            else 0,
            "prompts": len(catalog.get("prompts", [])) if isinstance(catalog.get("prompts"), list) else 0,
        },
        "last_connected_at": utc_isoformat(item.last_connected_at),
        "last_refreshed_at": utc_isoformat(item.last_refreshed_at),
        "created_at": utc_isoformat(item.created_at),
        "updated_at": utc_isoformat(item.updated_at),
    }


def _unique_slug(session: Session, requested: str | None, name: str) -> str:
    slug = _validate_slug(requested) if requested else _slugify(name)
    if session.scalar(select(McpServer.id).where(McpServer.slug == slug)):
        raise McpServerConflictError("MCP Server slug 已存在")
    return slug


def create_server(session: Session, payload: McpServerPayload) -> McpServer:
    slug = _unique_slug(session, payload.slug, payload.name)
    config = _validate_config(payload.transport, payload.config)
    refs, values, _ = _secret_refs_from_payload(slug, payload.transport, payload.secret_values)
    item = McpServer(
        name=payload.name.strip(),
        slug=slug,
        transport=payload.transport,
        config_json=json.dumps(config, ensure_ascii=False, separators=(",", ":")),
        secret_refs_json=json.dumps(refs, ensure_ascii=False, separators=(",", ":")),
        access_policy=payload.access_policy,
        private_users_json=json.dumps(payload.private_users, ensure_ascii=False),
        enabled=False,
        status="disabled",
        catalog_json=json.dumps({"tools": [], "resources": [], "resource_templates": [], "prompts": []}),
    )
    session.add(item)
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        raise McpServerConflictError("MCP Server slug 已存在") from error
    try:
        _write_env_values(values)
    except Exception as error:
        # The environment file is replaced only after a complete temporary
        # file is written. Remove the flushed row as well on local write
        # failure so no unusable server remains in the transaction.
        session.delete(item)
        session.flush()
        raise McpServerError("本机密钥保存失败，AnySearch Server 未创建") from error
    return item


def update_server(session: Session, item: McpServer, payload: McpServerUpdate) -> tuple[McpServer, bool]:
    next_transport = payload.transport or item.transport
    next_config = _server_config(item) if payload.config is None else payload.config
    config = _validate_config(next_transport, next_config)
    refs, values, removed = _secret_refs_from_payload(
        item.slug,
        next_transport,
        payload.secret_values,
        existing=_json_object(item.secret_refs_json),
        clear=payload.clear_secrets,
    )
    transport_changed = next_transport != item.transport or config != _server_config(item)
    if payload.name is not None:
        item.name = payload.name.strip()
    item.transport = next_transport
    item.config_json = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    item.secret_refs_json = json.dumps(refs, ensure_ascii=False, separators=(",", ":"))
    if payload.access_policy is not None:
        item.access_policy = payload.access_policy
    if payload.private_users is not None:
        item.private_users_json = json.dumps(payload.private_users, ensure_ascii=False)
    if values:
        _write_env_values(values)
    if removed:
        _remove_env_values(removed)
    return item, transport_changed


def delete_server(session: Session, item: McpServer) -> None:
    """Delete a server and only the generated environment variables it owns."""

    refs = _json_object(item.secret_refs_json)
    keys = [str(value) for value in refs.values() if str(value).startswith(SECRET_PREFIX)]
    session.delete(item)
    session.flush()
    _remove_env_values(keys)


def replace_grants(session: Session, item: McpServer, kind: str, allowed_keys: list[str]) -> list[McpGrant]:
    if kind not in _ALLOWED_KINDS:
        raise McpServerError("grant kind 必须是 tool、resource 或 prompt")
    catalog = _json_object(item.catalog_json)
    catalog_key = "resources" if kind == "resource" else f"{kind}s"
    if kind == "prompt":
        catalog_key = "prompts"
    entries = catalog.get(catalog_key, [])
    known = {str(entry.get("key")) for entry in entries if isinstance(entry, dict)}
    if kind == "resource":
        templates = catalog.get("resource_templates", [])
        known.update(str(entry.get("key")) for entry in templates if isinstance(entry, dict))
    unknown = set(allowed_keys).difference(known)
    if unknown:
        raise McpServerError("授权包含当前目录中不存在的项")
    session.execute(delete(McpGrant).where(McpGrant.server_id == item.id, McpGrant.kind == kind))
    grants = [
        McpGrant(server_id=item.id, kind=kind, item_key=key, allowed=True)
        for key in sorted(set(allowed_keys))
    ]
    session.add_all(grants)
    session.flush()
    return grants


def grant_map(session: Session, item_id: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {kind: set() for kind in _ALLOWED_KINDS}
    for grant in session.scalars(
        select(McpGrant).where(McpGrant.server_id == item_id, McpGrant.allowed.is_(True))
    ):
        result.setdefault(grant.kind, set()).add(grant.item_key)
    return result


def catalog_with_grants(session: Session, item: McpServer) -> dict[str, Any]:
    catalog = _json_object(item.catalog_json)
    grants = grant_map(session, item.id)
    for entry in catalog.get("tools", []) if isinstance(catalog.get("tools"), list) else []:
        if isinstance(entry, dict):
            entry["allowed"] = entry.get("key") in grants["tool"]
    for entry in catalog.get("resources", []) if isinstance(catalog.get("resources"), list) else []:
        if isinstance(entry, dict):
            entry["allowed"] = entry.get("key") in grants["resource"]
    if isinstance(catalog.get("resource_templates"), list):
        for entry in catalog["resource_templates"]:
            if isinstance(entry, dict):
                entry["allowed"] = entry.get("key") in grants["resource"]
    for entry in catalog.get("prompts", []) if isinstance(catalog.get("prompts"), list) else []:
        if isinstance(entry, dict):
            entry["allowed"] = entry.get("key") in grants["prompt"]
    return catalog


class McpServerManager:
    """Application-level owner of connected MCP sessions."""

    def __init__(self) -> None:
        self._connections: dict[str, McpConnection] = {}
        self._lock = asyncio.Lock()

    async def connect(self, item: McpServer) -> tuple[dict[str, Any], dict[str, Any]]:
        await self.disconnect(item.id)
        connection = McpConnection(item.id)
        try:
            info = await connection.connect(item)
            catalog = await connection.discover()
        except Exception:
            await connection.close()
            raise
        async with self._lock:
            self._connections[item.id] = connection
        return info, catalog

    async def test(self, item: McpServer) -> tuple[dict[str, Any], dict[str, Any]]:
        connection = McpConnection(item.id)
        try:
            info = await connection.connect(item)
            catalog = await connection.discover()
            return info, catalog
        finally:
            await connection.close()

    async def refresh(self, item: McpServer) -> tuple[dict[str, Any], dict[str, Any]]:
        async with self._lock:
            connection = self._connections.get(item.id)
        if connection is None:
            raise McpServerError("MCP Server 未启用或连接已断开")
        try:
            info = connection.server_info
            catalog = await connection.discover()
            return info, catalog
        except Exception as error:
            raise McpClientError(safe_error(error)) from error

    async def _connection(self, server_id: str) -> McpConnection:
        async with self._lock:
            connection = self._connections.get(server_id)
        if connection is None:
            raise McpServerError("MCP Server 未启用或连接已断开")
        return connection

    async def call_tool(self, server_id: str, name: str, arguments: dict[str, Any]) -> Any:
        connection = await self._connection(server_id)
        try:
            return await connection.call_tool(name, arguments)
        except Exception as error:
            raise McpClientError(safe_error(error)) from error

    async def read_resource(self, server_id: str, uri: str) -> Any:
        connection = await self._connection(server_id)
        try:
            return await connection.read_resource(uri)
        except Exception as error:
            raise McpClientError(safe_error(error)) from error

    async def get_prompt(
        self, server_id: str, name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        connection = await self._connection(server_id)
        try:
            return await connection.get_prompt(name, arguments)
        except Exception as error:
            raise McpClientError(safe_error(error)) from error

    async def disconnect(self, server_id: str) -> None:
        async with self._lock:
            connection = self._connections.pop(server_id, None)
        if connection is not None:
            await connection.close()

    async def startup(self) -> None:
        with SessionLocal() as session:
            servers = list(session.scalars(select(McpServer).where(McpServer.enabled.is_(True))))
        for detached in servers:
            try:
                with SessionLocal.begin() as session:
                    current = session.get(McpServer, detached.id)
                    if current is not None:
                        current.status = "connecting"
                        current.last_error = None
                info, catalog = await self.connect(detached)
                with SessionLocal.begin() as session:
                    current = session.get(McpServer, detached.id)
                    if current is not None:
                        current.status = "ready"
                        current.last_error = None
                        current.server_info_json = json.dumps(info, ensure_ascii=False)
                        current.catalog_json = json.dumps(catalog, ensure_ascii=False)
                        current.last_connected_at = datetime.utcnow()
                        current.last_refreshed_at = datetime.utcnow()
            except Exception as error:
                with SessionLocal.begin() as session:
                    current = session.get(McpServer, detached.id)
                    if current is not None:
                        current.status = "error"
                        current.last_error = safe_error(error)

    async def shutdown(self) -> None:
        async with self._lock:
            connections = list(self._connections.items())
            self._connections.clear()
        for _, connection in connections:
            await connection.close()


mcp_manager = McpServerManager()
