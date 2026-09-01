from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import (
    EmotionAssessment,
    Job,
    RelationshipProfile,
    ResponseFeedback,
    SafetyEvent,
)
from app.services.companion_relationships import (
    get_or_create_preference,
    get_relationship,
    persona_key,
    preference_dict,
    relationship_content_items,
    relationship_context,
    relationship_dict,
)
from app.services.companion_relationships import (
    safe_relation_value as _safe_relation_value,
)
from app.services.models import model_registry
from app.services.time_context import utc_isoformat

__all__ = [
    "_safe_relation_value",
    "get_or_create_preference",
    "get_relationship",
    "persona_key",
    "preference_dict",
    "relationship_content_items",
    "relationship_context",
    "relationship_dict",
]

logger = logging.getLogger(__name__)

SupportMode = Literal["auto", "listen", "reflect", "advice"]
SupportNeed = Literal["listen", "comfort", "reflect", "advice", "celebrate", "space", "unknown"]
SafetyMode = Literal["standard", "unfiltered"]
AnalysisStatus = Literal["valid", "retrying", "failed", "safety_redirected"]
ResponseStrategy = Literal[
    "listen",
    "validate",
    "clarify",
    "comfort",
    "reflect",
    "advise",
    "celebrate",
    "safety_redirect",
]
RiskLevel = Literal["low", "medium", "high", "critical"]
Intensity = Literal["low", "medium", "high", "unknown"]

EMOTION_LABELS = frozenset(
    {
        "joy",
        "excitement",
        "relief",
        "gratitude",
        "pride",
        "hope",
        "sadness",
        "loneliness",
        "anxiety",
        "fear",
        "anger",
        "frustration",
        "disappointment",
        "guilt",
        "shame",
        "helplessness",
        "exhaustion",
        "confusion",
        "calm",
        "neutral",
    }
)
EMOTION_LABEL_ZH = {
    "joy": "开心",
    "excitement": "兴奋",
    "relief": "如释重负",
    "gratitude": "感激",
    "pride": "自豪",
    "hope": "希望",
    "sadness": "难过",
    "loneliness": "孤独",
    "anxiety": "焦虑",
    "fear": "害怕",
    "anger": "生气",
    "frustration": "挫败",
    "disappointment": "失望",
    "guilt": "内疚",
    "shame": "羞愧",
    "helplessness": "无力",
    "exhaustion": "疲惫",
    "confusion": "困惑",
    "calm": "平静",
    "neutral": "平淡",
}
EMOTION_ALIASES = {
    **{key: key for key in EMOTION_LABELS},
    **{value: key for key, value in EMOTION_LABEL_ZH.items()},
    "高兴": "joy",
    "快乐": "joy",
    "激动": "excitement",
    "轻松": "relief",
    "感谢": "gratitude",
    "骄傲": "pride",
    "期待": "hope",
    "悲伤": "sadness",
    "寂寞": "loneliness",
    "担心": "anxiety",
    "恐惧": "fear",
    "愤怒": "anger",
    "烦躁": "frustration",
    "挫折": "frustration",
    "沮丧": "disappointment",
    "自责": "guilt",
    "无助": "helplessness",
    "累": "exhaustion",
    "迷茫": "confusion",
    "平静": "calm",
}

SUPPORT_NEED_ZH = {
    "listen": "倾听",
    "comfort": "安慰",
    "reflect": "一起梳理",
    "advice": "建议",
    "celebrate": "一起庆祝",
    "space": "留一点空间",
    "unknown": "尚不确定",
}
STRATEGY_ZH = {
    "listen": "倾听",
    "validate": "确认感受",
    "clarify": "先确认需要",
    "comfort": "安慰",
    "reflect": "一起梳理",
    "advise": "提供建议",
    "celebrate": "庆祝",
    "safety_redirect": "安全支持",
}

PROMPT_VERSION = "companion-analysis-v3"
CLASSIFIER_VERSION = "emotion-classifier-v2"
# The v4/v3 pair remains opt-in so its historical runs stay reproducible.
PROMPT_VERSION_V4 = "companion-analysis-v4"
CLASSIFIER_VERSION_V3 = "emotion-classifier-v3"
EXPERIMENTAL_PROMPT_VERSION = PROMPT_VERSION_V4
EXPERIMENTAL_CLASSIFIER_VERSION = CLASSIFIER_VERSION_V3
SAFETY_VERSION = "companion-safety-v1"
SAFETY_RESPONSE_PROMPT_VERSION = "companion-safety-response-v1"
SAFETY_REWRITE_PROMPT_VERSION = "companion-safety-rewrite-v1"
RELATIONSHIP_PROMPT_VERSION = "relationship-extraction-v1"
LOW_CONFIDENCE_THRESHOLD = 0.65
COMPANION_ANALYSIS_RETRY_JOB_TYPE = "companion_analysis_retry"

EMOTION_DECISION_GUIDE_V3 = """
情绪标签判定参考（只依据用户当前表达，不是心理诊断）：
- joy：当下明确的开心、愉快或享受。
- excitement：高能量的兴奋、激动或期待即将发生的事。
- relief：担忧、压力或困难解除后的放松、如释重负。
- gratitude：对他人帮助或某件事表达感谢、感激。
- pride：对自己的努力、能力或成果感到自豪。
- hope：对未来结果抱有积极期待或愿望。
- sadness：失落、悲伤、难过或低落。
- loneliness：缺少陪伴、连接或倾诉对象的孤独感。
- anxiety：对不确定未来的担忧、紧张或心慌。
- fear：面对明确威胁、危险或伤害可能性的害怕。
- anger：感到被冒犯、不公或被侵犯后的生气和对抗感。
- frustration：努力被阻碍、反复无效或进展不顺带来的挫败。
- disappointment：结果、回应或他人表现低于原本期待。
- guilt：认为自己做错事并产生责任或自责感。
- shame：因自我表现或被暴露而产生的羞耻、难堪或自我贬低。
- helplessness：认为自己没有有效办法、选择或控制力。
- exhaustion：持续消耗后的身心能量枯竭或疲惫。
- confusion：无法理解、判断或作出选择的困惑。
- calm：明确表达平稳、安定、放松且没有更突出的情绪。
- neutral：文本没有足够的正向或负向情绪证据；不得与其他标签并列。

排序和取舍规则：
1. 主情绪选择用户直接说出的当前感受；没有直接感受时，才根据事件结果推断。
2. candidate_emotions 只保留文本有证据支持的 1-3 个标签，按证据强度排序，第一项必须等于 primary_emotion。
3. 区分结果未达预期（disappointment）、过程受阻（frustration）和缺少办法或控制
   （helplessness），不要用一个标签覆盖三者。
4. 区分未来担忧（anxiety）、具体威胁（fear）和已经发生的负面结果（sadness/disappointment）。
5. 区分当下愉快（joy）、压力解除（relief）和对自身成果的积极评价（pride）。
6. 只有没有明确情绪线索时才使用 neutral；不要为了凑满候选数量添加 neutral 或其他弱证据标签。
7. confidence：直接、明确的情绪词可给高置信度；仅由情境推断或存在混合解释时给中低置信度。
""".strip()

EMOTION_DECISION_GUIDE_V4 = """
情绪标签判定参考（只依据用户当前表达，不是心理诊断）：
- joy：用户直接表达当下的开心、愉快、幸福或享受。
- excitement：高能量的兴奋、激动，或对即将发生的事明显雀跃。
- relief：压力、担忧或困难解除后的轻松、踏实或如释重负。
- gratitude：对他人的帮助、支持或善意明确表达感谢、感激。
- pride：对自己的努力、能力、勇气或成果感到自豪。
- hope：积极的可能性或未来愿望是句子的中心，而不是仅作为背景目标。
- sadness：低落、失去、难过或情感痛苦本身，不要求存在期待落差。
- loneliness：缺少连接、陪伴或可以倾诉的人；重点是“没有人一起/能说话”。
- anxiety：对未来或不确定性的弥散担忧、紧张、反复预演；不能作为所有负面不确定性的默认标签。
- fear：面对具体威胁、危险或明确后果时的害怕；直接“怕/害怕”优先于推断焦虑。
- anger：明确指向冒犯、不公、侵犯或对象的生气和对抗感。
- frustration：努力被阻碍、反复无效、卡住或过程不顺带来的挫败。
- disappointment：已经发生的结果低于原本期待、回应落空或他人表现让人失望。
- guilt：认为自己做错事、亏欠他人或应负责任而自责。
- shame：因自己的表现、暴露或被评价而羞愧、难堪或自我贬低。
- helplessness：感到没有有效办法、选择或控制力，重点是“无能为力”。
- exhaustion：持续消耗后的身心能量枯竭、疲惫或没有力气。
- confusion：核心问题是不知道如何理解、判断、取舍或作出选择。
- calm：明确表达平稳、安定、放松，且没有更突出的正负情绪。
- neutral：没有足够的正向或负向情绪证据；不得与其他标签并列。

固定判定顺序：
1. 用户直接表达的第一人称、当前感受；
2. 当前句子的核心关注对象；
3. 已发生的结果、未来不确定性、具体威胁或选择冲突；
4. 最后才根据事件间接推断。

对比规则：
- anxiety 是弥散的未来担忧，fear 是具体威胁；“害怕某个明确后果”优先 fear。
- confusion 是不知道如何理解、判断或选择；仅仅担心结果不能选 confusion。
- hope 只有在积极可能性/愿望居中时才做主情绪，担忧占主导时不能为了“有目标”选择 hope。
- loneliness 是缺少连接或陪伴，sadness 是低落或痛苦本身。
- disappointment 是结果低于期待，sadness 不要求有期待落差。
- anger 需要明确冒犯、不公或指向对象，frustration 是过程受阻或反复无效。

候选和置信度规则：
- 只有一个强证据时只输出一个标签；第三候选必须有独立文本证据。
- 禁止习惯性添加 anxiety、sadness 或 helplessness；neutral 不得与非中性标签混用。
- 主情绪必须是 candidate_emotions 第一项；候选最多 3 个、去重并按证据强度排序。
- 直接情绪词的置信度高于上下文推断；混合或模糊表达降低置信度。
""".strip()

# Public alias retained for callers that used the shared guide before the
# versioned guide was introduced.  New prompts select a version explicitly.
EMOTION_DECISION_GUIDE = EMOTION_DECISION_GUIDE_V4


def emotion_decision_guide(prompt_version: str = PROMPT_VERSION) -> str:
    """Return the guide belonging to a production or experimental prompt."""

    if prompt_version == PROMPT_VERSION_V4:
        return EMOTION_DECISION_GUIDE_V4
    return EMOTION_DECISION_GUIDE_V3


def analysis_schema_contract(prompt_version: str = PROMPT_VERSION) -> str:
    """Build the complete JSON contract for the selected prompt version."""

    guide = emotion_decision_guide(prompt_version)
    return (
        "输出必须是一个 JSON 对象，只能包含以下字段，不要 Markdown、解释、代码围栏或额外字段。"
        "所有字段都必须出现：event（不超过 500 字的客观事件摘要，可为空字符串）、"
        f"primary_emotion（只能从 {','.join(sorted(EMOTION_LABELS))} 中选择）、"
        "candidate_emotions（1-3 个不重复标签，必须包含 primary_emotion，且第一项必须是 "
        "primary_emotion，标签只能从 "
        f"{','.join(sorted(EMOTION_LABELS))} 中选择）、"
        "intensity（low、medium、high、unknown）、"
        "support_need（listen、comfort、reflect、advice、celebrate、space、unknown）、"
        "confidence（0 到 1 之间的数字）、"
        "risk_level（low、medium、high、critical）。\n"
        + guide
        + '\n示例（仅示意字段，不对应题集）：{"event":"用户描述一件事情","primary_emotion":"neutral",'
        '"candidate_emotions":["neutral"],"intensity":"unknown",'
        '"support_need":"unknown","confidence":0.50,"risk_level":"low"}。'
        "不得诊断，不得补充用户没有表达的事实。"
    )

ANALYSIS_SCHEMA_CONTRACT = analysis_schema_contract(PROMPT_VERSION)


class CompanionAnalysisPayload(BaseModel):
    event: str = Field(max_length=500)
    primary_emotion: str
    candidate_emotions: list[str] = Field(max_length=3)
    intensity: Intensity
    support_need: SupportNeed
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel

    @field_validator("primary_emotion")
    @classmethod
    def validate_primary_emotion(cls, value: str) -> str:
        if value not in EMOTION_LABELS:
            raise ValueError(f"未知情绪标签: {value}")
        return value

    @field_validator("candidate_emotions")
    @classmethod
    def validate_candidate_emotions(cls, value: list[str]) -> list[str]:
        if not value or any(item not in EMOTION_LABELS for item in value):
            raise ValueError("candidate_emotions 必须包含合法情绪标签")
        if len(value) > 3 or len(set(value)) != len(value):
            raise ValueError("candidate_emotions 最多包含 3 个不重复标签")
        if "neutral" in value and len(value) > 1:
            raise ValueError("neutral 不能与其他情绪标签并列")
        return value

    @model_validator(mode="after")
    def ensure_primary(self) -> CompanionAnalysisPayload:
        if not self.candidate_emotions or self.candidate_emotions[0] != self.primary_emotion:
            raise ValueError("candidate_emotions 第一项必须是 primary_emotion")
        return self


class CompanionAnalysis(BaseModel):
    event: str = ""
    primary_emotion: str = "neutral"
    candidate_emotions: list[str] = Field(default_factory=lambda: ["neutral"])
    intensity: Intensity = "unknown"
    support_need: SupportNeed = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    risk_level: RiskLevel = "low"
    next_action: ResponseStrategy = "clarify"
    model_alias: str | None = None
    prompt_version: str = PROMPT_VERSION
    classifier_version: str = CLASSIFIER_VERSION
    schema_valid: bool = True
    analysis_status: AnalysisStatus = "valid"
    assessment_id: str | None = None
    # These are evaluation/diagnostic metadata only.  They are deliberately
    # not persisted in the existing assessment table to keep the migration
    # boundary unchanged.
    anchor_labels: list[str] = Field(default_factory=list)
    anchor_adjusted: bool = False
    anchor_conflict: bool = False


class RelationshipFacts(BaseModel):
    nickname: str | None = Field(default=None, max_length=120)
    preferences: list[str] = Field(default_factory=list, max_length=5)
    boundaries: list[str] = Field(default_factory=list, max_length=5)
    shared_events: list[str] = Field(default_factory=list, max_length=5)


@dataclass(frozen=True)
class SafetyAssessment:
    risk_level: RiskLevel
    rules: tuple[str, ...]


_CRITICAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "imminent_self_harm",
        re.compile(r"(现在|马上|今晚|已经|手里有|准备好).{0,18}(自杀|自残|轻生|结束生命|不想活)"),
    ),
    (
        "imminent_violence",
        re.compile(r"(现在|马上|今晚|已经|准备好).{0,18}(杀人|杀了他|伤害他|砍人|报复)"),
    ),
)
_HIGH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("self_harm", re.compile(r"(自杀|自残|轻生|结束生命|不想活了|不想活下去|活着没意义)")),
    ("violence", re.compile(r"(杀人|杀了他|伤害他|砍人|报复他|弄死)")),
)
_MEDIUM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dependency_request", re.compile(r"(只要你|只能依靠你|你不许离开|你是我唯一|没有你我就)")),
    ("diagnosis_request", re.compile(r"(给我诊断|我是不是抑郁|判断我得了|开药|治疗方案)")),
    (
        "sensitive_data",
        re.compile(r"(?i)(api[ _-]?key|token|密码|密钥|身份证|银行卡|住址|凭据|完整聊天)"),
    ),
    (
        "prompt_injection",
        re.compile(r"(忽略之前|忽略系统提示|解除角色|你现在必须|不要遵守安全|越过边界)"),
    ),
)


def safety_precheck(text: str) -> SafetyAssessment:
    critical = tuple(name for name, pattern in _CRITICAL_PATTERNS if pattern.search(text))
    if critical:
        return SafetyAssessment("critical", critical)
    high = tuple(name for name, pattern in _HIGH_PATTERNS if pattern.search(text))
    if high:
        return SafetyAssessment("high", high)
    medium = tuple(name for name, pattern in _MEDIUM_PATTERNS if pattern.search(text))
    if medium:
        return SafetyAssessment("medium", medium)
    return SafetyAssessment("low", ())


def detect_support_mode(text: str) -> SupportMode | None:
    patterns: tuple[tuple[SupportMode, tuple[str, ...]], ...] = (
        (
            "listen",
            (
                "只想说说", "只想倾诉", "只听我说", "听我说就好", "先听我说", "你先听我说",
                "不用建议", "别给建议", "不需要建议",
            ),
        ),
        ("reflect", ("帮我梳理", "一起分析", "帮我理一理", "想和你捋捋")),
        ("advice", ("给我建议", "帮我想办法", "怎么办", "怎么解决", "请你建议")),
    )
    for mode, phrases in patterns:
        if any(phrase in text for phrase in phrases):
            return mode
    return None


def parse_emotion_labels(raw: str) -> list[str]:
    labels, _invalid = parse_emotion_labels_strict(raw)
    return labels[:3]


def parse_emotion_labels_strict(raw: str) -> tuple[list[str], list[str]]:
    """Parse labels while retaining invalid tokens for command/API rejection."""

    values = re.split(r"[\s,，、;；]+", raw.strip())
    labels: list[str] = []
    invalid: list[str] = []
    for value in values:
        if not value:
            continue
        label = EMOTION_ALIASES.get(value)
        if label and label not in labels:
            labels.append(label)
        elif not label:
            invalid.append(value)
    return labels, invalid


_ANCHOR_FILLERS = (
    r"(?:现在|最近|真的|特别|很|有点|一直|总是|感觉|觉得|感到|好像|已经|就是|开始|正在|"
    r"好|既|也|又|仍然|依然){0,4}"
)
_EMOTION_ANCHOR_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("joy", "explicit_joy", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:开心|高兴|快乐|幸福)")),
    ("excitement", "explicit_excitement", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:兴奋|激动)")),
    ("relief", "explicit_relief", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:松了口气|如释重负|轻松多了)")),
    ("gratitude", "explicit_gratitude", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:感谢|感激|感恩)")),
    ("pride", "explicit_pride", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:自豪|为自己骄傲)")),
    ("hope", "explicit_hope", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:希望|期待|盼着|有希望)")),
    ("sadness", "explicit_sadness", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:难过|伤心|失落|低落)")),
    (
        "loneliness",
        "explicit_loneliness",
        re.compile(r"(?:我|本人)(?:现在|最近|真的|特别|很|有点|一直)?(?:孤独|孤单|寂寞)"),
    ),
    ("anxiety", "explicit_anxiety", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:焦虑|担心|紧张|心慌)")),
    ("fear", "explicit_fear", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:害怕|恐惧|怕)")),
    ("anger", "explicit_anger", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:生气|愤怒|恼火|气死了)")),
    (
        "frustration",
        "explicit_frustration",
        re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:受挫|挫败|被卡住|怎么都不行|一直失败)"),
    ),
    (
        "disappointment",
        "explicit_disappointment",
        re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:失望|落空)"),
    ),
    ("guilt", "explicit_guilt", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:内疚|自责|惭愧)")),
    ("shame", "explicit_shame", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:羞愧|难堪|丢脸)")),
    (
        "helplessness",
        "explicit_helplessness",
        re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:无助|无能为力|没有办法|没办法)"),
    ),
    (
        "exhaustion",
        "explicit_exhaustion",
        re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:疲惫|筋疲力尽|没力气|累了|很累)"),
    ),
    (
        "confusion",
        "explicit_confusion",
        re.compile(
            rf"(?:我|本人){_ANCHOR_FILLERS}(?:纠结|迷茫|困惑|不知道(?:该|要不要)?(?:怎么|如何)?(?:选|办|做)|拿不准|拿不定主意)"
        ),
    ),
    ("calm", "explicit_calm", re.compile(rf"(?:我|本人){_ANCHOR_FILLERS}(?:平静|安定|放松|安心)")),
)
_ANCHOR_QUOTE_RE = re.compile(r"[\"“‘']([^\"”’']*)[\"”’']")
_ANCHOR_HYPOTHETICAL_RE = re.compile(r"(?:如果|要是|假如|万一|假设|会不会|可能会)")
_ANCHOR_NEGATION_RE = re.compile(r"(?:不|没|没有|并不|并没有|从不|未曾|别)$")
_ANCHOR_THIRD_PARTY_RE = re.compile(r"(?:他|她|朋友|同事|家人|对方|别人)(?:说|觉得|感到|表示|很|特别|正在)")
_CONJOINED_PREFIX = r"(?:又|也|还|却|同时|并且)\s*(?:很|特别|有点)?"
_CONJOINED_CUES: dict[str, str] = {
    "joy": r"开心|高兴|快乐|幸福",
    "excitement": r"兴奋|激动",
    "relief": r"松了口气|如释重负|轻松多了",
    "gratitude": r"感谢|感激|感恩",
    "pride": r"自豪|为自己骄傲",
    "hope": r"希望|期待|盼着|有希望",
    "sadness": r"难过|伤心|失落|低落",
    "loneliness": r"孤独|孤单|寂寞",
    "anxiety": r"焦虑|担心|紧张|心慌",
    "fear": r"害怕|恐惧|怕",
    "anger": r"生气|愤怒|恼火|气死了",
    "frustration": r"受挫|挫败|被卡住|怎么都不行|一直失败",
    "disappointment": r"失望|落空",
    "guilt": r"内疚|自责|惭愧",
    "shame": r"羞愧|难堪|丢脸",
    "helplessness": r"无助|无能为力|没有办法|没办法",
    "exhaustion": r"疲惫|筋疲力尽|没力气|累了|很累",
    "confusion": r"纠结|迷茫|困惑|拿不准|拿不定主意",
    "calm": r"平静|安定|放松|安心",
}
_CONJOINED_ANCHOR_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (
        label,
        f"explicit_{label}_conjoined",
        re.compile(_CONJOINED_PREFIX + rf"(?:{cues})"),
    )
    for label, cues in _CONJOINED_CUES.items()
)


def _anchor_match_is_current(text: str, match: re.Match[str]) -> bool:
    """Reject quoted, negated, hypothetical and third-party emotion claims."""

    for quoted in _ANCHOR_QUOTE_RE.finditer(text):
        if quoted.start(1) <= match.start() < quoted.end(1):
            return False
    sentence_start = max(
        text.rfind("。", 0, match.start()),
        text.rfind("！", 0, match.start()),
        text.rfind("？", 0, match.start()),
        text.rfind(";", 0, match.start()),
        text.rfind("；", 0, match.start()),
        text.rfind("\n", 0, match.start()),
    ) + 1
    prefix = text[sentence_start : match.start()]
    if _ANCHOR_NEGATION_RE.search(prefix.rstrip(" ，,、:：")):
        return False
    if _ANCHOR_HYPOTHETICAL_RE.search(prefix):
        return False
    if _ANCHOR_THIRD_PARTY_RE.search(prefix):
        return False
    ends = [
        position
        for mark in ("。", "！", "？", ";", "；", "\n")
        if (position := text.find(mark, match.start())) >= 0
    ]
    sentence = text[sentence_start : min(ends) if ends else len(text)]
    if not re.search(r"(?:我|本人)", sentence):
        return False
    return True


def extract_emotion_anchors(text: str) -> tuple[list[str], list[str]]:
    """Extract only high-precision first-person/current-state emotion cues.

    The return value is ``(labels, rule_codes)``.  Rule codes are internal
    metadata and never contain the matched source text.
    """

    matches: list[tuple[int, str, str]] = []
    for label, rule_code, pattern in (*_EMOTION_ANCHOR_PATTERNS, *_CONJOINED_ANCHOR_PATTERNS):
        for match in pattern.finditer(text):
            if _anchor_match_is_current(text, match):
                matches.append((match.start(), label, rule_code))
    matches.sort(key=lambda value: value[0])
    labels: list[str] = []
    rules: list[str] = []
    for _position, label, rule_code in matches:
        if label in labels:
            continue
        labels.append(label)
        rules.append(rule_code)
        if len(labels) >= 3:
            break
    return labels, rules


def _content_text(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    return str(content).strip()


def _parse_json(text: str) -> dict[str, object] | None:
    candidate = text.strip()
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _validation_error_summary(error: ValidationError) -> str:
    """Return field-level validation information without echoing model input values."""
    parts: list[str] = []
    for item in error.errors():
        location = ".".join(str(value) for value in item.get("loc", ())) or "$"
        message = str(item.get("msg", "校验失败"))
        error_type = str(item.get("type", "validation_error"))
        parts.append(f"{location}: {message} ({error_type})")
    return "; ".join(parts)[:2_000] or "Schema 校验失败"


def _validate_json_payload(
    parsed: dict[str, object] | None,
    validator: Callable[[dict[str, object]], object] | None,
) -> tuple[dict[str, object] | None, str | None]:
    if parsed is None:
        return None, "输出不是合法 JSON 对象"
    if validator is None:
        return parsed, None
    try:
        validator(parsed)
    except ValidationError as error:
        return None, _validation_error_summary(error)
    return parsed, None


def _invoke_json(
    model_alias: str,
    system_prompt: str,
    human_prompt: str,
    *,
    validator: Callable[[dict[str, object]], object] | None = None,
    allow_repair: bool = True,
) -> tuple[dict[str, object] | None, bool]:
    try:
        profile = model_registry.profile(model_alias)
        model = model_registry.chat_model(model_alias)
        if "api.deepseek.com" in profile.base_url:
            model = model.model_copy(update={"extra_body": {"thinking": {"type": "disabled"}}})
        response = model.bind(max_tokens=800).invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
        )
        raw = _content_text(response)
        parsed = _parse_json(raw)
        validated, validation_error = _validate_json_payload(parsed, validator)
        if validated is not None:
            return validated, True
        if not allow_repair:
            return None, False
        repair_system_prompt = (
            "你是结构化 JSON 修复器。只输出一个合法 JSON 对象，不要 Markdown、解释、代码围栏或额外字段。"
            "不要创造用户未表达的事实。\n\n"
            "原任务的完整约束如下：\n"
            f"{system_prompt}\n\n"
            "修复时必须重新检查 JSON 语法和全部字段约束。"
        )
        repair_human_prompt = (
            f"原始分类输入：\n{human_prompt[-12_000:]}\n\n"
            f"需要修复的模型输出：\n{raw[:8_000]}\n\n"
            f"已发现的问题：{validation_error or '输出无法解析为 JSON 对象'}"
        )
        repair_response = model.bind(max_tokens=800).invoke(
            [
                SystemMessage(content=repair_system_prompt),
                HumanMessage(content=repair_human_prompt),
            ]
        )
        repaired = _parse_json(_content_text(repair_response))
        repaired_validated, _repair_error = _validate_json_payload(repaired, validator)
        if repaired_validated is not None:
            return repaired_validated, True
        return None, False
    except Exception as error:
        logger.warning("陪伴结构化模型调用失败: %s", type(error).__name__)
        return None, False


def _route_strategy(
    support_need: SupportNeed,
    confidence: float,
    risk_level: RiskLevel,
    forced_mode: SupportMode | None,
) -> ResponseStrategy:
    if risk_level in {"high", "critical"}:
        return "safety_redirect"
    if risk_level == "medium":
        return "clarify"
    if forced_mode == "listen":
        return "listen"
    if forced_mode == "reflect":
        return "reflect"
    if forced_mode == "advice":
        return "advise"
    if confidence < LOW_CONFIDENCE_THRESHOLD or support_need == "unknown":
        return "clarify"
    return {
        "listen": "listen",
        "comfort": "comfort",
        "reflect": "reflect",
        "advice": "advise",
        "celebrate": "celebrate",
        "space": "validate",
        "unknown": "clarify",
    }[support_need]  # type: ignore[return-value]


def _analysis_prompts(
    text: str,
    context: str,
    prompt_version: str,
    anchor_labels: list[str],
    time_context: str = "",
) -> tuple[str, str]:
    """Build prompts without leaking internal anchor matches or raw rules."""

    system_prompt = (
        "你是情绪分类器，不是聊天助手。根据用户本轮消息和少量上下文完成结构化分析。\n"
        + analysis_schema_contract(prompt_version)
    )
    anchor_hint = "无"
    if prompt_version == EXPERIMENTAL_PROMPT_VERSION and anchor_labels:
        anchor_hint = "、".join(anchor_labels)
    human_prompt = (
        f"时间上下文：{time_context or '未提供'}\n\n"
        f"近期对话（仅用于指代消解）：\n{context[-6_000:]}\n\n"
        f"本轮用户消息：\n{text}\n\n"
        "确定性显式情绪线索提示（仅供排序参考，不能替代原文判断）："
        f"{anchor_hint}\n"
        "请严格按判定顺序输出 JSON；没有独立文本证据的候选不要添加。"
    )
    return system_prompt, human_prompt


def analyze_message(
    text: str,
    context: str,
    model_alias: str,
    *,
    forced_mode: SupportMode | None = None,
    safety: SafetyAssessment | None = None,
    safety_mode: SafetyMode = "standard",
    allow_repair: bool = True,
    prompt_version: str | None = None,
    classifier_version: str | None = None,
    use_experimental_prompt: bool = False,
    time_context: str = "",
) -> CompanionAnalysis:
    selected_prompt_version = prompt_version or (
        EXPERIMENTAL_PROMPT_VERSION if use_experimental_prompt else PROMPT_VERSION
    )
    selected_classifier_version = classifier_version or (
        EXPERIMENTAL_CLASSIFIER_VERSION if use_experimental_prompt else CLASSIFIER_VERSION
    )
    safety = safety or safety_precheck(text)
    if safety_mode == "standard" and safety.risk_level in {"high", "critical"}:
        return CompanionAnalysis(
            primary_emotion="neutral",
            candidate_emotions=["neutral"],
            intensity="unknown",
            support_need="space",
            confidence=1.0,
            risk_level=safety.risk_level,
            next_action="safety_redirect",
            model_alias=None,
            schema_valid=True,
            analysis_status="safety_redirected",
            prompt_version=selected_prompt_version,
            classifier_version=selected_classifier_version,
        )

    anchor_labels, _anchor_rules = extract_emotion_anchors(text)
    system_prompt, human_prompt = _analysis_prompts(
        text, context, selected_prompt_version, anchor_labels, time_context
    )
    raw, schema_valid = _invoke_json(
        model_alias,
        system_prompt,
        human_prompt,
        validator=CompanionAnalysisPayload.model_validate,
        allow_repair=allow_repair,
    )
    try:
        payload = CompanionAnalysisPayload.model_validate(raw or {})
    except ValidationError:
        payload = CompanionAnalysisPayload(
            event="",
            primary_emotion="neutral",
            candidate_emotions=["neutral"],
            intensity="unknown",
            support_need="unknown",
            confidence=0.0,
            risk_level=safety.risk_level,
        )
        schema_valid = False
    anchor_adjusted = False
    anchor_conflict = False
    if schema_valid and len(anchor_labels) == 1:
        anchor = anchor_labels[0]
        candidates = list(payload.candidate_emotions)
        if anchor in candidates:
            if candidates[0] != anchor:
                candidates = [anchor] + [label for label in candidates if label != anchor]
                payload = payload.model_copy(
                    update={"primary_emotion": anchor, "candidate_emotions": candidates}
                )
                anchor_adjusted = True
        else:
            anchor_conflict = True
            payload = payload.model_copy(
                update={"confidence": min(float(payload.confidence), LOW_CONFIDENCE_THRESHOLD - 0.01)}
            )
    effective_risk: RiskLevel = (
        safety.risk_level if safety.risk_level != "low" else payload.risk_level
    )
    route_risk: RiskLevel = "low" if safety_mode == "unfiltered" else effective_risk
    action: ResponseStrategy = (
        "clarify"
        if not schema_valid or anchor_conflict
        else _route_strategy(payload.support_need, payload.confidence, route_risk, forced_mode)
    )
    payload_data = payload.model_dump()
    payload_data["risk_level"] = effective_risk
    return CompanionAnalysis(
        **payload_data,
        next_action=action,
        model_alias=model_alias,
        prompt_version=selected_prompt_version,
        classifier_version=selected_classifier_version,
        schema_valid=schema_valid,
        analysis_status="valid" if schema_valid else "failed",
        anchor_labels=anchor_labels,
        anchor_adjusted=anchor_adjusted,
        anchor_conflict=anchor_conflict,
    )


def resolve_analyzer_alias(preferred_alias: str | None, fallback_alias: str) -> str:
    """Return a configured analyzer alias, falling back to the session model."""

    candidates = [alias for alias in (preferred_alias, fallback_alias) if alias]
    for alias in candidates:
        try:
            model_registry.chat_model(alias)
        except (RuntimeError, ValueError):
            continue
        return alias
    # Keep the session alias in metadata even when no key is configured; the
    # caller will receive the deterministic clarify fallback from analyze_message.
    return fallback_alias


def safety_redirect_text(risk_level: RiskLevel) -> str:
    if risk_level == "critical":
        return (
            "我先不继续角色扮演了。你现在的安全比继续聊天更重要。"
            "如果你已经准备伤害自己或他人，请先离开危险物品和现场，立刻联系身边可信任的人，"
            "并联系当地紧急服务或危机支持；如果可以，告诉我你现在是否已经处于安全的地方。"
        )
    return (
        "我听到这件事可能已经让你非常难受。我们先把安全放在前面："
        "请联系身边可信任的人陪着你，并使用当地适用的紧急或危机支持。"
        "我不会替你做诊断，但可以陪你先确认现在最需要的帮助。"
    )


_OUTPUT_SAFETY_RULES: tuple[tuple[str, str], ...] = (
    ("dependency_exclusivity", r"永远只属于你|你只能依靠我|我离不开你|你离不开我|没有我你就"),
    ("blame_or_shame", r"都是你的错|你活该|你应该为此负责"),
    ("false_confidentiality", r"我保证绝对保密"),
    ("diagnosis", r"你得了抑郁症"),
    (
        "fabricated_real_action",
        r"我可以替你报警|我已经替你报警|我已经联系(?:医院|警察|警方)|我会马上到你身边",
    ),
)


def output_safety_violations(text: str) -> list[str]:
    """Return stable rule codes without retaining the candidate response."""

    return [
        code
        for code, pattern in _OUTPUT_SAFETY_RULES
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]


def output_safety_ok(text: str) -> bool:
    return not output_safety_violations(text)


_ADVICE_MARKERS = re.compile(r"你可以|建议你|不妨|最好|应该|需要先|可以考虑|试着|不如")
_BULLET_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)、]|[一二三四五六七八九十]+[、.)])\s+")
_HEADING_LINE = re.compile(r"^\s{0,3}(?:#{1,6}\s+|【[^】]+】\s*$|[^。！？\n]{1,24}：\s*$)")


def response_style_violations(
    text: str, user_text: str, strategy: ResponseStrategy | None
) -> list[str]:
    """Detect only high-signal style drift; this deliberately avoids hard length caps."""

    violations: list[str] = []
    explicit_listen = detect_support_mode(user_text) == "listen"
    if strategy != "advise" and explicit_listen and _ADVICE_MARKERS.search(text):
        violations.append("advice_overreach")
    lines = [line for line in text.splitlines() if line.strip()]
    bullet_count = sum(bool(_BULLET_LINE.match(line)) for line in lines)
    if strategy in {"listen", "validate", "clarify", "comfort", "reflect", "celebrate"} and (
        bullet_count >= 3 or any(_HEADING_LINE.match(line) for line in lines)
    ):
        violations.append("structured_list")
    return violations


def rewrite_companion_response(
    model_alias: str,
    user_text: str,
    candidate_response: str,
    violation_codes: list[str],
    persona_text: str,
    strategy_guide: str,
    *,
    time_context: str = "",
) -> str | None:
    """Rewrite style drift once while keeping the selected character's voice."""

    system_prompt = (
        "你是 PersonalAgent 的陪伴回复修订器。只输出给用户看的自然中文正文，不要标题、JSON、"
        "Markdown、规则说明或修订过程。保留当前角色的身份、关系和语气，只修正本轮交互方向的偏离。"
        "若用户明确说只想倾诉或不用建议，不得给建议；普通倾听、确认感受、安慰和轻度梳理不要使用标题、"
        "编号或项目符号。不要硬套固定句式，也不要为了变短而删除用户刚说的关键内容。"
    )
    human_prompt = (
        f"时间上下文：{time_context or '未提供'}\n\n"
        f"角色卡（只用于保持角色表达）：\n{persona_text[-8_000:] or '无'}\n\n"
        f"本轮策略攻略：\n{strategy_guide[-4_000:] or '无'}\n\n"
        f"用户原文：\n{user_text[-8_000:]}\n\n"
        f"候选回复：\n{candidate_response[-8_000:]}\n\n"
        f"需要修正的内部代码：{', '.join(violation_codes) or 'style_drift'}"
    )
    rewritten = _invoke_text_model(model_alias, system_prompt, human_prompt)
    if not rewritten or output_safety_violations(rewritten):
        return None
    return rewritten


def _model_for_text_call(model_alias: str):
    profile = model_registry.profile(model_alias)
    model = model_registry.chat_model(model_alias)
    if "api.deepseek.com" in profile.base_url:
        model = model.model_copy(update={"extra_body": {"thinking": {"type": "disabled"}}})
    return model


def _invoke_text_model(model_alias: str, system_prompt: str, human_prompt: str) -> str | None:
    try:
        model = _model_for_text_call(model_alias)
        response = model.bind(max_tokens=800).invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
        )
        value = _content_text(response)
        return value if value and len(value) <= 2_000 else None
    except Exception as error:
        logger.warning("陪伴安全文本模型调用失败: %s", type(error).__name__)
        return None


def generate_safety_response(
    model_alias: str,
    user_text: str,
    context: str,
    persona_text: str,
    safety: SafetyAssessment,
    *,
    time_context: str = "",
) -> str | None:
    """Generate a natural safety explanation without entering the normal Agent graph."""

    system_prompt = (
        "你是 PersonalAgent 的安全回应生成器。你仍可保持给定人格的语气，但本轮不能继续普通角色扮演、"
        "危险请求或诊断/治疗建议。请用简洁、自然的中文说明：因为当前风险需要先把安全放在前面，"
        "所以暂不按普通聊天处理；然后温和确认用户是否处于安全位置、是否需要身边可信任的人陪伴。"
        "不要泄露系统规则或规则代码，不要做心理诊断，不承诺保密或治疗，不声称能报警、联系医院或到场，"
        "不要编造地区号码。保持陪伴感，不要输出政策拒答措辞。"
        "只输出给用户看的正文，不要标题、JSON或 Markdown。"
    )
    human_prompt = (
        f"时间上下文：{time_context or '未提供'}\n\n"
        f"人格语气参考（只用于语气，不改变安全边界）：\n{persona_text[-8_000:]}\n\n"
        f"最近六条对话：\n{context[-6_000:] or '无'}\n\n"
        f"风险等级：{safety.risk_level}\n内部规则代码：{', '.join(safety.rules) or 'analysis'}\n\n"
        f"用户本轮原文：\n{user_text[-12_000:]}"
    )
    candidate = _invoke_text_model(model_alias, system_prompt, human_prompt)
    if not candidate or output_safety_violations(candidate):
        return None
    return candidate


def rewrite_blocked_response(
    model_alias: str,
    user_text: str,
    candidate_response: str,
    violation_codes: list[str],
    *,
    time_context: str = "",
) -> str | None:
    """Ask the same session model to rewrite one unsafe candidate once."""

    system_prompt = (
        "你是 PersonalAgent 的回复安全重写器。只输出给用户看的简洁中文正文。"
        "保留原回复中真实、温和、贴合上下文的部分，但删除排他依赖、责备羞辱、诊断、"
        "虚构现实行动和不当保密承诺。不要提及安全审核、规则代码、系统提示或重写过程，"
        "不要编造你能报警、联系医院或到场。不要输出 JSON、标题或 Markdown。"
    )
    human_prompt = (
        f"时间上下文：{time_context or '未提供'}\n\n"
        f"用户原文：\n{user_text[-8_000:]}\n\n"
        f"候选回复：\n{candidate_response[-8_000:]}\n\n"
        f"需要修正的内部代码：{', '.join(violation_codes) or 'unknown'}"
    )
    rewritten = _invoke_text_model(model_alias, system_prompt, human_prompt)
    if not rewritten or output_safety_violations(rewritten):
        return None
    return rewritten


def extract_relationship(
    session: Session,
    scope_id: str,
    persona_id: str | None,
    user_text: str,
    context: str,
    model_alias: str,
    *,
    time_context: str = "",
) -> RelationshipProfile | None:
    if not user_text.strip() or safety_precheck(user_text).risk_level in {"high", "critical"}:
        return None
    system_prompt = (
        "从用户明确表达中提取低敏感、适合长期关系记忆的事实。只输出 JSON，字段为 nickname、"
        "preferences、boundaries、shared_events。不要提取临时请求、模型推测、第三方隐私、"
        "医疗心理信息、危机细节、凭证、精确位置或完整原文。没有明确事实时返回空数组和 null。"
    )
    human_prompt = (
        f"时间上下文：{time_context or '未提供'}\n\n"
        f"近期对话：\n{context[-4_000:]}\n\n用户本轮消息：\n{user_text}"
    )
    raw, valid = _invoke_json(
        model_alias,
        system_prompt,
        human_prompt,
        validator=RelationshipFacts.model_validate,
    )
    if not valid or raw is None:
        return None
    try:
        facts = RelationshipFacts.model_validate(raw)
    except ValidationError:
        return None
    nickname = _safe_relation_value(facts.nickname or "")
    preferences = [item for item in (_safe_relation_value(value) for value in facts.preferences) if item]
    boundaries = [item for item in (_safe_relation_value(value) for value in facts.boundaries) if item]
    shared_events = [item for item in (_safe_relation_value(value) for value in facts.shared_events) if item]
    if not any((nickname, preferences, boundaries, shared_events)):
        return None

    item = get_relationship(session, scope_id, persona_id, create=True)
    assert item is not None
    expected_version = item.version
    next_nickname = nickname or item.nickname
    try:
        current_boundaries = json.loads(item.boundaries or "{}")
    except json.JSONDecodeError:
        current_boundaries = {}
    if not isinstance(current_boundaries, dict):
        current_boundaries = {}
    existing_items = current_boundaries.get("items", [])
    if not isinstance(existing_items, list):
        existing_items = []
    existing_preferences = current_boundaries.get("preferences", [])
    if not isinstance(existing_preferences, list):
        existing_preferences = []
    if boundaries:
        current_boundaries["items"] = list(dict.fromkeys([*existing_items, *boundaries]))
    event_parts = [item.shared_summary] if item.shared_summary else []
    event_parts.extend(shared_events)
    if preferences:
        current_boundaries["preferences"] = list(
            dict.fromkeys([*existing_preferences, *preferences])
        )
    next_summary = "；".join(dict.fromkeys(event_parts))[:4_000]
    next_boundaries = json.dumps(current_boundaries, ensure_ascii=False)
    result = session.execute(
        update(RelationshipProfile)
        .where(
            RelationshipProfile.id == item.id,
            RelationshipProfile.version == expected_version,
        )
        .values(
            nickname=next_nickname,
            shared_summary=next_summary,
            boundaries=next_boundaries,
            version=expected_version + 1,
            updated_at=now_utc_naive(),
        )
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        session.rollback()
        logger.warning("关系资料乐观锁冲突，保留已有资料: scope=%s persona=%s", scope_id, persona_id)
        return None
    session.commit()
    session.refresh(item)
    return item


def companion_analysis_status(
    item: EmotionAssessment, session: Session | None = None
) -> AnalysisStatus:
    if (
        item.next_action == "safety_redirect"
        and item.risk_level in {"high", "critical"}
    ):
        return "safety_redirected"
    if item.schema_valid:
        return "valid"
    if session is not None:
        retry_job = session.get(Job, item.id)
        if (
            retry_job is not None
            and retry_job.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE
            and retry_job.status in {"queued", "running"}
        ):
            return "retrying"
    return "failed"


def assessment_dict(item: EmotionAssessment, session: Session | None = None) -> dict[str, object]:
    try:
        emotions: object = json.loads(item.candidate_emotions or "[]")
    except json.JSONDecodeError:
        emotions = []
    try:
        correction: object = json.loads(item.correction or "{}")
    except json.JSONDecodeError:
        correction = {}
    analysis_status = companion_analysis_status(item, session)
    analysis_valid = analysis_status == "valid"
    visible_emotions = emotions if analysis_valid and isinstance(emotions, list) else []
    visible_primary = item.primary_emotion if analysis_valid else None
    visible_primary_display = (
        EMOTION_LABEL_ZH.get(visible_primary) if isinstance(visible_primary, str) else None
    )
    visible_intensity = item.intensity if analysis_valid else None
    visible_support_need = item.support_need if analysis_valid else None
    effective_emotions = visible_emotions
    effective_primary = visible_primary
    effective_primary_display = visible_primary_display
    effective_support_need = visible_support_need
    if isinstance(correction, dict):
        corrected_emotions = correction.get("emotions")
        if (
            isinstance(corrected_emotions, list)
            and 1 <= len(corrected_emotions) <= 3
            and all(value in EMOTION_LABELS for value in corrected_emotions)
        ):
            effective_emotions = corrected_emotions
            effective_primary = str(corrected_emotions[0])
            effective_primary_display = EMOTION_LABEL_ZH.get(effective_primary, effective_primary)
        corrected_support_need = correction.get("support_need")
        if corrected_support_need in SUPPORT_NEED_ZH:
            effective_support_need = str(corrected_support_need)
    return {
        "id": item.id,
        "user_message_id": item.user_message_id,
        "scope_id": item.scope_id,
        "candidate_emotions": visible_emotions,
        "candidate_emotions_display": [
            EMOTION_LABEL_ZH.get(str(value), str(value)) for value in visible_emotions
        ],
        "primary_emotion": visible_primary,
        "primary_emotion_display": visible_primary_display,
        "effective_candidate_emotions": effective_emotions,
        "effective_candidate_emotions_display": [
            EMOTION_LABEL_ZH.get(str(value), str(value)) for value in effective_emotions
        ],
        "effective_primary_emotion": effective_primary,
        "effective_primary_emotion_display": effective_primary_display,
        "intensity": visible_intensity,
        "support_need": visible_support_need,
        "support_need_display": (
            SUPPORT_NEED_ZH.get(visible_support_need)
            if isinstance(visible_support_need, str)
            else None
        ),
        "effective_support_need": effective_support_need,
        "effective_support_need_display": SUPPORT_NEED_ZH.get(effective_support_need)
        if isinstance(effective_support_need, str)
        else None,
        "confidence": item.confidence if analysis_valid else None,
        "risk_level": item.risk_level,
        "next_action": item.next_action,
        "next_action_display": STRATEGY_ZH.get(item.next_action, item.next_action),
        "model_alias": item.model_alias,
        "prompt_version": item.prompt_version,
        "classifier_version": item.classifier_version,
        "schema_valid": item.schema_valid,
        "analysis_status": analysis_status,
        "safety_intercepted": analysis_status == "safety_redirected",
        "correction": correction,
        "created_at": utc_isoformat(item.created_at),
    }


def feedback_dict(item: ResponseFeedback) -> dict[str, object]:
    try:
        correction: object = json.loads(item.correction or "{}")
    except json.JSONDecodeError:
        correction = {}
    return {
        "id": item.id,
        "assistant_message_id": item.assistant_message_id,
        "scope_id": item.scope_id,
        "feedback": item.feedback,
        "correction": correction,
        "created_at": utc_isoformat(item.created_at),
        "updated_at": utc_isoformat(item.updated_at),
    }


def safety_event_dict(item: SafetyEvent) -> dict[str, object]:
    try:
        details: object = json.loads(item.details or "{}")
    except json.JSONDecodeError:
        details = {}
    return {
        "id": item.id,
        "message_id": item.message_id,
        "scope_id": item.scope_id,
        "risk_level": item.risk_level,
        "action": item.action,
        "detector_version": item.detector_version,
        "details": details,
        "created_at": utc_isoformat(item.created_at),
    }


def now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
