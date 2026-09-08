"""Unified tool registration for built-ins, extensions and MCP capabilities.

The agent workflow only consumes the list returned by this module.  MCP
transport/session details stay in :mod:`mcp_client`; this layer is responsible
for turning a safe, administrator-approved catalog entry into a LangChain
tool and for re-checking permissions immediately before every call.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field, create_model
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import McpGrant, McpServer
from app.domain.mcp_routing import McpToolSelection
from app.domain.mcp_types import jsonable
from app.domain.tool_types import ToolContext
from app.services.artifacts import ArtifactError, artifact_envelope, create_artifact
from app.services.builtin_tools import (
    KnowledgeSearchGuard,
    create_builtin_tools,
    tool_envelope,
)
from app.services.context import limit_tool_content
from app.services.extensions import load_python_tools
from app.services.mcp_presets import ANYSEARCH_SLUG
from app.services.mcp_search import ANYSEARCH_TOOL_NAMES, AnySearchCallGuard, is_error_result
from app.services.mcp_servers import McpServerError, mcp_manager

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_TOOL_NAME = 64
_MAX_BLOB_BYTES = 10 * 1024 * 1024
_MCP_STATUS_READY = "ready"


class McpToolDenied(PermissionError):
    """A safe denial returned when an MCP grant or request policy fails."""


def _anysearch_failure() -> str:
    return tool_envelope(
        {
            "error": "AnySearch 本次调用失败，当前信息无法核实",
            "code": "realtime_search_failed",
            "realtime_verified": False,
        }
    )


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed


def _object(value: Any) -> dict[str, Any]:
    parsed = _json(value, value if isinstance(value, dict) else {})
    return parsed if isinstance(parsed, dict) else {}


def _list(value: Any) -> list[Any]:
    parsed = _json(value, value if isinstance(value, list) else [])
    return parsed if isinstance(parsed, list) else []


def _catalog(item: McpServer) -> dict[str, list[dict[str, Any]]]:
    raw = _object(item.catalog_json)
    return {
        key: [entry for entry in _list(raw.get(key)) if isinstance(entry, dict)]
        for key in ("tools", "resources", "resource_templates", "prompts")
    }


def _grant_key(kind: str, entry: dict[str, Any]) -> str:
    return f"{kind}:{entry.get('name') or entry.get('uri') or entry.get('uri_template') or ''}"


def _short_tool_name(prefix: str, remote_name: str) -> str:
    raw = f"mcp__{prefix}__{remote_name}"
    normalized = _SAFE_NAME.sub("_", raw).strip("_") or "mcp__server__tool"
    if len(normalized) <= _MAX_TOOL_NAME:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{normalized[: _MAX_TOOL_NAME - 11]}_{digest}"


def mcp_tool_name(server_slug: str, remote_name: str) -> str:
    """Return the stable public name for a remote MCP Tool."""

    return _short_tool_name(server_slug, remote_name)


def _create_model(name: str, fields: dict[str, Any]) -> type[BaseModel]:
    # Pydantic intentionally accepts dynamic field names here because an MCP
    # schema is remote data rather than a Python function signature.
    return create_model(  # pyright: ignore[reportCallIssue]
        name,
        __config__=ConfigDict(extra="allow"),
        **fields,
    )


def _args_schema(input_schema: Any, name: str) -> type[BaseModel]:
    """Build a permissive Pydantic schema while preserving MCP field names."""

    schema = input_schema if isinstance(input_schema, dict) else {}
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = {str(item) for item in schema.get("required", []) if isinstance(item, str)}
    fields: dict[str, tuple[Any, Any]] = {}
    for key, descriptor in properties.items():
        field_name = str(key)
        description = descriptor.get("description") if isinstance(descriptor, dict) else None
        default: Any = ... if field_name in required else None
        if description:
            default = Field(default, description=str(description)[:500])
        fields[field_name] = (Any, default)
    try:
        return _create_model(f"McpArgs_{hashlib.sha256(name.encode()).hexdigest()[:12]}", fields)
    except (TypeError, ValueError):
        return _create_model(f"McpArgs_{hashlib.sha256(name.encode()).hexdigest()[:12]}", {})


def _fixed_args_schema(name: str, fields: dict[str, tuple[Any, Any]]) -> type[BaseModel]:
    return _create_model(f"McpBridgeArgs_{hashlib.sha256(name.encode()).hexdigest()[:12]}", fields)


def _policy_allows(item: McpServer, context: ToolContext) -> bool:
    if context.is_group or context.platform == "group":
        return False
    if item.access_policy == "owner_only":
        return context.is_owner
    if item.access_policy == "private_users":
        return context.requester_id in {str(value) for value in _list(item.private_users_json)}
    return False


def _authorize(
    session: Session,
    context: ToolContext,
    server_id: str,
    kind: str,
    item_key: str,
    selection: McpToolSelection | None = None,
) -> McpServer:
    if selection is not None and not selection.allows(server_id, item_key):
        raise McpToolDenied("本轮 MCP 意图未绑定该能力")
    item = session.get(McpServer, server_id)
    if item is None or not item.enabled or item.status != _MCP_STATUS_READY:
        raise McpToolDenied("MCP Server 当前未启用或未连接")
    if not _policy_allows(item, context):
        raise McpToolDenied("当前会话没有使用该 MCP 能力的权限")
    grant = session.scalar(
        select(McpGrant).where(
            McpGrant.server_id == server_id,
            McpGrant.kind == kind,
            McpGrant.item_key == item_key,
            McpGrant.allowed.is_(True),
        )
    )
    if grant is None:
        raise McpToolDenied("该 MCP 能力尚未获得管理员授权")
    return item


def _safe_filename(value: str, suffix: str = ".bin") -> str:
    name = _SAFE_NAME.sub("_", str(value or "artifact")).strip("_")[:80] or "artifact"
    if "." not in name:
        name += suffix
    return name


def _mime_suffix(mime_type: str | None) -> str:
    value = str(mime_type or "").casefold()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
    }.get(value, ".bin")


def _decode_blob(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
    if len(decoded) > _MAX_BLOB_BYTES:
        return None
    return decoded


def _mcp_result(
    result: Any,
    *,
    session: Session,
    context: ToolContext,
    label: str,
) -> str:
    """Turn SDK result objects into bounded text and private Artifacts."""

    if is_error_result(result):
        return tool_envelope(
            {
                "error": "MCP 工具返回错误结果，当前信息无法核实",
                "code": "mcp_result_error",
                "realtime_verified": False,
            }
        )
    payload = jsonable(result)
    if not isinstance(payload, dict):
        if payload is None or payload == "" or payload == []:
            return tool_envelope(
                {
                    "error": "MCP 工具未返回可用内容",
                    "code": "mcp_empty_result",
                    "realtime_verified": False,
                }
            )
        return tool_envelope({"server_item": label, "content": limit_tool_content(payload)})
    artifacts: list[dict[str, Any]] = []
    textual: list[Any] = []
    content_items = payload.get("content") or payload.get("contents") or []
    if not isinstance(content_items, list):
        content_items = []
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                message_content = message.get("content")
                if isinstance(message_content, dict):
                    content_items.append(message_content)
                elif message_content is not None:
                    textual.append(message_content)
    for index, entry in enumerate(content_items):
        if not isinstance(entry, dict):
            textual.append(entry)
            continue
        blob = _decode_blob(entry.get("blob"))
        if blob is None and entry.get("type") in {"image", "audio", "resource"}:
            blob = _decode_blob(entry.get("data"))
        if blob is not None:
            try:
                mime = str(entry.get("mimeType") or entry.get("mime_type") or "application/octet-stream")
                artifact = create_artifact(
                    session,
                    owner_id=context.requester_id,
                    conversation_id=context.conversation_id,
                    files=[(_safe_filename(f"{label}-{index}{_mime_suffix(mime)}"), blob)],
                )
                artifacts.append(artifact_envelope(artifact))
            except ArtifactError:
                textual.append({"binary_omitted": True, "reason": "内容超过 Artifact 限制"})
            continue
        if "text" in entry:
            textual.append(entry.get("text"))
        else:
            textual.append(entry)
    if not textual and not artifacts:
        structured = payload.get("structuredContent")
        if structured is not None:
            textual.append(structured)
        elif not any(
            key in payload
            for key in ("content", "contents", "messages", "structuredContent", "isError", "is_error")
        ):
            if payload:
                textual.append(payload)
            else:
                return tool_envelope(
                    {
                        "error": "MCP 工具未返回可用内容",
                        "code": "mcp_empty_result",
                        "realtime_verified": False,
                    }
                )
        else:
            return tool_envelope(
                {
                    "error": "MCP 工具未返回可用内容",
                    "code": "mcp_empty_result",
                    "realtime_verified": False,
                }
            )
    data: dict[str, Any] = {"server_item": label}
    if textual:
        data["content"] = textual
    if artifacts:
        data["artifacts"] = artifacts
    return tool_envelope(data, artifact_ids=[str(item["id"]) for item in artifacts])


def _template_pattern(template: str) -> re.Pattern[str]:
    cursor = 0
    parts: list[str] = []
    for match in re.finditer(r"\{[^{}]+\}", template):
        parts.append(re.escape(template[cursor : match.start()]))
        parts.append(r"[^/]+")
        cursor = match.end()
    parts.append(re.escape(template[cursor:]))
    return re.compile("^" + "".join(parts) + "$")


def _mcp_tool(
    session: Session,
    context: ToolContext,
    item: McpServer,
    entry: dict[str, Any],
    search_guard: AnySearchCallGuard | None = None,
    selection: McpToolSelection | None = None,
) -> BaseTool:
    remote_name = str(entry.get("name") or "remote_tool")
    item_key = str(entry.get("key") or _grant_key("tool", entry))
    public_name = mcp_tool_name(item.slug, remote_name)
    schema = _args_schema(entry.get("input_schema"), public_name)

    async def invoke(**kwargs: Any) -> str:
        if search_guard is not None:
            boundary_error = search_guard.before_call(remote_name, kwargs)
            if boundary_error:
                return tool_envelope(
                    {
                        "error": boundary_error,
                        "code": "vertical_domain_discovery_required",
                    }
                )
        try:
            current = _authorize(session, context, item.id, "tool", item_key, selection)
            result = await mcp_manager.call_tool(current.id, remote_name, kwargs)
            if search_guard is not None:
                if is_error_result(result):
                    return tool_envelope(
                        {
                            "error": "AnySearch 本次调用未返回可用结果",
                            "code": "realtime_search_failed",
                            "realtime_verified": False,
                        }
                    )
                search_guard.after_call(remote_name, kwargs, result)
            return _mcp_result(
                result, session=session, context=context, label=f"{current.slug}:{remote_name}"
            )
        except (McpToolDenied, McpServerError) as error:
            if search_guard is not None:
                return _anysearch_failure()
            return tool_envelope({"error": str(error)})
        except Exception as error:  # third-party failures must not break the graph
            if search_guard is not None:
                return _anysearch_failure()
            return tool_envelope({"error": f"MCP 工具调用失败：{type(error).__name__}"})

    return StructuredTool.from_function(
        coroutine=invoke,
        name=public_name,
        description=str(entry.get("description") or f"调用 MCP Server {item.name} 的 {remote_name}")[:2_000],
        args_schema=schema,
    )


def _resource_tool(
    session: Session,
    context: ToolContext,
    item: McpServer,
    entry: dict[str, Any],
    selection: McpToolSelection | None = None,
) -> BaseTool:
    uri = str(entry.get("uri") or "")
    item_key = str(entry.get("key") or f"resource:{uri}")
    public_name = _short_tool_name(
        item.slug, f"read_resource_{hashlib.sha256(uri.encode()).hexdigest()[:12]}"
    )
    schema = _fixed_args_schema(public_name, {})

    async def invoke(**_kwargs: Any) -> str:
        try:
            current = _authorize(session, context, item.id, "resource", item_key, selection)
            result = await mcp_manager.read_resource(current.id, uri)
            return _mcp_result(result, session=session, context=context, label=f"{current.slug}:{uri}")
        except (McpToolDenied, McpServerError) as error:
            return tool_envelope({"error": str(error)})
        except Exception as error:
            return tool_envelope({"error": f"MCP Resource 读取失败：{type(error).__name__}"})

    return StructuredTool.from_function(
        coroutine=invoke,
        name=public_name,
        description=str(entry.get("description") or f"按需读取 MCP Resource：{uri}")[:2_000],
        args_schema=schema,
    )


def _resource_template_tool(
    session: Session,
    context: ToolContext,
    item: McpServer,
    entry: dict[str, Any],
    selection: McpToolSelection | None = None,
) -> BaseTool:
    template = str(entry.get("uri_template") or "")
    item_key = str(entry.get("key") or f"resource_template:{template}")
    public_name = _short_tool_name(
        item.slug, f"read_template_{hashlib.sha256(template.encode()).hexdigest()[:12]}"
    )
    schema = _fixed_args_schema(public_name, {"uri": (str, ... )})
    matcher = _template_pattern(template)

    async def invoke(uri: str, **_kwargs: Any) -> str:
        if not matcher.fullmatch(uri):
            return tool_envelope({"error": "Resource Template URI 与已授权模板不匹配"})
        try:
            current = _authorize(session, context, item.id, "resource", item_key, selection)
            result = await mcp_manager.read_resource(current.id, uri)
            return _mcp_result(result, session=session, context=context, label=f"{current.slug}:{uri}")
        except (McpToolDenied, McpServerError) as error:
            return tool_envelope({"error": str(error)})
        except Exception as error:
            return tool_envelope({"error": f"MCP Resource Template 读取失败：{type(error).__name__}"})

    return StructuredTool.from_function(
        coroutine=invoke,
        name=public_name,
        description=str(entry.get("description") or f"按需读取 MCP Resource Template：{template}")[:2_000],
        args_schema=schema,
    )


def _prompt_tool(
    session: Session,
    context: ToolContext,
    item: McpServer,
    entry: dict[str, Any],
    selection: McpToolSelection | None = None,
) -> BaseTool:
    prompt_name = str(entry.get("name") or "remote_prompt")
    item_key = str(entry.get("key") or f"prompt:{prompt_name}")
    public_name = _short_tool_name(item.slug, f"get_prompt_{prompt_name}")
    schema = _fixed_args_schema(public_name, {"arguments": (dict[str, Any], Field(default_factory=dict))})

    async def invoke(arguments: dict[str, Any] | None = None, **_kwargs: Any) -> str:
        try:
            current = _authorize(session, context, item.id, "prompt", item_key, selection)
            result = await mcp_manager.get_prompt(
                current.id,
                prompt_name,
                {str(key): str(value) for key, value in (arguments or {}).items()},
            )
            return _mcp_result(
                result, session=session, context=context, label=f"{current.slug}:{prompt_name}"
            )
        except (McpToolDenied, McpServerError) as error:
            return tool_envelope({"error": str(error)})
        except Exception as error:
            return tool_envelope({"error": f"MCP Prompt 获取失败：{type(error).__name__}"})

    return StructuredTool.from_function(
        coroutine=invoke,
        name=public_name,
        description=str(entry.get("description") or f"按需获取 MCP Prompt：{prompt_name}")[:2_000],
        args_schema=schema,
    )


def _mcp_tools(
    session: Session,
    context: ToolContext,
    *,
    allow_anysearch_tools: bool = True,
    search_guard: AnySearchCallGuard | None = None,
    mcp_selection: McpToolSelection | None = None,
) -> list[BaseTool]:
    tools: list[BaseTool] = []
    rows = session.scalars(
        select(McpServer).where(
            McpServer.enabled.is_(True), McpServer.status == _MCP_STATUS_READY
        )
    ).all()
    for item in rows:
        if not _policy_allows(item, context):
            continue
        catalog = _catalog(item)
        grants = {
            grant.item_key
            for grant in session.scalars(
                select(McpGrant).where(McpGrant.server_id == item.id, McpGrant.allowed.is_(True))
            )
        }
        for entry in catalog["tools"]:
            remote_name = str(entry.get("name") or "remote_tool")
            item_key = str(entry.get("key") or _grant_key("tool", entry))
            if mcp_selection is not None and not mcp_selection.allows(item.id, item_key):
                continue
            if item.slug == ANYSEARCH_SLUG and (
                not allow_anysearch_tools or remote_name not in ANYSEARCH_TOOL_NAMES
            ):
                continue
            if item_key in grants:
                tools.append(
                    _mcp_tool(
                        session,
                        context,
                        item,
                        entry,
                        search_guard if item.slug == ANYSEARCH_SLUG else None,
                        mcp_selection,
                    )
                )
        for entry in catalog["resources"]:
            item_key = str(entry.get("key") or _grant_key("resource", entry))
            if (
                (mcp_selection is None or mcp_selection.allows(item.id, item_key))
                and item_key in grants
            ):
                tools.append(_resource_tool(session, context, item, entry, mcp_selection))
        for entry in catalog["resource_templates"]:
            item_key = str(entry.get("key") or _grant_key("resource_template", entry))
            if (
                (mcp_selection is None or mcp_selection.allows(item.id, item_key))
                and item_key in grants
            ):
                tools.append(
                    _resource_template_tool(session, context, item, entry, mcp_selection)
                )
        for entry in catalog["prompts"]:
            item_key = str(entry.get("key") or _grant_key("prompt", entry))
            if (
                (mcp_selection is None or mcp_selection.allows(item.id, item_key))
                and item_key in grants
            ):
                tools.append(_prompt_tool(session, context, item, entry, mcp_selection))
    return tools


def _dedupe(tools: list[BaseTool]) -> list[BaseTool]:
    result: list[BaseTool] = []
    names: set[str] = set()
    for item in tools:
        if item.name in names:
            continue
        names.add(item.name)
        result.append(item)
    return result


def build_registered_tools(
    session: Session,
    context: ToolContext,
    *,
    allowed_manga_actions: frozenset[str] = frozenset(),
    knowledge_search_guard: KnowledgeSearchGuard | None = None,
    manga_download_job_factory: Callable[..., Any] | None = None,
    allow_anysearch_tools: bool = True,
    search_guard: AnySearchCallGuard | None = None,
    mcp_selection: McpToolSelection | None = None,
) -> list[BaseTool]:
    """Return the sole tool list consumed by model binding and ``ToolNode``."""

    builtins = create_builtin_tools(
        session,
        context,
        allowed_manga_actions=allowed_manga_actions,
        knowledge_search_guard=knowledge_search_guard,
        manga_download_job_factory=manga_download_job_factory,
    )
    python_tools = load_python_tools(session, context)
    return _dedupe(
        [
            *builtins,
            *python_tools,
            *_mcp_tools(
                session,
                context,
                allow_anysearch_tools=allow_anysearch_tools,
                search_guard=search_guard,
                mcp_selection=mcp_selection,
            ),
        ]
    )


# Naming aliases make the service convenient for callers and tests while the
# canonical entry point remains build_registered_tools.
build_tool_registry = build_registered_tools
get_registered_tools = build_registered_tools


__all__ = [
    "McpToolDenied",
    "build_registered_tools",
    "build_tool_registry",
    "get_registered_tools",
    "mcp_tool_name",
]
