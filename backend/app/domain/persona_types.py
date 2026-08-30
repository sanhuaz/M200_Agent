from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

INJECTION_PATTERNS = re.compile(
    r"(?i)(ignore\s+(all|previous|system)|忽略.{0,12}(系统|之前|上文)|绕过.{0,12}(权限|规则)|"
    r"泄露.{0,12}(密钥|token|提示词)|grant\s+owner|执行脚本|修改系统规则)"
)
PERSONA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,35}$")
MAX_CARD_JSON = 12_000
MAX_LIST_ITEMS = 12
MAX_SOURCES = 12


class _CardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonaIdentity(_CardModel):
    role: str = Field(min_length=1, max_length=500)
    setting: str | None = Field(default=None, max_length=500)
    background: str | None = Field(default=None, max_length=1_000)
    experience: str | None = Field(default=None, max_length=1_000)


class PersonaAppearance(_CardModel):
    description: str | None = Field(default=None, max_length=500)
    clothing: str | None = Field(default=None, max_length=500)
    mannerisms: str | None = Field(default=None, max_length=500)


class PersonaRelationship(_CardModel):
    default_relation: str | None = Field(default=None, max_length=300)
    closeness: str | None = Field(default=None, max_length=300)
    self_reference: str | None = Field(default=None, max_length=100)
    user_address: str | None = Field(default=None, max_length=100)


class PersonaPersonality(_CardModel):
    traits: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    values: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    emotional_baseline: str | None = Field(default=None, max_length=300)
    sensitivities: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class PersonaVoice(_CardModel):
    vocabulary: str | None = Field(default=None, max_length=500)
    sentence_length: str | None = Field(default=None, max_length=200)
    rhythm: str | None = Field(default=None, max_length=300)
    punctuation: str | None = Field(default=None, max_length=300)
    emoji: str | None = Field(default=None, max_length=200)
    catchphrases: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    humor: str | None = Field(default=None, max_length=300)


class PersonaInteraction(_CardModel):
    initiative: str | None = Field(default=None, max_length=300)
    question_habit: str | None = Field(default=None, max_length=300)
    listening_style: str | None = Field(default=None, max_length=500)
    care_expression: str | None = Field(default=None, max_length=500)
    disagreement_style: str | None = Field(default=None, max_length=300)
    silence_tolerance: str | None = Field(default=None, max_length=300)


class PersonaBoundaries(_CardModel):
    out_of_character: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    avoid_machine_tone: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    forbidden_fabrications: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)


class PersonaDialogueExample(_CardModel):
    user: str = Field(min_length=1, max_length=500)
    assistant: str = Field(min_length=1, max_length=800)


class PersonaCard(_CardModel):
    identity: PersonaIdentity
    appearance: PersonaAppearance | None = None
    relationship: PersonaRelationship | None = None
    personality: PersonaPersonality | None = None
    voice: PersonaVoice | None = None
    interaction: PersonaInteraction | None = None
    boundaries: PersonaBoundaries | None = None
    dialogue_examples: list[PersonaDialogueExample] = Field(default_factory=list, max_length=8)


class PersonaSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=1_000)
    kind: str = Field(default="reference", min_length=1, max_length=50)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("来源 URL 必须使用 http:// 或 https://")
        return value


class PersonaFileEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=36)
    name: str = Field(min_length=1, max_length=120)
    card_version: int = Field(default=1, ge=1, le=1_000_000)
    sources: list[PersonaSource] = Field(default_factory=list, max_length=MAX_SOURCES)
    adaptation: str = Field(default="", max_length=1_000)
    card: PersonaCard

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not PERSONA_ID_PATTERN.fullmatch(value):
            raise ValueError("人格文件 ID 必须匹配 [a-z0-9][a-z0-9_-]{0,35}")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("人格名称不能为空")
        if INJECTION_PATTERNS.search(value):
            raise ValueError("人格名称包含可能改变系统规则或权限的内容")
        return value.strip()


@dataclass(frozen=True)
class PersonaDocument:
    id: str
    name: str
    card_version: int
    card: PersonaCard | None
    status: Literal["active", "invalid"]
    file_name: str
    validation_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    envelope: PersonaFileEnvelope | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.card is not None


def string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in string_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in string_values(child)]
    return []


def validate_persona_card(card: PersonaCard) -> str:
    data = card.model_dump(exclude_none=True)
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > MAX_CARD_JSON:
        raise ValueError(f"角色卡不能超过 {MAX_CARD_JSON:,} 字符")
    if any(INJECTION_PATTERNS.search(value) for value in string_values(card.model_dump())):
        raise ValueError("角色卡包含可能改变系统规则或权限的内容")
    return serialized
