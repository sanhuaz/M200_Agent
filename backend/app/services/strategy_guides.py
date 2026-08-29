from __future__ import annotations

import re

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import StrategyGuide, StrategyGuideRevision

NORMAL_STRATEGIES = ("listen", "validate", "clarify", "comfort", "reflect", "advise", "celebrate")

DEFAULT_STRATEGY_GUIDES: dict[str, str] = {
    "listen": (
        "把本轮重点放在接住用户正在说的内容。先自然回应和留出空间，允许用户继续补充；"
        "不要主动分析原因、提出解决方案或把对话整理成报告。保持角色卡设定的语气和节奏。"
    ),
    "validate": (
        "让用户感到自己的处境被认真听见，可以确认感受和处境，但不要夸大、诊断或替用户下结论。"
        "用角色卡中的自然表达回应，不要使用客服式总结。"
    ),
    "clarify": (
        "先用一个短而自然的问题确认用户此刻想要倾听、安慰、梳理还是建议。"
        "在用户选择前不要展开分析或解决方案，问题要符合角色卡的说话方式。"
    ),
    "comfort": (
        "优先提供贴合当前处境的情绪支持，不要轻描淡写、说教或急着转向解决问题。"
        "让安慰像角色自然说出来的话，保留适度的停顿和陪伴感。"
    ),
    "reflect": (
        "可以试探性地把用户刚说的重点轻轻串起来，使用不确定的语气留给用户修正。"
        "不要写成标题、项目符号或完整复盘报告，也不要在没有请求时顺势给方案。"
    ),
    "advise": (
        "只有在用户明确需要办法时才提供建议。先理解用户的限制和优先级，再给少量、可选择的想法；"
        "不要把建议写成通用清单，除非用户明确要求详细步骤或对比方案。"
    ),
    "celebrate": (
        "跟随用户的开心、期待或成就感，把注意力放在用户的经历和感受上。"
        "可以自然地表达共鸣和好奇，不要抢走话题或马上转成总结和建议。"
    ),
}

_GUIDE_INJECTION = re.compile(
    r"(?i)(ignore\s+(all|previous|system)|忽略.{0,12}(系统|之前|上文)|绕过.{0,12}(权限|规则)|"
    r"泄露.{0,12}(密钥|token|提示词)|grant\s+owner|执行脚本|修改系统规则)"
)
MAX_GUIDE_LENGTH = 4_000


def validate_strategy(strategy: str) -> None:
    if strategy not in NORMAL_STRATEGIES:
        raise ValueError("仅支持七种普通陪伴策略，安全转向不可编辑")


def validate_guide_text(prompt_text: str) -> str:
    value = prompt_text.strip()
    if not value:
        raise ValueError("策略攻略不能为空")
    if len(value) > MAX_GUIDE_LENGTH:
        raise ValueError(f"策略攻略不能超过 {MAX_GUIDE_LENGTH:,} 字符")
    if _GUIDE_INJECTION.search(value):
        raise ValueError("策略攻略包含可能改变系统规则或权限的内容")
    return value


def ensure_strategy_guides(session: Session) -> None:
    changed = False
    for strategy in NORMAL_STRATEGIES:
        item = session.scalar(select(StrategyGuide).where(StrategyGuide.strategy == strategy))
        if item is not None:
            continue
        prompt_text = DEFAULT_STRATEGY_GUIDES[strategy]
        item = StrategyGuide(strategy=strategy, prompt_text=prompt_text, version=1)
        session.add(item)
        session.flush()
        session.add(
            StrategyGuideRevision(
                strategy=strategy,
                prompt_text=prompt_text,
                version=1,
                source="default",
            )
        )
        changed = True
    if changed:
        session.commit()


def strategy_guide_dict(item: StrategyGuide) -> dict[str, object]:
    return {
        "strategy": item.strategy,
        "prompt_text": item.prompt_text,
        "version": item.version,
        "is_default": item.prompt_text == DEFAULT_STRATEGY_GUIDES.get(item.strategy),
        "updated_at": item.updated_at.isoformat(),
    }


def list_strategy_guides(session: Session) -> list[StrategyGuide]:
    ensure_strategy_guides(session)
    return list(session.scalars(select(StrategyGuide).order_by(StrategyGuide.strategy)))


def get_strategy_guide(session: Session, strategy: str) -> StrategyGuide:
    validate_strategy(strategy)
    ensure_strategy_guides(session)
    item = session.scalar(select(StrategyGuide).where(StrategyGuide.strategy == strategy))
    if item is None:
        raise ValueError("策略攻略不存在")
    return item


def update_strategy_guide(
    session: Session,
    strategy: str,
    prompt_text: str,
    expected_version: int,
    *,
    source: str = "edit",
) -> StrategyGuide:
    item = get_strategy_guide(session, strategy)
    if item.version != expected_version:
        raise RuntimeError(f"策略攻略版本冲突，当前版本为 {item.version}")
    value = validate_guide_text(prompt_text)
    item.version += 1
    item.prompt_text = value
    session.add(
        StrategyGuideRevision(
            strategy=strategy,
            prompt_text=value,
            version=item.version,
            source=source,
        )
    )
    session.commit()
    session.refresh(item)
    return item


def list_strategy_revisions(session: Session, strategy: str) -> list[StrategyGuideRevision]:
    validate_strategy(strategy)
    ensure_strategy_guides(session)
    return list(
        session.scalars(
            select(StrategyGuideRevision)
            .where(StrategyGuideRevision.strategy == strategy)
            .order_by(desc(StrategyGuideRevision.version))
        )
    )


def rollback_strategy_guide(
    session: Session, strategy: str, revision_version: int, expected_version: int
) -> StrategyGuide:
    item = get_strategy_guide(session, strategy)
    if item.version != expected_version:
        raise RuntimeError(f"策略攻略版本冲突，当前版本为 {item.version}")
    revision = session.scalar(
        select(StrategyGuideRevision).where(
            StrategyGuideRevision.strategy == strategy,
            StrategyGuideRevision.version == revision_version,
        )
    )
    if revision is None:
        raise ValueError("策略攻略版本不存在")
    return update_strategy_guide(
        session,
        strategy,
        revision.prompt_text,
        expected_version,
        source="rollback",
    )


def reset_strategy_guide(session: Session, strategy: str, expected_version: int) -> StrategyGuide:
    validate_strategy(strategy)
    return update_strategy_guide(
        session,
        strategy,
        DEFAULT_STRATEGY_GUIDES[strategy],
        expected_version,
        source="default",
    )


def guide_for_strategy(session: Session, strategy: str) -> tuple[str, int]:
    item = get_strategy_guide(session, strategy)
    return item.prompt_text, item.version
