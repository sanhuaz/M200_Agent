from __future__ import annotations

import os
import threading
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import ModelProfile, get_settings


class ModelRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        profiles = get_settings().model_profiles
        self._profiles = {profile.alias: profile for profile in profiles}
        self._default_alias = "default" if "default" in self._profiles else profiles[0].alias

    def replace(self, profiles: list[ModelProfile], default_alias: str | None = None) -> None:
        aliases = [profile.alias for profile in profiles]
        if not profiles:
            raise ValueError("至少需要一个模型配置")
        if len(aliases) != len(set(aliases)):
            raise ValueError("模型 alias 不能重复")
        with self._lock:
            self._profiles = {profile.alias: profile for profile in profiles}
            if default_alias in self._profiles:
                self._default_alias = default_alias
            elif self._default_alias not in self._profiles:
                self._default_alias = "default" if "default" in self._profiles else aliases[0]

    def list(self, in_use_aliases: set[str] | None = None) -> list[dict[str, object]]:
        with self._lock:
            profiles = list(self._profiles.values())
            default_alias = self._default_alias
        in_use_aliases = in_use_aliases or set()
        return [
            {
                **profile.model_dump(exclude={"api_key_env"}),
                "has_api_key": bool(os.getenv(profile.api_key_env)),
                "configured": bool(os.getenv(profile.api_key_env)) and profile.model != "unconfigured",
                "in_use": profile.alias in in_use_aliases,
                "is_default": profile.alias == default_alias,
                "can_delete": (
                    profile.alias != "default"
                    and profile.alias != default_alias
                    and profile.alias not in in_use_aliases
                ),
            }
            for profile in profiles
        ]

    def default_alias(self) -> str:
        with self._lock:
            return self._default_alias

    def profile(self, alias: str) -> ModelProfile:
        with self._lock:
            try:
                return self._profiles[alias]
            except KeyError as error:
                raise ValueError(f"未知模型配置: {alias}") from error

    def chat_model(self, alias: str) -> ChatOpenAI:
        profile = self.profile(alias)
        api_key = os.getenv(profile.api_key_env, "")
        if not api_key or profile.model == "unconfigured":
            raise RuntimeError(f"模型 {alias} 尚未配置可用的 API Key 或模型名")
        return build_chat_model(profile, api_key)


def build_chat_model(profile: ModelProfile, api_key: str, *, max_tokens: int | None = None) -> ChatOpenAI:
    if not api_key or profile.model == "unconfigured":
        raise RuntimeError(f"模型 {profile.alias} 尚未配置可用的 API Key 或模型名")
    kwargs: dict[str, Any] = {
        "model": profile.model,
        "api_key": SecretStr(api_key),
        "base_url": profile.base_url,
        "timeout": profile.timeout_seconds,
        "max_tokens": max_tokens or profile.max_output_tokens,
        "streaming": profile.streaming,
        "reasoning_effort": profile.reasoning_effort,
    }
    if profile.temperature is not None:
        kwargs["temperature"] = profile.temperature
    return ChatOpenAI(**kwargs)


model_registry = ModelRegistry()
