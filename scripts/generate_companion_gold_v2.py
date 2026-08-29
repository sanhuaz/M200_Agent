# ruff: noqa: E501
"""Create the versioned Companion P0 gold-v2 audit without overwriting v1.

Gold labels are scenario-design labels, not claims about objective emotions.
The overrides below are deliberately small and reviewable; every source row is
copied to the ignored gold-v2 directory together with an audit record.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "workspace" / "evaluations" / "companion-p0" / "questions.json"
OUTPUT_DIR = PROJECT_ROOT / "workspace" / "evaluations" / "companion-p0" / "gold-v2"
GOLD_VERSION = "companion-p0-gold-v2"

# Only the two emotion fields are changed.  These cases follow the v4 rubric:
# explicit process anger is anger, lack of connection is loneliness, and a
# choice/uncertainty core is confusion rather than a background hope.
OVERRIDES: dict[str, tuple[str, list[str], str]] = {
    "daily-026": (
        "anxiety",
        ["anxiety", "fear"],
        "担心年龄是否太晚是当前直接关注点，转行愿望属于背景目标；按 anxiety/hope 对比规则调整。",
    ),
    "daily-041": (
        "anger",
        ["anger", "frustration"],
        "临时改需求并归责于用户，文本明确表达生气且存在不公平指向；anger 排在过程受阻的 frustration 前。",
    ),
    "daily-073": (
        "frustration",
        ["frustration", "anger"],
        "意见反复不合和争吵描述过程受阻，未直接表达生气或不公；frustration 比 anger 更严格。",
    ),
    "daily-083": (
        "loneliness",
        ["loneliness", "sadness"],
        "一个人待着、缺少连接是句子核心，空虚是伴随痛苦；按 loneliness/sadness 边界调整。",
    ),
    "daily-087": (
        "loneliness",
        ["loneliness", "sadness"],
        "“孤单”和需要有人倾听是直接情绪与连接线索，loneliness 应排在 sadness 前。",
    ),
    "daily-116": (
        "confusion",
        ["confusion", "fear"],
        "核心是“不知道自己是否准备好”的判断困难；机会看起来好只是背景，不以 hope 做主情绪。",
    ),
}

AMBIGUOUS_IDS = {
    "daily-013": "截止日期带来的具体威胁与弥散担忧均可解释，保留严格原金标并标记歧义。",
    "daily-037": "接受 offer 的积极可能性与离开熟悉城市的失落并存，保留原金标。",
    "daily-109": "纠结和舍不得同时出现，confusion 作为严格主情绪仍可成立，标记歧义。",
    "daily-117": "独立完成旅行的自豪与孤单并存，保留原金标并标记歧义。",
}


def build() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    source_rows = json.loads(SOURCE.read_text(encoding="utf-8"))
    if not isinstance(source_rows, list) or len(source_rows) != 150:
        raise ValueError("v1 题集必须包含 150 条")
    output: list[dict[str, object]] = []
    audit: list[dict[str, object]] = []
    for value in source_rows:
        if not isinstance(value, dict):
            raise ValueError("题集行必须是对象")
        row = dict(value)
        question_id = str(row["id"])
        old_primary = str(row["gold_primary_emotion"])
        old_emotions = list(row["gold_emotions"])
        override = OVERRIDES.get(question_id)
        if override:
            new_primary, new_emotions, reason = override
            row["gold_primary_emotion"] = new_primary
            row["gold_emotions"] = new_emotions
            status = "corrected"
        elif question_id in AMBIGUOUS_IDS:
            new_primary = old_primary
            new_emotions = old_emotions
            reason = AMBIGUOUS_IDS[question_id]
            status = "ambiguous"
        else:
            new_primary = old_primary
            new_emotions = old_emotions
            reason = "按 v4 判定顺序复核，未发现需要改动的主情绪或候选标签。"
            status = "unchanged"
        row["gold_version"] = GOLD_VERSION
        output.append(row)
        audit.append(
            {
                "id": question_id,
                "audit_status": status,
                "original_gold_primary_emotion": old_primary,
                "original_gold_emotions": old_emotions,
                "new_gold_primary_emotion": new_primary,
                "new_gold_emotions": new_emotions,
                "reason": reason,
            }
        )
    return output, audit


def main() -> None:
    rows, audit = build()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "questions.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUTPUT_DIR / "audit.json").write_text(
        json.dumps(
            {
                "gold_version": GOLD_VERSION,
                "source": "questions.json",
                "changed_fields_only": ["gold_primary_emotion", "gold_emotions"],
                "override_count": len(OVERRIDES),
                "ambiguous_count": len(AMBIGUOUS_IDS),
                "records": audit,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"generated {len(rows)} gold-v2 questions and {len(audit)} audit records at {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
