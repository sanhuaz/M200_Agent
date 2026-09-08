"""Static, non-sensitive MCP preset definitions."""

from __future__ import annotations

from app.domain.mcp_types import AnySearchPresetPayload, McpPreset, McpServerPayload

ANYSEARCH_NAME = "AnySearch"
ANYSEARCH_SLUG = "anysearch"
ANYSEARCH_URL = "https://api.anysearch.com/mcp"
ANYSEARCH_CLIENT_HEADER = "PersonalAgent/0.4.7"


def anysearch_preset(*, installed: bool = False) -> McpPreset:
    """Return the public AnySearch descriptor without secret material."""

    return McpPreset(
        name=ANYSEARCH_NAME,
        slug=ANYSEARCH_SLUG,
        transport="streamable_http",
        url=ANYSEARCH_URL,
        supports_anonymous=True,
        supports_api_key=True,
        risk_note="远程搜索服务；仅在明确需要当前信息时调用，API Key 只保存在本机环境变量。",
        installed=installed,
    )


def anysearch_server_payload(payload: AnySearchPresetPayload) -> McpServerPayload:
    """Build the regular MCP create payload without DB or network access."""

    secret_values: dict[str, str] = {}
    if payload.api_key:
        secret_values["Authorization"] = f"Bearer {payload.api_key}"
    return McpServerPayload(
        name=ANYSEARCH_NAME,
        slug=ANYSEARCH_SLUG,
        transport="streamable_http",
        config={
            "url": ANYSEARCH_URL,
            "headers": {"X-Anysearch-Client": ANYSEARCH_CLIENT_HEADER},
        },
        secret_values=secret_values,
        access_policy="owner_only",
        private_users=[],
    )


__all__ = [
    "ANYSEARCH_NAME",
    "ANYSEARCH_SLUG",
    "ANYSEARCH_URL",
    "anysearch_preset",
    "anysearch_server_payload",
]
