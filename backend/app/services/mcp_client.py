"""Thin, lifecycle-safe client wrapper around the official MCP SDK.

The wrapper intentionally owns only transport/session mechanics.  Permission
decisions and tool registration are implemented by later service layers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from app.core.config import get_settings
from app.db.models import McpServer
from app.domain.mcp_types import jsonable
from app.services.operation_logs import scrub_text

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 2_000


class McpClientError(RuntimeError):
    """A user-safe MCP connection or protocol error."""


def safe_error(error: BaseException) -> str:
    """Return a short message that cannot expose query strings or secrets."""

    text = scrub_text(str(error)) or type(error).__name__

    def strip_url(match: re.Match[str]) -> str:
        parts = urlsplit(match.group(0).rstrip(".,;"))
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        suffix = match.group(0)[len(match.group(0).rstrip(".,;")) :]
        return clean + suffix

    text = re.sub(r"https?://[^\s)]+", strip_url, text)
    return text[:MAX_ERROR_CHARS]


def _json_object(value: str, *, default: dict[str, Any]) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "")
    except (TypeError, ValueError):
        return default.copy()
    return decoded if isinstance(decoded, dict) else default.copy()


def _json_list(value: str, *, default: list[Any]) -> list[Any]:
    try:
        decoded = json.loads(value or "")
    except (TypeError, ValueError):
        return default.copy()
    return decoded if isinstance(decoded, list) else default.copy()


def _bounded_text(value: Any, limit: int = 4_000) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return {str(key): _bounded_text(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_bounded_text(item, limit) for item in value]
    return value


def _item_key(kind: str, item: Any) -> str:
    if kind == "resource":
        value = getattr(item, "uri", "")
    elif kind == "resource_template":
        value = getattr(item, "uri_template", "")
    else:
        value = getattr(item, "name", "")
    return f"{kind}:{str(value).strip()}"


def _tool_metadata(item: Any) -> dict[str, Any]:
    return {
        "key": _item_key("tool", item),
        "name": str(getattr(item, "name", "")),
        "title": getattr(item, "title", None),
        "description": getattr(item, "description", None),
        "input_schema": _bounded_text(jsonable(getattr(item, "input_schema", {}))),
        "output_schema": _bounded_text(jsonable(getattr(item, "output_schema", None))),
    }


def _resource_metadata(item: Any) -> dict[str, Any]:
    return {
        "key": _item_key("resource", item),
        "name": str(getattr(item, "name", "")),
        "title": getattr(item, "title", None),
        "uri": str(getattr(item, "uri", "")),
        "description": getattr(item, "description", None),
        "mime_type": getattr(item, "mime_type", None),
        "size": getattr(item, "size", None),
    }


def _template_metadata(item: Any) -> dict[str, Any]:
    return {
        "key": _item_key("resource_template", item),
        "name": str(getattr(item, "name", "")),
        "title": getattr(item, "title", None),
        "uri_template": str(getattr(item, "uri_template", "")),
        "description": getattr(item, "description", None),
        "mime_type": getattr(item, "mime_type", None),
    }


def _prompt_metadata(item: Any) -> dict[str, Any]:
    return {
        "key": _item_key("prompt", item),
        "name": str(getattr(item, "name", "")),
        "title": getattr(item, "title", None),
        "description": getattr(item, "description", None),
        "arguments": _bounded_text(jsonable(getattr(item, "arguments", None))),
    }


@dataclass
class McpConnection:
    """One connected server whose SDK resources have a single owner task.

    AnyIO task groups used by the official stdio/SSE transports must be
    entered and exited by the same task.  A small command queue keeps that
    invariant even when an HTTP request enables a server and the FastAPI
    lifespan later disables it.
    """

    server_id: str
    server_info: dict[str, Any] = field(default_factory=dict, init=False)
    _commands: asyncio.Queue[tuple[str, asyncio.Future[Any]]] | None = field(
        default=None, init=False
    )
    _owner_task: asyncio.Task[None] | None = field(default=None, init=False)
    _session_ready: asyncio.Future[dict[str, Any]] | None = field(default=None, init=False)

    async def connect(self, server: McpServer) -> dict[str, Any]:
        if self._owner_task is not None:
            raise McpClientError("MCP Server 连接已存在")
        loop = asyncio.get_running_loop()
        self._commands = asyncio.Queue()
        self._session_ready = loop.create_future()
        self._owner_task = asyncio.create_task(self._run_owner(server), name=f"mcp-{self.server_id}")
        try:
            return await self._session_ready
        except Exception as error:
            await self.close()
            if isinstance(error, McpClientError):
                raise
            raise McpClientError(safe_error(error)) from error

    async def _run_owner(self, server: McpServer) -> None:
        stack = AsyncExitStack()
        ready = self._session_ready
        try:
            settings = get_settings()
            connect_timeout = float(settings.mcp_connect_timeout_seconds)
            read_timeout = float(settings.mcp_read_timeout_seconds)
            config = _json_object(server.config_json, default={})
            refs = _json_object(server.secret_refs_json, default={})
            headers = _resolve_headers(config, refs)
            if server.transport == "stdio":
                streams = await stack.enter_async_context(
                    stdio_client(_stdio_parameters(config, refs))
                )
            elif server.transport == "sse":
                streams = await stack.enter_async_context(
                    sse_client(
                        _http_url(config),
                        headers=headers,
                        timeout=connect_timeout,
                        sse_read_timeout=read_timeout,
                    )
                )
            elif server.transport == "streamable_http":
                client = httpx2.AsyncClient(
                    headers=headers,
                    timeout=httpx2.Timeout(connect_timeout, read=read_timeout),
                )
                await stack.enter_async_context(client)
                streams = await stack.enter_async_context(
                    streamable_http_client(_http_url(config), http_client=client, terminate_on_close=True)
                )
            else:
                raise McpClientError(f"不支持的 MCP Transport：{server.transport}")

            session = await stack.enter_async_context(
                ClientSession(streams[0], streams[1], read_timeout_seconds=read_timeout)
            )
            initialize = await asyncio.wait_for(session.initialize(), timeout=connect_timeout)
            info = _bounded_text(jsonable(getattr(initialize, "server_info", None)), limit=2_000)
            self.server_info = info if isinstance(info, dict) else {}
            if ready is not None and not ready.done():
                ready.set_result(self.server_info)

            while True:
                operation, future = await self._commands.get()  # type: ignore[union-attr]
                if operation == "close":
                    if not future.done():
                        future.set_result(None)
                    break
                try:
                    if operation == "discover":
                        result = await self._discover_session(session, read_timeout)
                    else:  # pragma: no cover - guarded by _request
                        raise McpClientError(f"未知 MCP 操作：{operation}")
                except Exception as error:
                    if not future.done():
                        future.set_exception(
                            error
                            if isinstance(error, McpClientError)
                            else McpClientError(safe_error(error))
                        )
                else:
                    if not future.done():
                        future.set_result(result)
        except Exception as error:
            wrapped = error if isinstance(error, McpClientError) else McpClientError(safe_error(error))
            if ready is not None and not ready.done():
                ready.set_exception(wrapped)
            if self._commands is not None:
                while not self._commands.empty():
                    _, future = self._commands.get_nowait()
                    if not future.done():
                        future.set_exception(wrapped)
        finally:
            try:
                await stack.aclose()
            except Exception as error:
                logger.debug("MCP 连接关闭异常：%s", safe_error(error))
            self.server_info = {}

    async def _request(self, operation: str) -> Any:
        if self._owner_task is None or self._commands is None or self._owner_task.done():
            raise McpClientError("MCP Server 尚未连接")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._commands.put((operation, future))
        return await future

    async def _discover_session(
        self, session: ClientSession, read_timeout: float
    ) -> dict[str, list[dict[str, Any]]]:
        limit = get_settings().mcp_catalog_item_limit
        catalog: dict[str, list[dict[str, Any]]] = {
            "tools": [],
            "resources": [],
            "resource_templates": [],
            "prompts": [],
        }

        async def fetch(method_name: str, result_name: str) -> None:
            try:
                result = await asyncio.wait_for(getattr(session, method_name)(), timeout=read_timeout)
                items = list(getattr(result, result_name, []) or [])[:limit]
                if result_name == "tools":
                    catalog["tools"] = [_tool_metadata(item) for item in items]
                elif result_name == "resources":
                    catalog["resources"] = [_resource_metadata(item) for item in items]
                elif result_name == "resource_templates":
                    catalog["resource_templates"] = [_template_metadata(item) for item in items]
                else:
                    catalog["prompts"] = [_prompt_metadata(item) for item in items]
            except Exception as error:
                logger.debug("MCP catalog capability unavailable: %s", safe_error(error))

        await fetch("list_tools", "tools")
        await fetch("list_resources", "resources")
        await fetch("list_resource_templates", "resource_templates")
        await fetch("list_prompts", "prompts")
        return catalog

    async def discover(self) -> dict[str, list[dict[str, Any]]]:
        return await self._request("discover")

    async def close(self) -> None:
        task = self._owner_task
        if task is None:
            return
        ready_failed = (
            self._session_ready is not None
            and self._session_ready.done()
            and not self._session_ready.cancelled()
            and self._session_ready.exception() is not None
        )
        if not task.done() and not ready_failed and self._commands is not None:
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            await self._commands.put(("close", future))
            try:
                await asyncio.wait_for(future, timeout=5)
            except Exception:
                pass
        try:
            await task
        except Exception:
            pass
        self._owner_task = None
        self._commands = None
        self._session_ready = None


def _http_url(config: dict[str, Any]) -> str:
    url = str(config.get("url") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise McpClientError("SSE/Streamable HTTP 必须配置有效的 http(s) URL")
    if parsed.username or parsed.password:
        raise McpClientError("MCP URL 不允许内嵌用户名或密码")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _stdio_parameters(config: dict[str, Any], refs: dict[str, Any]) -> StdioServerParameters:
    command = str(config.get("command") or "").strip()
    if not command:
        raise McpClientError("stdio 必须配置 command")
    raw_args = config.get("args", [])
    if not isinstance(raw_args, list) or any(not isinstance(item, str) for item in raw_args):
        raise McpClientError("stdio args 必须是字符串数组")
    cwd_value = config.get("cwd")
    cwd: str | None = None
    if cwd_value:
        cwd_path = Path(str(cwd_value)).expanduser().resolve()
        if not cwd_path.is_dir():
            raise McpClientError("stdio 工作目录不存在")
        cwd = str(cwd_path)
    environment = os.environ.copy()
    for logical, env_name in refs.items():
        if str(logical).startswith("env:"):
            value = os.getenv(str(env_name))
            if value is not None:
                environment[str(logical)[4:]] = value
    return StdioServerParameters(command=command, args=list(raw_args), cwd=cwd, env=environment)


def _resolve_headers(config: dict[str, Any], refs: dict[str, Any]) -> dict[str, str]:
    raw_headers = config.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise McpClientError("HTTP headers 必须是对象")
    headers = {str(key): str(value) for key, value in raw_headers.items()}
    for logical, env_name in refs.items():
        key = str(logical)
        if key.startswith("header:"):
            value = os.getenv(str(env_name))
            if value is not None:
                headers[key[7:]] = value
    return headers
