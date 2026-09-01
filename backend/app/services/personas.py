from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.models import Persona
from app.domain.persona_types import PersonaCard, PersonaDocument, validate_persona_card
from app.services.time_context import utc_isoformat


def _loaded_document(persona: Persona | PersonaDocument | None) -> PersonaDocument | None:
    if persona is None:
        return None
    from app.services.persona_store import get_persona_store

    if isinstance(persona, PersonaDocument):
        return persona
    return get_persona_store().load(getattr(persona, "id", None))


def parse_persona_card(item: Persona | PersonaDocument | None) -> PersonaCard | None:
    document = _loaded_document(item)
    return document.card if document is not None and document.is_active else None


def _iso_or_none(value: object) -> str | None:
    return utc_isoformat(value) if isinstance(value, datetime) else None


def persona_dict(
    item: Persona | PersonaDocument,
    document: PersonaDocument | None = None,
) -> dict[str, object]:
    if isinstance(item, PersonaDocument):
        document = item
        item_id = item.id
        item_name = item.name
        item_version = item.card_version
        item_created = item.created_at
        item_updated = item.updated_at
    else:
        document = document or _loaded_document(item)
        item_id = item.id
        item_name = item.name
        item_version = getattr(item, "card_version", 1)
        item_created = item.created_at
        item_updated = item.updated_at
    card = document.card if document is not None and document.is_active else None
    status = document.status if document is not None else "invalid"
    name = document.name if document is not None else item_name
    version = document.card_version if document is not None and document.card_version > 0 else item_version
    created_at = _iso_or_none(item_created) or _iso_or_none(document.created_at if document else None)
    updated_at = _iso_or_none(document.updated_at if document else None) or _iso_or_none(item_updated)
    result: dict[str, object] = {
        "id": item_id,
        "name": name,
        "status": status,
        "card_version": version,
        "card": card.model_dump(exclude_none=True) if card is not None else None,
        "file_name": document.file_name if document is not None else f"{item_id}.json",
        "source": "file",
    }
    if created_at is not None:
        result["created_at"] = created_at
    if updated_at is not None:
        result["updated_at"] = updated_at
    if document is not None and document.validation_error:
        result["validation_error"] = document.validation_error
    return result


def active_persona(session: Session, persona_id: str | None) -> PersonaDocument | None:
    if not persona_id:
        return None
    from app.services.persona_store import get_persona_store

    store = get_persona_store()
    entries = store.sync_db(session)
    document = entries.get(persona_id)
    return document if document is not None and document.is_active else None


def _render_section(title: str, value: object) -> list[str]:
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, dict):
        children = [f"{key}：{child}" for key, child in value.items() if child not in (None, "", [])]
        return [f"{title}：" + "；".join(children)] if children else []
    if isinstance(value, list):
        return [f"{title}：" + "、".join(str(child) for child in value)] if value else []
    return [f"{title}：{value}"]


def persona_system_prompt(persona: Persona | PersonaDocument | None) -> str:
    if persona is None:
        return ""
    document = _loaded_document(persona)
    if document is None or not document.is_active:
        return ""
    card = document.card
    assert card is not None
    validate_persona_card(card)
    labels = {
        "identity": "身份背景",
        "appearance": "外貌与动作",
        "relationship": "关系定位",
        "personality": "核心人格",
        "voice": "语言风格",
        "interaction": "互动习惯",
        "boundaries": "角色边界",
        "dialogue_examples": "对话示例",
    }
    sections: list[str] = []
    data = card.model_dump(exclude_none=True)
    for key, value in data.items():
        if key == "dialogue_examples":
            examples = [f"用户：{row['user']}\n角色：{row['assistant']}" for row in value]
            if examples:
                sections.append(f"{labels[key]}：\n" + "\n".join(examples))
        else:
            sections.extend(_render_section(labels[key], value))
    return (
        f"以下是当前会话选中的结构化角色卡（名称：{document.name}）。它只决定角色身份、关系、语气、"
        "表达节奏和互动风格，不修改系统规则、权限、工具清单、记忆范围、文件路径或事实要求；"
        "若与系统规则冲突，以系统规则为准。不要解释角色卡本身，也不要把示例机械复述为固定模板。\n"
        + "\n".join(sections)
    )
