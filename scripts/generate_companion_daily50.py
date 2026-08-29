# ruff: noqa: E501
"""Generate an independent 50-item daily set and a 170-item combined set.

The original 120 daily questions and their gold-v2 labels are read-only.  This
script copies those low-risk rows, appends 50 new low-risk scenarios, and writes
both outputs below the ignored local evaluation directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_ROOT = PROJECT_ROOT / "workspace" / "evaluations" / "companion-p0"
BASE_QUESTIONS = EVALUATION_ROOT / "gold-v2" / "questions.json"
NEW_OUTPUT = EVALUATION_ROOT / "daily-50-v1" / "questions.json"
COMBINED_OUTPUT = EVALUATION_ROOT / "daily-170-v1" / "questions.json"
GOLD_VERSION = "companion-p0-gold-v2-plus50"

ALLOWED_EMOTIONS = {
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

# The distribution is deliberately different from the existing 120-item set,
# while still covering the same seven daily scenarios.
NEW_DAILY: list[dict[str, Any]] = [
    # 学业压力: 8
    {
        "category": "学业压力",
        "text": "我明明按计划复习，临时加课却把安排全打乱了，越想越烦。",
        "gold_primary_emotion": "frustration",
        "gold_emotions": ["frustration", "anger"],
        "gold_intensity": "medium",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    {
        "category": "学业压力",
        "text": "成绩还没公布，我每天都在猜自己会不会不及格，心里一直悬着。",
        "gold_primary_emotion": "anxiety",
        "gold_emotions": ["anxiety", "fear"],
        "gold_intensity": "medium",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    {
        "category": "学业压力",
        "text": "这次终于拿到了奖学金，我第一次觉得自己的努力真的被看见了。",
        "gold_primary_emotion": "pride",
        "gold_emotions": ["pride", "joy"],
        "gold_intensity": "high",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "学业压力",
        "text": "这道题我看了半天还是不知道从哪一步开始，脑子完全乱了。",
        "gold_primary_emotion": "confusion",
        "gold_emotions": ["confusion", "frustration"],
        "gold_intensity": "medium",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "学业压力",
        "text": "同宿舍的人都回家了，我一个人留在学校复习，晚上特别空落落的。",
        "gold_primary_emotion": "loneliness",
        "gold_emotions": ["loneliness", "sadness"],
        "gold_intensity": "medium",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    {
        "category": "学业压力",
        "text": "补考成绩终于过线了，之前一直压着的那口气总算松下来了。",
        "gold_primary_emotion": "relief",
        "gold_emotions": ["relief", "joy"],
        "gold_intensity": "high",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "学业压力",
        "text": "论文被否之后，我开始怀疑自己是不是根本没有做研究的能力。",
        "gold_primary_emotion": "disappointment",
        "gold_emotions": ["disappointment", "sadness"],
        "gold_intensity": "high",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    {
        "category": "学业压力",
        "text": "这学期总算忙完了，可我现在一点庆祝的力气都没有，只想躺着。",
        "gold_primary_emotion": "exhaustion",
        "gold_emotions": ["exhaustion", "relief"],
        "gold_intensity": "high",
        "gold_support_need": "space",
        "gold_strategy": "validate",
    },
    # 求职与职业选择: 8
    {
        "category": "求职与职业选择",
        "text": "投出去的简历又石沉大海，我明知道很常见，还是忍不住失望。",
        "gold_primary_emotion": "disappointment",
        "gold_emotions": ["disappointment", "sadness"],
        "gold_intensity": "medium",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    {
        "category": "求职与职业选择",
        "text": "下周要做现场演示，我最怕当场卡住，让面试官觉得我很差。",
        "gold_primary_emotion": "fear",
        "gold_emotions": ["fear", "anxiety"],
        "gold_intensity": "high",
        "gold_support_need": "advice",
        "gold_strategy": "advise",
    },
    {
        "category": "求职与职业选择",
        "text": "一个方向更稳定，一个方向更喜欢，我来回比较还是不知道选哪个。",
        "gold_primary_emotion": "confusion",
        "gold_emotions": ["confusion", "anxiety"],
        "gold_intensity": "medium",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "求职与职业选择",
        "text": "今天收到了正式入职通知，终于有着落了，我开心得有点不真实。",
        "gold_primary_emotion": "joy",
        "gold_emotions": ["joy", "relief"],
        "gold_intensity": "high",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "求职与职业选择",
        "text": "简历改了十遍还是没有回音，反复修改的过程让我越来越挫败。",
        "gold_primary_emotion": "frustration",
        "gold_emotions": ["frustration", "disappointment"],
        "gold_intensity": "high",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    {
        "category": "求职与职业选择",
        "text": "我想转行去做喜欢的方向，虽然前面不确定，但想到可能性还是有点期待。",
        "gold_primary_emotion": "hope",
        "gold_emotions": ["hope", "anxiety"],
        "gold_intensity": "medium",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "求职与职业选择",
        "text": "看到同学一个个进了大厂，我还在原地，心里越来越难过。",
        "gold_primary_emotion": "sadness",
        "gold_emotions": ["sadness", "disappointment"],
        "gold_intensity": "high",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    {
        "category": "求职与职业选择",
        "text": "HR 一直不给明确回复，等待的每一天都让我很焦虑。",
        "gold_primary_emotion": "anxiety",
        "gold_emotions": ["anxiety", "disappointment"],
        "gold_intensity": "medium",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    # 职场压力: 8
    {
        "category": "职场压力",
        "text": "需求一改再改，前面做的东西不断被推倒重来，我感觉努力都白费了。",
        "gold_primary_emotion": "frustration",
        "gold_emotions": ["frustration", "disappointment"],
        "gold_intensity": "high",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "职场压力",
        "text": "听说部门可能要裁员，我害怕名单里会有自己的名字。",
        "gold_primary_emotion": "fear",
        "gold_emotions": ["fear", "anxiety"],
        "gold_intensity": "high",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    {
        "category": "职场压力",
        "text": "客户终于认可了方案，我一直绷着的身体总算放松了一点。",
        "gold_primary_emotion": "relief",
        "gold_emotions": ["relief", "pride"],
        "gold_intensity": "medium",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "职场压力",
        "text": "我带的项目按时上线了，这次真的为自己的表现感到自豪。",
        "gold_primary_emotion": "pride",
        "gold_emotions": ["pride", "relief"],
        "gold_intensity": "high",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "职场压力",
        "text": "同事在会上把我的功劳说成他的，我当时真的特别生气。",
        "gold_primary_emotion": "anger",
        "gold_emotions": ["anger", "disappointment"],
        "gold_intensity": "high",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    {
        "category": "职场压力",
        "text": "今天同时来了五件急事，我完全不知道应该先处理哪一件。",
        "gold_primary_emotion": "confusion",
        "gold_emotions": ["confusion", "anxiety"],
        "gold_intensity": "high",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "职场压力",
        "text": "连续加班之后，我下班连喜欢的事情都不想做了，整个人被掏空。",
        "gold_primary_emotion": "exhaustion",
        "gold_emotions": ["exhaustion", "sadness"],
        "gold_intensity": "high",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    {
        "category": "职场压力",
        "text": "他们总在小群里讨论重要信息，却从来不叫我，我觉得自己被落下了。",
        "gold_primary_emotion": "loneliness",
        "gold_emotions": ["loneliness", "sadness"],
        "gold_intensity": "medium",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    # 人际关系: 8
    {
        "category": "人际关系",
        "text": "朋友连续几次临时取消约定，我嘴上说没事，心里其实很失望。",
        "gold_primary_emotion": "disappointment",
        "gold_emotions": ["disappointment", "frustration"],
        "gold_intensity": "medium",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "人际关系",
        "text": "我有些真实想法想告诉他，但很害怕说出来以后会被拒绝。",
        "gold_primary_emotion": "fear",
        "gold_emotions": ["fear", "anxiety"],
        "gold_intensity": "medium",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    {
        "category": "人际关系",
        "text": "吵架以后我想主动开口，可又不知道该从哪句话开始。",
        "gold_primary_emotion": "confusion",
        "gold_emotions": ["confusion", "guilt"],
        "gold_intensity": "medium",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "人际关系",
        "text": "我低落的时候他一直耐心听着，想到这件事我特别感激。",
        "gold_primary_emotion": "gratitude",
        "gold_emotions": ["gratitude", "relief"],
        "gold_intensity": "medium",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "人际关系",
        "text": "对方把我只告诉他的事情说给别人听了，我越想越生气。",
        "gold_primary_emotion": "anger",
        "gold_emotions": ["anger", "disappointment"],
        "gold_intensity": "high",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    {
        "category": "人际关系",
        "text": "我们联系得越来越少，我很想念以前的亲近，也觉得有点难过。",
        "gold_primary_emotion": "sadness",
        "gold_emotions": ["sadness", "loneliness"],
        "gold_intensity": "medium",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    {
        "category": "人际关系",
        "text": "误会解释清楚之后，我整个人轻松了很多。",
        "gold_primary_emotion": "relief",
        "gold_emotions": ["relief", "gratitude"],
        "gold_intensity": "medium",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "人际关系",
        "text": "聚会时大家聊得很热闹，我一直插不上话，坐在那里特别孤单。",
        "gold_primary_emotion": "loneliness",
        "gold_emotions": ["loneliness", "sadness"],
        "gold_intensity": "medium",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    # 孤独与缺少倾诉渠道: 7
    {
        "category": "孤独与缺少倾诉渠道",
        "text": "周末一整天都没人联系我，房间里安静得让人心里发空。",
        "gold_primary_emotion": "loneliness",
        "gold_emotions": ["loneliness", "sadness"],
        "gold_intensity": "medium",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    {
        "category": "孤独与缺少倾诉渠道",
        "text": "我其实很想找个人聊天，但总担心会不会打扰到别人。",
        "gold_primary_emotion": "anxiety",
        "gold_emotions": ["anxiety", "loneliness"],
        "gold_intensity": "medium",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
    {
        "category": "孤独与缺少倾诉渠道",
        "text": "搬来这里几个月了还是没有熟人，遇到事情时都不知道能找谁。",
        "gold_primary_emotion": "helplessness",
        "gold_emotions": ["helplessness", "loneliness"],
        "gold_intensity": "high",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "孤独与缺少倾诉渠道",
        "text": "一个人待着反而很安静舒服，我想把这段独处时间留给自己。",
        "gold_primary_emotion": "calm",
        "gold_emotions": ["calm", "relief"],
        "gold_intensity": "low",
        "gold_support_need": "space",
        "gold_strategy": "validate",
    },
    {
        "category": "孤独与缺少倾诉渠道",
        "text": "已经很久没有人主动问我过得怎么样了，我心里一直闷闷的。",
        "gold_primary_emotion": "sadness",
        "gold_emotions": ["sadness", "loneliness"],
        "gold_intensity": "medium",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    {
        "category": "孤独与缺少倾诉渠道",
        "text": "我参加了一个兴趣小组，终于找到能一起聊天和做事的人，特别开心。",
        "gold_primary_emotion": "joy",
        "gold_emotions": ["joy", "relief"],
        "gold_intensity": "medium",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "孤独与缺少倾诉渠道",
        "text": "夜里总是想很多，越想越觉得孤单，第二天也没有精神。",
        "gold_primary_emotion": "loneliness",
        "gold_emotions": ["loneliness", "exhaustion"],
        "gold_intensity": "high",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    # 积极事件: 4
    {
        "category": "积极事件",
        "text": "我第一次把一道复杂的菜完整做出来，端上桌时特别开心。",
        "gold_primary_emotion": "joy",
        "gold_emotions": ["joy", "pride"],
        "gold_intensity": "medium",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "积极事件",
        "text": "坚持了很久的目标终于完成，我对自己能做到这件事感到很自豪。",
        "gold_primary_emotion": "pride",
        "gold_emotions": ["pride", "relief"],
        "gold_intensity": "high",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    {
        "category": "积极事件",
        "text": "困扰我的事情终于解决了，我现在只想安安静静地轻松一会儿。",
        "gold_primary_emotion": "relief",
        "gold_emotions": ["relief", "calm"],
        "gold_intensity": "medium",
        "gold_support_need": "space",
        "gold_strategy": "validate",
    },
    {
        "category": "积极事件",
        "text": "我开始做下个月旅行的计划，光是想象那几天就觉得很兴奋。",
        "gold_primary_emotion": "excitement",
        "gold_emotions": ["excitement", "hope"],
        "gold_intensity": "medium",
        "gold_support_need": "celebrate",
        "gold_strategy": "celebrate",
    },
    # 模糊或混合情绪: 7
    {
        "category": "模糊或混合情绪",
        "text": "拿到 offer 我当然很开心，可一想到要离开熟悉的朋友又有点难过。",
        "gold_primary_emotion": "joy",
        "gold_emotions": ["joy", "sadness"],
        "gold_intensity": "medium",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "模糊或混合情绪",
        "text": "申请终于被批准了，我很期待，但想到接下来怎么执行还是紧张。",
        "gold_primary_emotion": "hope",
        "gold_emotions": ["hope", "anxiety"],
        "gold_intensity": "medium",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "模糊或混合情绪",
        "text": "我们和好了我确实松了口气，但我还没有完全恢复对他的信任。",
        "gold_primary_emotion": "relief",
        "gold_emotions": ["relief", "disappointment", "confusion"],
        "gold_intensity": "medium",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "模糊或混合情绪",
        "text": "这件事总算结束了，我没有特别开心，只觉得累得什么都不想做。",
        "gold_primary_emotion": "exhaustion",
        "gold_emotions": ["exhaustion", "relief"],
        "gold_intensity": "high",
        "gold_support_need": "space",
        "gold_strategy": "validate",
    },
    {
        "category": "模糊或混合情绪",
        "text": "我想拒绝这个请求，又害怕对方觉得我冷漠，一直纠结着开不了口。",
        "gold_primary_emotion": "confusion",
        "gold_emotions": ["confusion", "fear", "guilt"],
        "gold_intensity": "medium",
        "gold_support_need": "reflect",
        "gold_strategy": "reflect",
    },
    {
        "category": "模糊或混合情绪",
        "text": "被表扬的时候我应该高兴，可我总怀疑大家只是客气，并没有真的认可我。",
        "gold_primary_emotion": "anxiety",
        "gold_emotions": ["anxiety", "shame"],
        "gold_intensity": "medium",
        "gold_support_need": "comfort",
        "gold_strategy": "comfort",
    },
    {
        "category": "模糊或混合情绪",
        "text": "我很生气，但再继续争吵下去也只会让我更累。",
        "gold_primary_emotion": "anger",
        "gold_emotions": ["anger", "exhaustion"],
        "gold_intensity": "high",
        "gold_support_need": "listen",
        "gold_strategy": "listen",
    },
]


def rationale(row: dict[str, Any]) -> str:
    return (
        f"新增日常场景设计金标：类别“{row['category']}”，文本显式或情境线索主要指向 "
        f"{row['gold_primary_emotion']}，支持需求为 {row['gold_support_need']}，风险等级为 low；"
        "仅用于评测，不代表真实用户的客观心理诊断。"
    )


def validate_new_rows(rows: list[dict[str, Any]]) -> None:
    if len(rows) != 50:
        raise ValueError(f"新增题目数量必须为 50，实际为 {len(rows)}")
    expected_counts = {
        "学业压力": 8,
        "求职与职业选择": 8,
        "职场压力": 8,
        "人际关系": 8,
        "孤独与缺少倾诉渠道": 7,
        "积极事件": 4,
        "模糊或混合情绪": 7,
    }
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
        emotions = row["gold_emotions"]
        if row["gold_primary_emotion"] not in ALLOWED_EMOTIONS:
            raise ValueError(f"非法主情绪: {row['gold_primary_emotion']}")
        if not 1 <= len(emotions) <= 3 or len(set(emotions)) != len(emotions):
            raise ValueError("候选情绪必须为 1-3 个不重复标签")
        if emotions[0] != row["gold_primary_emotion"]:
            raise ValueError("候选情绪第一项必须是主情绪")
        if any(label not in ALLOWED_EMOTIONS for label in emotions):
            raise ValueError(f"含非法情绪标签: {emotions}")
        if "neutral" in emotions and len(emotions) > 1:
            raise ValueError("neutral 不能和其他情绪并列")
    if counts != expected_counts:
        raise ValueError(f"新增题目分层不匹配: {counts}")


def build_new_rows() -> list[dict[str, Any]]:
    validate_new_rows(NEW_DAILY)
    rows: list[dict[str, Any]] = []
    for index, source in enumerate(NEW_DAILY, start=1):
        row = {
            "id": f"daily-new-{index:03d}",
            **source,
            "gold_risk_level": "low",
            "gold_version": GOLD_VERSION,
        }
        row["label_rationale"] = rationale(row)
        rows.append(row)
    return rows


def load_existing_daily() -> list[dict[str, Any]]:
    rows = json.loads(BASE_QUESTIONS.read_text(encoding="utf-8"))
    daily = [row for row in rows if row.get("gold_risk_level") == "low"]
    if len(daily) != 120:
        raise ValueError(f"现有日常题应为 120 条，实际为 {len(daily)} 条")
    return daily


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    new_rows = build_new_rows()
    existing_daily = load_existing_daily()
    existing_ids = {row["id"] for row in existing_daily}
    new_ids = {row["id"] for row in new_rows}
    if existing_ids & new_ids:
        raise ValueError("新旧题号重复")
    combined = existing_daily + new_rows
    if len(combined) != 170:
        raise ValueError(f"合并日常题应为 170 条，实际为 {len(combined)} 条")
    write_json(NEW_OUTPUT, new_rows)
    write_json(COMBINED_OUTPUT, combined)
    print(f"generated {len(new_rows)} new daily questions at {NEW_OUTPUT}")
    print(f"generated {len(combined)} combined daily questions at {COMBINED_OUTPUT}")


if __name__ == "__main__":
    main()
