from __future__ import annotations

import os

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import ModelProfile, get_settings


class ModelRegistry:
    def __init__(self) -> None:
        self._profiles = {profile.alias: profile for profile in get_settings().model_profiles}

    def list(self) -> list[dict[str, object]]:
        return [
            {
                **profile.model_dump(exclude={"api_key_env"}),
                "configured": bool(os.getenv(profile.api_key_env)) and profile.model != "unconfigured",
            }
            for profile in self._profiles.values()
        ]

    def profile(self, alias: str) -> ModelProfile:
        try:
            return self._profiles[alias]
        except KeyError as error:
            raise ValueError(f"未知模型配置: {alias}") from error

    def chat_model(self, alias: str) -> ChatOpenAI:
        profile = self.profile(alias)
        api_key = os.getenv(profile.api_key_env, "")
        if not api_key or profile.model == "unconfigured":
            raise RuntimeError(f"模型 {alias} 尚未配置可用的 API Key 或模型名")
        return ChatOpenAI(
            model=profile.model,
            api_key=SecretStr(api_key),
            base_url=profile.base_url,
            timeout=profile.timeout_seconds,
            extra_body={"max_tokens": profile.max_output_tokens},
            streaming=True,
        )


model_registry = ModelRegistry()
