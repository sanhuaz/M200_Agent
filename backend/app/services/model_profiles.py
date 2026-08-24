from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, ModelProfile, get_settings
from app.db.models import AppSetting, Conversation
from app.services.models import build_chat_model, model_registry

MODEL_PROFILES_SETTING_KEY = "model_profiles_v1"
DEFAULT_MODEL_SETTING_KEY = "default_model_alias_v1"
_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MISSING = object()


def _validate_base_url(value: str) -> str:
    base_url = value.strip()
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url 必须是完整的 HTTP 或 HTTPS 地址")
    return base_url.rstrip("/")


def _validate_alias(value: str) -> str:
    alias = value.strip()
    if not _ALIAS_PATTERN.fullmatch(alias):
        raise ValueError("模型 alias 只能包含字母、数字、下划线和短横线，且长度不超过 40")
    return alias


def _validate_profile(profile: ModelProfile) -> ModelProfile:
    if profile.input_soft_limit > profile.context_window:
        raise ValueError("输入软限制不能超过上下文窗口")
    if profile.max_output_tokens > profile.context_window:
        raise ValueError("最大输出 Token 不能超过上下文窗口")
    if not _ENV_NAME_PATTERN.fullmatch(profile.api_key_env):
        raise ValueError("API Key 环境变量名格式无效")
    return profile


def profile_from_data(data: dict[str, object], *, api_key_env: str | None = None) -> ModelProfile:
    values = dict(data)
    values["alias"] = _validate_alias(str(values.get("alias", "")))
    values["model"] = str(values.get("model", "")).strip()
    if not values["model"]:
        raise ValueError("模型名称不能为空")
    values["base_url"] = _validate_base_url(str(values.get("base_url", "")))
    values["api_key_env"] = api_key_env or str(values.get("api_key_env", ""))
    if not values["api_key_env"]:
        raise ValueError("缺少 API Key 环境变量名")
    return _validate_profile(ModelProfile.model_validate(values))


def _new_api_key_env(alias: str, existing: set[str]) -> str:
    stem = re.sub(r"[^A-Za-z0-9]", "_", alias).upper().strip("_") or "MODEL"
    digest = hashlib.sha256(alias.encode("utf-8")).hexdigest()[:8].upper()
    candidate = f"PERSONAL_AGENT_MODEL_{stem}_{digest}_API_KEY"
    if candidate not in existing:
        return candidate
    index = 2
    while f"{candidate}_{index}" in existing:
        index += 1
    return f"{candidate}_{index}"


def _profiles_from_setting(item: AppSetting | None) -> list[ModelProfile]:
    if item is None:
        return list(get_settings().model_profiles)
    try:
        raw = json.loads(item.value)
    except json.JSONDecodeError as error:
        raise RuntimeError("数据库中的模型配置不是有效 JSON") from error
    if not isinstance(raw, list):
        raise RuntimeError("数据库中的模型配置必须是数组")
    return [profile_from_data(dict(value)) for value in raw if isinstance(value, dict)]


def bootstrap_model_profiles(session: Session) -> list[ModelProfile]:
    item = session.get(AppSetting, MODEL_PROFILES_SETTING_KEY)
    profiles = _profiles_from_setting(item)
    changed = False
    if item is None:
        session.add(
            AppSetting(
                key=MODEL_PROFILES_SETTING_KEY,
                value=json.dumps(
                    [profile.model_dump(mode="json") for profile in profiles],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
        changed = True
    aliases = {profile.alias for profile in profiles}
    default_item = session.get(AppSetting, DEFAULT_MODEL_SETTING_KEY)
    default_alias = default_item.value.strip() if default_item else ""
    if default_alias not in aliases:
        default_alias = "default" if "default" in aliases else profiles[0].alias
        if default_item is None:
            session.add(AppSetting(key=DEFAULT_MODEL_SETTING_KEY, value=default_alias))
        else:
            default_item.value = default_alias
        changed = True
    if changed:
        session.commit()
    model_registry.replace(profiles, default_alias=default_alias)
    return profiles


def _env_path() -> Path:
    return PROJECT_ROOT / ".env"


def _env_line(key: str, value: str, newline: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("API Key 不能包含换行")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"{newline}'


def _write_env_value(key: str, value: str) -> None:
    path = _env_path()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)
    assignment = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    replacement = _env_line(key, value, newline)
    found = False
    rewritten: list[str] = []
    for line in lines:
        if assignment.match(line.rstrip("\r\n")):
            rewritten.append(replacement)
            found = True
        else:
            rewritten.append(line)
    if not found:
        if rewritten and not rewritten[-1].endswith(("\n", "\r")):
            rewritten.append(newline)
        rewritten.append(replacement)
    text = "".join(rewritten)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, prefix=".env.", suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    os.environ[key] = value


def _commit_profiles(session: Session, profiles: list[ModelProfile]) -> None:
    item = session.get(AppSetting, MODEL_PROFILES_SETTING_KEY)
    serialized = json.dumps(
        [profile.model_dump(mode="json") for profile in profiles],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if item is None:
        session.add(AppSetting(key=MODEL_PROFILES_SETTING_KEY, value=serialized))
    else:
        item.value = serialized
    default_item = session.get(AppSetting, DEFAULT_MODEL_SETTING_KEY)
    default_alias = default_item.value.strip() if default_item else None
    session.commit()
    model_registry.replace(profiles, default_alias=default_alias)


def _in_use_aliases(session: Session) -> set[str]:
    return set(session.scalars(select(Conversation.model_alias).distinct()))


def list_profiles(session: Session) -> list[dict[str, object]]:
    if session.get(AppSetting, MODEL_PROFILES_SETTING_KEY) is None:
        bootstrap_model_profiles(session)
    return model_registry.list(_in_use_aliases(session))


def set_default_profile(session: Session, alias: str) -> ModelProfile:
    profiles = _profiles_from_setting(session.get(AppSetting, MODEL_PROFILES_SETTING_KEY))
    profile = next((item for item in profiles if item.alias == alias), None)
    if profile is None:
        raise KeyError(alias)
    if profile.model == "unconfigured" or not os.getenv(profile.api_key_env):
        raise ValueError("只能将已配置 API Key 且模型名称有效的配置设为主默认模型")
    setting = session.get(AppSetting, DEFAULT_MODEL_SETTING_KEY)
    if setting is None:
        session.add(AppSetting(key=DEFAULT_MODEL_SETTING_KEY, value=alias))
    else:
        setting.value = alias
    session.execute(update(Conversation).values(model_alias=alias))
    session.commit()
    model_registry.replace(profiles, default_alias=alias)
    return profile


def create_profile(session: Session, data: dict[str, object], api_key: str | None) -> ModelProfile:
    profiles = _profiles_from_setting(session.get(AppSetting, MODEL_PROFILES_SETTING_KEY))
    alias = _validate_alias(str(data.get("alias", "")))
    if any(profile.alias == alias for profile in profiles):
        raise ValueError(f"模型 alias 已存在: {alias}")
    env_names = {profile.api_key_env for profile in profiles}
    profile = profile_from_data(data, api_key_env=_new_api_key_env(alias, env_names))
    updated = [*profiles, profile]
    previous = _env_path().read_bytes() if _env_path().exists() else None
    previous_process_value = os.environ.get(profile.api_key_env, _MISSING)
    try:
        if api_key and api_key.strip():
            _write_env_value(profile.api_key_env, api_key.strip())
        _commit_profiles(session, updated)
    except Exception:
        session.rollback()
        _restore_env(previous, profile.api_key_env, previous_process_value)
        raise
    return profile


def update_profile(
    session: Session,
    alias: str,
    data: dict[str, object],
    api_key: str | None,
    clear_api_key: bool,
) -> ModelProfile:
    profiles = _profiles_from_setting(session.get(AppSetting, MODEL_PROFILES_SETTING_KEY))
    current = next((profile for profile in profiles if profile.alias == alias), None)
    if current is None:
        raise KeyError(alias)
    if clear_api_key and api_key and api_key.strip():
        raise ValueError("不能同时设置 API Key 和清除 API Key")
    values = current.model_dump()
    values.update(data)
    values["alias"] = alias
    change_secret = clear_api_key or bool(api_key and api_key.strip())
    env_names = {profile.api_key_env for profile in profiles if profile.alias != alias}
    env_name = current.api_key_env
    if change_secret and current.api_key_env in env_names:
        env_name = _new_api_key_env(alias, env_names | {current.api_key_env})
    updated_profile = profile_from_data(values, api_key_env=env_name)
    updated = [updated_profile if profile.alias == alias else profile for profile in profiles]
    previous = _env_path().read_bytes() if _env_path().exists() else None
    previous_process_value = os.environ.get(env_name, _MISSING)
    try:
        if clear_api_key:
            _write_env_value(env_name, "")
        elif api_key and api_key.strip():
            _write_env_value(env_name, api_key.strip())
        _commit_profiles(session, updated)
    except Exception:
        session.rollback()
        _restore_env(previous, env_name, previous_process_value)
        raise
    return updated_profile


def delete_profile(session: Session, alias: str) -> None:
    profiles = _profiles_from_setting(session.get(AppSetting, MODEL_PROFILES_SETTING_KEY))
    current = next((profile for profile in profiles if profile.alias == alias), None)
    if current is None:
        raise KeyError(alias)
    default_item = session.get(AppSetting, DEFAULT_MODEL_SETTING_KEY)
    default_alias = default_item.value.strip() if default_item else model_registry.default_alias()
    if alias == "default" or alias == default_alias:
        raise PermissionError("当前主默认模型配置不能删除")
    if alias in _in_use_aliases(session):
        raise RuntimeError("该模型配置仍被会话引用，不能删除")
    previous = _env_path().read_bytes() if _env_path().exists() else None
    previous_process_value = os.environ.get(current.api_key_env, _MISSING)
    try:
        if not any(
            profile.api_key_env == current.api_key_env for profile in profiles if profile.alias != alias
        ):
            _write_env_value(current.api_key_env, "")
        _commit_profiles(session, [profile for profile in profiles if profile.alias != alias])
    except Exception:
        session.rollback()
        _restore_env(previous, current.api_key_env, previous_process_value)
        raise


def test_connection(
    data: dict[str, object], api_key: str | None, existing_alias: str | None = None
) -> dict[str, object]:
    existing = None
    if existing_alias:
        existing = model_registry.profile(existing_alias)
    env_name = existing.api_key_env if existing else None
    api_key_env = env_name or _new_api_key_env(str(data.get("alias", "model")), set())
    profile = profile_from_data(data, api_key_env=api_key_env)
    secret = (api_key or "").strip() or (os.getenv(profile.api_key_env, "") if env_name else "")
    probe = profile.model_copy(update={"streaming": False, "max_output_tokens": 8})
    started = time.perf_counter()
    try:
        build_chat_model(probe, secret, max_tokens=8).invoke([("human", "只回复 OK")])
    except Exception as error:
        message = str(error).replace(secret, "***") if secret else str(error)
        raise RuntimeError(f"连接测试失败：{message}") from error
    return {"ok": True, "message": "连接成功", "elapsed_ms": round((time.perf_counter() - started) * 1000)}


def _restore_env(snapshot: bytes | None, key: str, process_value: object) -> None:
    path = _env_path()
    if snapshot is None:
        if path.exists():
            path.unlink()
    else:
        path.write_bytes(snapshot)
    if process_value is _MISSING:
        os.environ.pop(key, None)
    else:
        os.environ[key] = str(process_value)
