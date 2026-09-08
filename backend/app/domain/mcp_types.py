"""Domain contracts for configuring and observing MCP servers.

The API and persistence layers use plain JSON-compatible values from this
module.  The MCP SDK objects stay inside ``services.mcp_client`` so the rest
of the application does not depend on a particular transport implementation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

McpTransport = Literal["stdio", "sse", "streamable_http"]
McpAccessPolicy = Literal["owner_only", "private_users"]
McpGrantKind = Literal["tool", "resource", "prompt"]


class McpServerPayload(BaseModel):
    """Create/update payload shared by the management API.

    ``config`` deliberately remains a small JSON object because each
    transport has different options.  The service validates the options and
    strips secrets before persisting it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    transport: McpTransport
    config: dict[str, Any] = Field(default_factory=dict)
    secret_values: dict[str, str] = Field(default_factory=dict)
    access_policy: McpAccessPolicy = "owner_only"
    private_users: list[str] = Field(default_factory=list)

    @field_validator("private_users")
    @classmethod
    def normalize_private_users(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in value if str(item).strip()]
        return list(dict.fromkeys(normalized))

    @field_validator("secret_values")
    @classmethod
    def validate_secret_values(cls, value: dict[str, str]) -> dict[str, str]:
        for key, secret in value.items():
            if not key.strip() or len(key) > 120:
                raise ValueError("密钥字段名不能为空且不能超过 120 个字符")
            if not isinstance(secret, str):
                raise ValueError("密钥值必须是字符串")
        return value


class McpServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    transport: McpTransport | None = None
    config: dict[str, Any] | None = None
    secret_values: dict[str, str] = Field(default_factory=dict)
    clear_secrets: list[str] = Field(default_factory=list)
    access_policy: McpAccessPolicy | None = None
    private_users: list[str] | None = None

    @field_validator("clear_secrets")
    @classmethod
    def normalize_clear_secrets(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("private_users")
    @classmethod
    def normalize_update_private_users(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [str(item).strip() for item in value if str(item).strip()]
        return list(dict.fromkeys(normalized))


class AnySearchPresetPayload(BaseModel):
    """Optional one-time API key used when installing the AnySearch preset."""

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, max_length=2_000)

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if "\r" in normalized or "\n" in normalized:
            raise ValueError("AnySearch API Key 不能包含换行")
        return normalized


class McpPreset(BaseModel):
    """Non-sensitive description of an installable MCP preset."""

    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str
    transport: McpTransport
    url: str
    supports_anonymous: bool
    supports_api_key: bool
    risk_note: str
    installed: bool = False


class McpEnabledPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class McpGrantPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_keys: list[str] = Field(default_factory=list)

    @field_validator("allowed_keys")
    @classmethod
    def normalize_allowed_keys(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))


class McpIntentPhrasePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=1, max_length=80)
    phrase: str = Field(min_length=1, max_length=200)
    enabled: bool = True


class McpIntentRulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, min_length=1, max_length=80)
    phrase: str = Field(min_length=1, max_length=200)
    server_id: str = Field(min_length=1, max_length=36)
    tool_key: str | None = Field(default=None, max_length=500)
    enabled: bool = True


class McpGlobalIntentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    custom_rules: list[McpIntentRulePayload] = Field(default_factory=list, max_length=200)


class McpToolIntentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_key: str = Field(min_length=1, max_length=500)
    phrases: list[McpIntentPhrasePayload] = Field(default_factory=list, max_length=100)


class McpServerIntentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_phrases: list[McpIntentPhrasePayload] = Field(default_factory=list, max_length=200)
    tools: list[McpToolIntentPayload] = Field(default_factory=list, max_length=200)


class McpCatalog(BaseModel):
    """Persisted directory metadata; never contains resource bodies."""

    model_config = ConfigDict(extra="forbid")

    tools: list[dict[str, Any]] = Field(default_factory=list)
    resources: list[dict[str, Any]] = Field(default_factory=list)
    resource_templates: list[dict[str, Any]] = Field(default_factory=list)
    prompts: list[dict[str, Any]] = Field(default_factory=list)


def jsonable(value: Any) -> Any:
    """Convert an SDK/Pydantic value to bounded JSON-compatible data."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
