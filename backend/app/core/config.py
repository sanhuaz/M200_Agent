from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env", override=False)


class ModelProfile(BaseModel):
    alias: str
    model: str
    base_url: str
    api_key_env: str
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    streaming: bool = True
    temperature: float | None = Field(default=None, ge=0, le=2)
    context_window: int = 1_000_000
    input_soft_limit: int = 131_072
    max_output_tokens: int = 16_384
    timeout_seconds: float = 120.0
    supports_vision: bool = False

    @field_validator("context_window", "input_soft_limit", "max_output_tokens")
    @classmethod
    def positive_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("模型 Token 配置必须为正数")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("模型超时时间必须为正数")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    frontend_origin: str = "http://127.0.0.1:5176"
    database_path: Path = PROJECT_ROOT / "data" / "personal_agent.db"
    chroma_path: Path = PROJECT_ROOT / "data" / "chroma"
    upload_path: Path = PROJECT_ROOT / "data" / "uploads"
    download_path: Path = PROJECT_ROOT / "data" / "downloads"
    chat_image_path: Path = PROJECT_ROOT / "data" / "chat-images"
    tools_path: Path = PROJECT_ROOT / "tools"
    skills_path: Path = PROJECT_ROOT / "skills"
    workspace_path: Path = PROJECT_ROOT / "workspace"
    persona_path: Path = PROJECT_ROOT / "persona"
    personal_agent_timezone: str = "Asia/Shanghai"

    model_profiles_json: str = (
        '[{"alias":"default","model":"unconfigured","base_url":"https://api.example.com/v1",'
        '"api_key_env":"PERSONAL_AGENT_LLM_API_KEY","context_window":1000000,'
        '"input_soft_limit":131072,"max_output_tokens":16384,'
        '"timeout_seconds":120,"supports_vision":false}]'
    )
    default_embedding_profile: str = "local-bge"
    local_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    online_embedding_model: str = "text-embedding-3-small"
    online_embedding_base_url: str = "https://api.openai.com/v1"
    online_embedding_api_key_env: str = "PERSONAL_AGENT_EMBEDDING_API_KEY"

    rerank_enabled: bool = False
    rerank_url: str = "https://api.siliconflow.cn/v1/rerank"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_api_key: str = ""

    onebot_token: str = "change-me"
    owner_qq_ids: list[str] = Field(default_factory=list)
    qq_upload_limit_mb: int = 100
    group_command_prefix: str = "/ai"

    # MCP 连接默认值；单个 Server 的失败由 Manager 隔离，不阻断应用启动。
    mcp_connect_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    mcp_read_timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    mcp_catalog_item_limit: int = Field(default=200, gt=0, le=2_000)

    @field_validator("owner_qq_ids", mode="before")
    @classmethod
    def parse_owner_ids(cls, value: object) -> object:
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return [str(item) for item in json.loads(stripped)]
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("personal_agent_timezone")
    @classmethod
    def validate_personal_agent_timezone(cls, value: str) -> str:
        timezone_name = value.strip()
        if not timezone_name:
            raise ValueError("PERSONAL_AGENT_TIMEZONE 无效：不能为空")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"PERSONAL_AGENT_TIMEZONE 无效：{timezone_name}") from error
        return timezone_name

    @field_validator(
        "database_path",
        "chroma_path",
        "upload_path",
        "download_path",
        "chat_image_path",
        "tools_path",
        "skills_path",
        "workspace_path",
        "persona_path",
        mode="after",
    )
    @classmethod
    def resolve_project_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @property
    def model_profiles(self) -> list[ModelProfile]:
        profiles = [ModelProfile.model_validate(item) for item in json.loads(self.model_profiles_json)]
        if not profiles:
            raise ValueError("MODEL_PROFILES_JSON 至少需要一个模型配置")
        aliases = [profile.alias for profile in profiles]
        if len(aliases) != len(set(aliases)):
            raise ValueError("模型 alias 不能重复")
        return profiles

    def ensure_directories(self) -> None:
        for path in (
            self.database_path.parent,
            self.chroma_path,
            self.upload_path,
            self.download_path,
            self.chat_image_path,
            self.tools_path,
            self.skills_path,
            self.workspace_path,
            self.persona_path,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
