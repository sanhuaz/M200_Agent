"""Persistence and validation for deterministic MCP intent configuration."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, McpServer, new_id
from app.domain.mcp_types import McpGlobalIntentPayload, McpServerIntentPayload

logger = logging.getLogger(__name__)

MCP_INTENT_SETTING_KEY = "mcp_intent_config_v1"
MCP_INTENT_VERSION = 1
MCP_INTENT_INHERITANCE_TTL_SECONDS = 600
_WHITESPACE_RE = re.compile(r"\s+")

_BUILTIN_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "builtin.anysearch.direct_search",
        "description": "搜索、搜一下、查一下、联网查、网页来源和事实核查等明确联网表达",
        "server_slug": "anysearch",
        "readonly": True,
    },
    {
        "id": "builtin.anysearch.current_fact",
        "description": "最新、实时、今日、当前、最近发布等当前事实核验表达",
        "server_slug": "anysearch",
        "readonly": True,
    },
)


class McpIntentError(ValueError):
    """A safe, user-facing MCP intent configuration error."""


def _empty_config() -> dict[str, Any]:
    return {"version": MCP_INTENT_VERSION, "global_rules": [], "server_rules": {}}


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_config(session: Session) -> dict[str, Any]:
    item = session.get(AppSetting, MCP_INTENT_SETTING_KEY)
    if item is None:
        return _empty_config()
    try:
        parsed = json.loads(item.value or "")
    except (TypeError, ValueError):
        logger.warning("MCP intent configuration is invalid JSON; using empty custom rules")
        return _empty_config()
    if not isinstance(parsed, dict) or not isinstance(parsed.get("global_rules", []), list):
        logger.warning("MCP intent configuration has invalid structure; using empty custom rules")
        return _empty_config()
    server_rules = parsed.get("server_rules", {})
    if not isinstance(server_rules, dict):
        logger.warning("MCP intent server rules have invalid structure; using empty custom rules")
        return _empty_config()
    return {
        "version": MCP_INTENT_VERSION,
        "global_rules": [item for item in parsed["global_rules"] if isinstance(item, dict)],
        "server_rules": {
            str(server_id): value
            for server_id, value in server_rules.items()
            if isinstance(value, dict)
        },
    }


def _save_config(session: Session, config: dict[str, Any]) -> None:
    serialized = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    item = session.get(AppSetting, MCP_INTENT_SETTING_KEY)
    if item is None:
        session.add(AppSetting(key=MCP_INTENT_SETTING_KEY, value=serialized))
    else:
        item.value = serialized
    session.flush()


def normalize_phrase(value: Any) -> str:
    """Return the canonical phrase used for matching and conflict checks."""

    phrase = unicodedata.normalize("NFKC", str(value or ""))
    phrase = _WHITESPACE_RE.sub(" ", phrase).strip().lower()
    if not phrase:
        raise McpIntentError("意图短语不能为空")
    if not any(character.isalnum() or character.isalpha() for character in phrase):
        raise McpIntentError("意图短语不能只有标点或符号")
    return phrase


def _rule_id(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized or new_id()


def _tool_key(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _phrase_rule(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _rule_id(raw.get("id")),
        "phrase": normalize_phrase(raw.get("phrase")),
        "enabled": bool(raw.get("enabled", True)),
    }


def _global_rule(raw: dict[str, Any]) -> dict[str, Any]:
    server_id = str(raw.get("server_id") or "").strip()
    if not server_id:
        raise McpIntentError("全局意图必须指定 Server")
    return {
        "id": _rule_id(raw.get("id")),
        "phrase": normalize_phrase(raw.get("phrase")),
        "server_id": server_id,
        "tool_key": _tool_key(raw.get("tool_key")),
        "enabled": bool(raw.get("enabled", True)),
    }


def _catalog_tools(item: McpServer) -> dict[str, dict[str, Any]]:
    catalog = _json_object(item.catalog_json)
    entries = catalog.get("tools", [])
    if not isinstance(entries, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if key:
            result[key] = entry
    return result


def _existing_global_by_id(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id")): item
        for item in config.get("global_rules", [])
        if isinstance(item, dict) and item.get("id")
    }


def _existing_server_tools(config: dict[str, Any], server_id: str) -> set[str]:
    server = config.get("server_rules", {}).get(server_id, {})
    tools = server.get("tool_rules", {}) if isinstance(server, dict) else {}
    return {str(key) for key in tools} if isinstance(tools, dict) else set()


def _validate_global_rules(
    session: Session, config: dict[str, Any], rules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    servers = {item.id: item for item in session.scalars(select(McpServer))}
    previous = _existing_global_by_id(config)
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    phrases: set[str] = set()
    for raw in rules:
        rule = _global_rule(raw)
        if rule["id"] in ids:
            raise McpIntentError("全局意图规则 ID 不能重复")
        ids.add(rule["id"])
        if rule["phrase"] in phrases:
            raise McpIntentError(f"全局意图短语重复或产生同级冲突：{rule['phrase']}")
        phrases.add(rule["phrase"])
        server = servers.get(rule["server_id"])
        previous_rule = previous.get(rule["id"])
        historical_target = (
            isinstance(previous_rule, dict)
            and previous_rule.get("server_id") == rule["server_id"]
            and _tool_key(previous_rule.get("tool_key")) == rule["tool_key"]
        )
        if server is None and not historical_target:
            raise McpIntentError("全局意图引用的 MCP Server 不存在")
        if server is not None and rule["tool_key"] is not None:
            if rule["tool_key"] not in _catalog_tools(server) and not historical_target:
                raise McpIntentError("全局意图引用的 MCP Tool 不存在于当前目录")
        normalized.append(rule)
    return normalized


def _server_rules_from_config(config: dict[str, Any], server_id: str) -> dict[str, Any]:
    raw = config.get("server_rules", {}).get(server_id, {})
    if not isinstance(raw, dict):
        return {"server_phrases": [], "tool_rules": {}}
    server_phrases = raw.get("server_phrases", [])
    tool_rules = raw.get("tool_rules", {})
    return {
        "server_phrases": [item for item in server_phrases if isinstance(item, dict)],
        "tool_rules": {
            str(key): [item for item in value if isinstance(item, dict)]
            for key, value in tool_rules.items()
            if isinstance(value, list)
        },
    }


def _validate_server_rules(
    session: Session,
    config: dict[str, Any],
    server: McpServer,
    payload: McpServerIntentPayload,
) -> dict[str, Any]:
    previous = _server_rules_from_config(config, server.id)
    catalog_tools = _catalog_tools(server)
    previous_tool_keys = set(previous["tool_rules"])

    server_phrases: list[dict[str, Any]] = []
    server_ids: set[str] = set()
    server_phrase_values: set[str] = set()
    for raw in payload.server_phrases:
        phrase = _phrase_rule(raw.model_dump())
        if phrase["id"] in server_ids:
            raise McpIntentError("Server 意图规则 ID 不能重复")
        if phrase["phrase"] in server_phrase_values:
            raise McpIntentError(f"Server 意图短语重复或产生同级冲突：{phrase['phrase']}")
        server_ids.add(phrase["id"])
        server_phrase_values.add(phrase["phrase"])
        server_phrases.append(phrase)

    tool_rules: dict[str, list[dict[str, Any]]] = {}
    tool_ids: set[str] = set()
    tool_phrase_values: set[str] = set()
    for tool in payload.tools:
        item_key = tool.item_key.strip()
        if item_key in tool_rules:
            raise McpIntentError("Tool 意图规则不能重复配置同一个 Tool")
        if item_key not in catalog_tools and item_key not in previous_tool_keys:
            raise McpIntentError("Tool 意图引用的 Tool 不存在于当前目录")
        phrases: list[dict[str, Any]] = []
        phrase_values: set[str] = set()
        for raw in tool.phrases:
            phrase = _phrase_rule(raw.model_dump())
            if phrase["id"] in tool_ids:
                raise McpIntentError("Tool 意图规则 ID 不能重复")
            if phrase["phrase"] in phrase_values or phrase["phrase"] in tool_phrase_values:
                raise McpIntentError(f"Tool 意图短语重复或产生同级冲突：{phrase['phrase']}")
            tool_ids.add(phrase["id"])
            phrase_values.add(phrase["phrase"])
            tool_phrase_values.add(phrase["phrase"])
            phrases.append(phrase)
        if phrases:
            tool_rules[item_key] = phrases
    return {"server_phrases": server_phrases, "tool_rules": tool_rules}


def _validate_cross_server_conflicts(config: dict[str, Any]) -> None:
    server_phrases: dict[str, str] = {}
    tool_phrases: dict[str, str] = {}
    for server_id, raw in config.get("server_rules", {}).items():
        if not isinstance(raw, dict):
            continue
        for item in raw.get("server_phrases", []):
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            phrase = str(item.get("phrase") or "")
            previous = server_phrases.get(phrase)
            if previous is not None and previous != server_id:
                raise McpIntentError(f"Server 意图短语在多个 Server 间冲突：{phrase}")
            server_phrases[phrase] = str(server_id)
        tools = raw.get("tool_rules", {})
        if not isinstance(tools, dict):
            continue
        for item_key, phrases in tools.items():
            if not isinstance(phrases, list):
                continue
            for item in phrases:
                if not isinstance(item, dict) or not item.get("enabled", True):
                    continue
                phrase = str(item.get("phrase") or "")
                target = f"{server_id}:{item_key}"
                previous = tool_phrases.get(phrase)
                if previous is not None and previous != target:
                    raise McpIntentError(f"Tool 意图短语在多个 Tool 间冲突：{phrase}")
                tool_phrases[phrase] = target


def get_global_intents(session: Session) -> dict[str, Any]:
    config = _load_config(session)
    servers = {
        item.id: item for item in session.scalars(select(McpServer))
    }
    custom_rules: list[dict[str, Any]] = []
    for raw_rule in config["global_rules"]:
        rule = dict(raw_rule)
        server_id = str(rule.get("server_id") or "")
        tool_key = _tool_key(rule.get("tool_key"))
        server = servers.get(server_id)
        catalog_tools = _catalog_tools(server) if server is not None else {}
        catalog_present = server is not None and (
            tool_key is None or tool_key in catalog_tools
        )
        rule["catalog_present"] = catalog_present
        rule["orphaned"] = not catalog_present
        custom_rules.append(rule)
    return {
        "version": MCP_INTENT_VERSION,
        "inheritance_ttl_seconds": MCP_INTENT_INHERITANCE_TTL_SECONDS,
        "builtin_rules": [dict(item) for item in _BUILTIN_RULES],
        "custom_rules": custom_rules,
    }


def load_intent_config(session: Session) -> dict[str, Any]:
    """Return the normalized persisted rules for the runtime router.

    The API-facing helpers intentionally expose a presentation-oriented
    shape.  Runtime routing needs the same validated storage shape without
    reaching into a private loader from another service.
    """

    return _load_config(session)


def replace_global_intents(session: Session, payload: McpGlobalIntentPayload) -> dict[str, Any]:
    config = _load_config(session)
    config["global_rules"] = _validate_global_rules(
        session, config, [item.model_dump() for item in payload.custom_rules]
    )
    _validate_cross_server_conflicts(config)
    _save_config(session, config)
    return get_global_intents(session)


def get_server_intents(session: Session, server: McpServer) -> dict[str, Any]:
    config = _load_config(session)
    stored = _server_rules_from_config(config, server.id)
    catalog_tools = _catalog_tools(server)
    tool_keys = list(catalog_tools)
    tool_keys.extend(key for key in stored["tool_rules"] if key not in catalog_tools)
    tools: list[dict[str, Any]] = []
    for item_key in tool_keys:
        entry = catalog_tools.get(item_key, {})
        tools.append(
            {
                "item_key": item_key,
                "name": entry.get("name") or item_key,
                "title": entry.get("title"),
                "phrases": [dict(item) for item in stored["tool_rules"].get(item_key, [])],
                "catalog_present": item_key in catalog_tools,
            }
        )
    return {
        "server_id": server.id,
        "server_phrases": [dict(item) for item in stored["server_phrases"]],
        "tools": tools,
    }


def replace_server_intents(
    session: Session, server: McpServer, payload: McpServerIntentPayload
) -> dict[str, Any]:
    config = _load_config(session)
    normalized = _validate_server_rules(session, config, server, payload)
    server_rules = config.setdefault("server_rules", {})
    if normalized["server_phrases"] or normalized["tool_rules"]:
        server_rules[server.id] = normalized
    else:
        server_rules.pop(server.id, None)
    _validate_cross_server_conflicts(config)
    _save_config(session, config)
    return get_server_intents(session, server)


__all__ = [
    "MCP_INTENT_INHERITANCE_TTL_SECONDS",
    "MCP_INTENT_SETTING_KEY",
    "MCP_INTENT_VERSION",
    "McpIntentError",
    "get_global_intents",
    "get_server_intents",
    "load_intent_config",
    "normalize_phrase",
    "replace_global_intents",
    "replace_server_intents",
]
