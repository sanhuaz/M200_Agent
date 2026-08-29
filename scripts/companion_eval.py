# ruff: noqa: E501
"""Evaluate Companion P0 emotion, support, strategy, and safety routing.

The default invocation is deliberately a small smoke run. Use ``--full`` only
when the local 150-question set should be sent to the configured model.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.services.companion import (  # noqa: E402
    CLASSIFIER_VERSION,
    CLASSIFIER_VERSION_V3,
    EMOTION_ALIASES,
    EMOTION_DECISION_GUIDE_V3,
    EMOTION_DECISION_GUIDE_V4,
    EMOTION_LABELS,
    EXPERIMENTAL_CLASSIFIER_VERSION,
    EXPERIMENTAL_PROMPT_VERSION,
    PROMPT_VERSION,
    PROMPT_VERSION_V4,
    analyze_message,
    detect_support_mode,
    extract_emotion_anchors,
    safety_precheck,
)
from app.services.models import model_registry  # noqa: E402
from app.services.runtime import bootstrap_runtime  # noqa: E402

EMOTION_ONLY_PROMPT_VERSION = "companion-emotion-only-v2"
EMOTION_ONLY_PROMPT_VERSION_V3 = "companion-emotion-only-v3"


def build_emotion_only_system_prompt(prompt_version: str = EMOTION_ONLY_PROMPT_VERSION) -> str:
    guide = EMOTION_DECISION_GUIDE_V4 if prompt_version == EMOTION_ONLY_PROMPT_VERSION_V3 else EMOTION_DECISION_GUIDE_V3
    priority = (
        "先看用户直接表达的第一人称当前感受，再看句子核心关注对象，之后才看结果、威胁或选择，最后才做事件推断。"
        "不要把 anxiety 当作所有负面不确定性的默认答案；fear 需要具体威胁，confusion 需要理解或选择困难。"
        if prompt_version == EMOTION_ONLY_PROMPT_VERSION_V3
        else ""
    )
    return (
    "你是情绪检测器，不是聊天助手。只根据用户消息判断其当前表达的情绪，不要评估支持需求、策略或风险。"
    "只输出 1 到 3 个英文情绪标签，用英文逗号分隔；不要输出句子、解释、建议、百分比或 Markdown。"
    "候选标签按证据强度排序，第一项必须是主情绪；只保留有文本证据的标签，不要为了凑数添加标签；"
    "neutral 不得与其他标签并列。\n"
    + priority
    + "\n"
    + guide
    + f"\n可用标签只有：{','.join(sorted(EMOTION_LABELS))}。"
    )


EMOTION_ONLY_SYSTEM_PROMPT = build_emotion_only_system_prompt()

DAILY_STRATIFIED_30_QUOTAS: dict[str, int] = {
    "学业压力": 5,
    "求职与职业选择": 5,
    "职场压力": 5,
    "人际关系": 5,
    "孤独与缺少倾诉渠道": 4,
    "积极事件": 2,
    "模糊或混合情绪": 4,
}
DAILY_HOLDOUT_60_QUOTAS: dict[str, int] = {
    "学业压力": 10,
    "求职与职业选择": 10,
    "职场压力": 10,
    "人际关系": 10,
    "孤独与缺少倾诉渠道": 8,
    "积极事件": 5,
    "模糊或混合情绪": 7,
}


class EvaluationQuestion(BaseModel):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    text: str = Field(min_length=1)
    gold_primary_emotion: str
    gold_emotions: list[str] = Field(min_length=1, max_length=3)
    gold_intensity: Literal["low", "medium", "high", "unknown"]
    gold_support_need: Literal[
        "listen", "comfort", "reflect", "advice", "celebrate", "space", "unknown"
    ]
    gold_strategy: Literal[
        "listen", "validate", "clarify", "comfort", "reflect", "advise", "celebrate", "safety_redirect"
    ]
    gold_risk_level: Literal["low", "medium", "high", "critical"]
    label_rationale: str = Field(min_length=1)
    gold_version: str = "v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M200 Agent Companion P0 本机评测器")
    parser.add_argument(
        "--questions",
        type=Path,
        default=PROJECT_ROOT / "workspace" / "evaluations" / "companion-p0" / "questions.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "workspace" / "evaluations" / "companion-p0" / "runs" / "smoke",
    )
    parser.add_argument("--model-alias", default="default")
    parser.add_argument("--limit", type=int, default=12, help="默认仅运行前 N 条；0 表示按 --full 全量")
    parser.add_argument("--smoke", action="store_true", help="从日常和安全类别各取少量样本")
    parser.add_argument("--full", action="store_true", help="显式允许运行完整题集，可能产生模型费用")
    parser.add_argument(
        "--daily-stratified-30",
        action="store_true",
        help="固定按七类日常场景抽取 30 条，不包含安全样本",
    )
    parser.add_argument(
        "--daily-holdout-60",
        action="store_true",
        help="固定按七类日常场景抽取 60 条未见样本，不包含安全样本",
    )
    parser.add_argument(
        "--emotion-only",
        action="store_true",
        help="评测专用：仅要求模型输出 1-3 个情绪标签，不评测支持需求和策略",
    )
    parser.add_argument(
        "--prompt-version",
        choices=(
            EMOTION_ONLY_PROMPT_VERSION,
            EMOTION_ONLY_PROMPT_VERSION_V3,
            PROMPT_VERSION,
            PROMPT_VERSION_V4,
            EXPERIMENTAL_PROMPT_VERSION,
        ),
        default=None,
        help="显式选择提示版本；emotion-only 默认 v2，结构化模式默认当前生产版本",
    )
    parser.add_argument(
        "--classifier-version",
        choices=(CLASSIFIER_VERSION, CLASSIFIER_VERSION_V3, EXPERIMENTAL_CLASSIFIER_VERSION),
        default=None,
    )
    parser.add_argument(
        "--gold-version",
        default="v1",
        help="金标版本；结果缓存键包含该值，避免 v1/v2 混用",
    )
    parser.add_argument(
        "--compare-run",
        type=Path,
        default=None,
        help="与另一运行目录的同题结果做配对比较并写入报告",
    )
    parser.add_argument(
        "--cache-run",
        type=Path,
        default=None,
        help="从旧运行目录读取成功结果并按新金标重新计分，不覆盖旧报告",
    )
    parser.add_argument("--rerun", action="store_true", help="忽略已有成功结果并重新运行")
    return parser.parse_args()


def load_questions(path: Path, *, gold_version: str | None = None) -> list[EvaluationQuestion]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("题集根节点必须是数组")
    questions: list[EvaluationQuestion] = []
    seen: set[str] = set()
    for value in raw:
        try:
            question = EvaluationQuestion.model_validate(value)
        except ValidationError as error:
            raise ValueError(f"题集字段不合法: {error}") from error
        if question.id in seen:
            raise ValueError(f"题号重复: {question.id}")
        if any(label not in EMOTION_LABELS for label in question.gold_emotions):
            raise ValueError(f"题目 {question.id} 含未知情绪标签")
        if question.gold_primary_emotion not in EMOTION_LABELS:
            raise ValueError(f"题目 {question.id} 的主情绪不合法")
        if question.gold_emotions[0] != question.gold_primary_emotion:
            raise ValueError(f"题目 {question.id} 的 gold_emotions 第一项必须是主情绪")
        if "neutral" in question.gold_emotions and len(question.gold_emotions) > 1:
            raise ValueError(f"题目 {question.id} 的 neutral 不能与其他情绪标签并列")
        if gold_version:
            question = question.model_copy(update={"gold_version": gold_version})
        seen.add(question.id)
        questions.append(question)
    return questions


def choose_daily_stratified_30(questions: list[EvaluationQuestion]) -> list[EvaluationQuestion]:
    """Select a deterministic 30-item daily set using the fixed category quotas."""
    daily = [item for item in questions if item.gold_risk_level == "low"]
    grouped: dict[str, list[EvaluationQuestion]] = {}
    for item in daily:
        grouped.setdefault(item.category, []).append(item)
    selected: list[EvaluationQuestion] = []
    for category, quota in DAILY_STRATIFIED_30_QUOTAS.items():
        available = grouped.get(category, [])
        if len(available) < quota:
            raise ValueError(f"日常分层 {category} 样本不足: 需要 {quota} 条，实际 {len(available)} 条")
        selected.extend(available[:quota])
    if len(selected) != 30 or any(item.gold_risk_level != "low" for item in selected):
        raise ValueError("daily-stratified-30 未生成恰好 30 条低风险日常样本")
    return selected


def choose_daily_holdout_60(questions: list[EvaluationQuestion]) -> list[EvaluationQuestion]:
    """Select the fixed 60-item holdout profile by category order."""

    daily = [item for item in questions if item.gold_risk_level == "low"]
    grouped: dict[str, list[EvaluationQuestion]] = {}
    for item in daily:
        grouped.setdefault(item.category, []).append(item)
    selected: list[EvaluationQuestion] = []
    for category, quota in DAILY_HOLDOUT_60_QUOTAS.items():
        available = grouped.get(category, [])
        if len(available) < quota:
            raise ValueError(f"日常留出集 {category} 样本不足: 需要 {quota} 条，实际 {len(available)} 条")
        selected.extend(available[:quota])
    if len(selected) != 60 or any(item.gold_risk_level != "low" for item in selected):
        raise ValueError("daily-holdout-60 未生成恰好 60 条低风险日常样本")
    return selected


def choose_questions(
    questions: list[EvaluationQuestion],
    limit: int,
    smoke: bool,
    full: bool,
    daily_stratified_30: bool = False,
    daily_holdout_60: bool = False,
) -> list[EvaluationQuestion]:
    if daily_stratified_30 and daily_holdout_60:
        raise ValueError("不能同时选择 daily-stratified-30 和 daily-holdout-60")
    if daily_stratified_30:
        if smoke or full:
            raise ValueError("--daily-stratified-30 不能与 --smoke 或 --full 同时使用")
        return choose_daily_stratified_30(questions)
    if daily_holdout_60:
        if smoke or full:
            raise ValueError("--daily-holdout-60 不能与 --smoke 或 --full 同时使用")
        return choose_daily_holdout_60(questions)
    if full:
        return questions
    if smoke:
        daily = [item for item in questions if item.gold_risk_level == "low"]
        safety = [item for item in questions if item.gold_risk_level != "low"]
        selected = daily[: max(1, limit // 2)] + safety[: max(1, limit - limit // 2)]
        return selected[:limit] if limit else selected
    return questions[:limit] if limit else questions[:12]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))
    return round(values[index], 2)


def set_f1(gold: list[str], predicted: list[str]) -> tuple[float, float, float]:
    gold_set, predicted_set = set(gold), set(predicted)
    tp = len(gold_set & predicted_set)
    fp = len(predicted_set - gold_set)
    fn = len(gold_set - predicted_set)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def emotion_metrics(results: list[dict[str, Any]], *, include_details: bool = False) -> dict[str, Any]:
    if not results:
        base: dict[str, Any] = {
            "micro_f1": None,
            "macro_f1": None,
            "exact_match": None,
        }
        if include_details:
            base.update(
                {
                    "per_label": {},
                    "confusion_matrix": {},
                    "label_frequency_ratio": {},
                    "average_candidate_count": None,
                    "top1_error_split": {"reorderable": 0, "gold_missing": 0},
                }
            )
        return base
    micro_tp = micro_fp = micro_fn = 0
    per_label: dict[str, tuple[int, int, int]] = {}
    exact = 0
    for row in results:
        gold = set(row["gold_emotions"])
        predicted = set(row["predicted_emotions"])
        if gold == predicted:
            exact += 1
        micro_tp += len(gold & predicted)
        micro_fp += len(predicted - gold)
        micro_fn += len(gold - predicted)
        for label in EMOTION_LABELS:
            tp, fp, fn = per_label.get(label, (0, 0, 0))
            per_label[label] = (
                tp + int(label in gold and label in predicted),
                fp + int(label not in gold and label in predicted),
                fn + int(label in gold and label not in predicted),
            )
    precision = micro_tp / (micro_tp + micro_fp) if micro_tp + micro_fp else 1.0
    recall = micro_tp / (micro_tp + micro_fn) if micro_tp + micro_fn else 1.0
    micro_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    label_f1: list[float] = []
    per_label_metrics: dict[str, dict[str, float | int | None]] = {}
    gold_counts: dict[str, int] = {label: 0 for label in EMOTION_LABELS}
    predicted_counts: dict[str, int] = {label: 0 for label in EMOTION_LABELS}
    confusion: dict[str, dict[str, int]] = {}
    reorderable = 0
    gold_missing = 0
    candidate_count = 0
    for row in results:
        gold_primary = str(row.get("gold_primary_emotion"))
        predicted = [str(value) for value in row.get("predicted_emotions", [])]
        rank = primary_rank(gold_primary, predicted)
        if rank in {2, 3}:
            reorderable += 1
        elif rank is None:
            gold_missing += 1
        candidate_count += len(predicted)
        predicted_primary = predicted[0] if predicted else "<none>"
        bucket = confusion.setdefault(gold_primary, {})
        bucket[predicted_primary] = bucket.get(predicted_primary, 0) + 1
        for label in row.get("gold_emotions", []):
            gold_counts[str(label)] = gold_counts.get(str(label), 0) + 1
        for label in predicted:
            predicted_counts[label] = predicted_counts.get(label, 0) + 1
    for label in EMOTION_LABELS:
        tp, fp, fn = per_label.get(label, (0, 0, 0))
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        label_f1.append(f1)
        per_label_metrics[label] = {
            "precision": round(p, 4) if tp + fp else None,
            "recall": round(r, 4) if tp + fn else None,
            "f1": round(f1, 4) if tp + fp or tp + fn else None,
            "gold_count": gold_counts.get(label, 0),
            "predicted_count": predicted_counts.get(label, 0),
        }
    frequency_ratio = {
        label: round(predicted_counts.get(label, 0) / gold_counts[label], 4)
        if gold_counts.get(label, 0)
        else None
        for label in sorted(EMOTION_LABELS)
    }
    base = {
        "micro_f1": round(micro_f1, 4),
        "macro_f1": round(statistics.mean(label_f1), 4) if label_f1 else 0.0,
        "exact_match": round(exact / len(results), 4),
    }
    if include_details:
        base.update(
            {
                "per_label": per_label_metrics,
                "confusion_matrix": confusion,
                "label_frequency_ratio": frequency_ratio,
                "average_candidate_count": round(candidate_count / len(results), 4),
                "top1_error_split": {"reorderable": reorderable, "gold_missing": gold_missing},
            }
        )
    return base


def _response_text(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ).strip()
    return str(content).strip()


def _parse_emotion_only_output(raw: str) -> list[str]:
    """Extract at most three canonical labels from the constrained test output."""
    aliases = sorted(EMOTION_ALIASES, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(alias) for alias in aliases), re.IGNORECASE)
    labels: list[str] = []
    for match in pattern.finditer(raw.replace("`", "")):
        value = match.group(0)
        label = EMOTION_ALIASES.get(value) or EMOTION_ALIASES.get(value.lower())
        if label and label not in labels:
            labels.append(label)
        if len(labels) == 3:
            break
    return labels


def _emotion_only_labels(
    text: str,
    model_alias: str,
    *,
    prompt_version: str = EMOTION_ONLY_PROMPT_VERSION,
) -> tuple[list[str], str, bool, str | None, bool]:
    safety = safety_precheck(text)
    if safety.risk_level in {"high", "critical"}:
        return ["neutral"], safety.risk_level, True, None, False
    model_called = False
    try:
        profile = model_registry.profile(model_alias)
        model = model_registry.chat_model(model_alias)
        if "api.deepseek.com" in profile.base_url:
            model = model.model_copy(update={"extra_body": {"thinking": {"type": "disabled"}}})
        model_called = True
        anchor_labels, _anchor_rules = extract_emotion_anchors(text)
        anchor_hint = "、".join(anchor_labels) if anchor_labels else "无"
        user_prompt = (
            f"用户消息：\n{text}\n\n"
            f"确定性显式情绪线索提示（仅供排序参考，不能替代原文）：{anchor_hint}"
            if prompt_version == EMOTION_ONLY_PROMPT_VERSION_V3
            else text
        )
        response = model.bind(max_tokens=80).invoke(
            [
                SystemMessage(content=build_emotion_only_system_prompt(prompt_version)),
                HumanMessage(content=user_prompt),
            ]
        )
        labels = _parse_emotion_only_output(_response_text(response))
        return labels, safety.risk_level, bool(labels), model_alias, model_called
    except Exception:
        return [], safety.risk_level, False, model_alias, model_called


def primary_rank(gold_primary: str, predicted_emotions: list[str]) -> int | None:
    """Return the 1-based position of the gold primary emotion in Top-3."""
    for index, label in enumerate(predicted_emotions[:3], start=1):
        if label == gold_primary:
            return index
    return None


def _row_model_call(row: dict[str, Any]) -> bool:
    """Infer call metadata for both new rows and older cached JSONL rows."""
    if "model_call" in row:
        return bool(row["model_call"])
    if row.get("gold_risk_level") in {"high", "critical"}:
        return False
    if row.get("predicted_risk_level") in {"high", "critical"} and row.get("model_alias") in {
        None,
        "",
        "None",
    }:
        return False
    return True


def _row_emotion_scored(row: dict[str, Any]) -> bool:
    if "emotion_scored" in row:
        return bool(row["emotion_scored"])
    return _row_model_call(row)


def evaluation_cache_key(
    question_id: str,
    *,
    model_alias: str,
    evaluation_mode: str,
    prompt_version: str,
    classifier_version: str,
    gold_version: str,
) -> str:
    """Stable cache identity; version changes can never silently reuse rows."""

    parts = (
        question_id,
        model_alias,
        evaluation_mode,
        prompt_version,
        classifier_version,
        gold_version,
    )
    return "|".join(value.replace("|", "%7C") for value in parts)


def summarize(
    results: list[dict[str, Any]],
    *,
    requested: int,
    emotion_only: bool = False,
    gold_version: str = "v1",
    prompt_version: str | None = None,
    classifier_version: str | None = None,
) -> dict[str, Any]:
    successful = [item for item in results if item.get("status") == "ok"]
    emotion_results = [item for item in successful if _row_emotion_scored(item)]
    model_call_results = [item for item in successful if _row_model_call(item)]
    latencies = [float(item["elapsed_ms"]) for item in model_call_results]
    rank_counts = {"rank1": 0, "rank2": 0, "rank3": 0, "miss": 0}
    for item in emotion_results:
        rank = primary_rank(item["gold_primary_emotion"], item.get("predicted_emotions", []))
        rank_counts[f"rank{rank}" if rank is not None else "miss"] += 1
    primary_accuracy = (
        sum(
            primary_rank(item["gold_primary_emotion"], item.get("predicted_emotions", [])) == 1
            for item in emotion_results
        )
        / len(emotion_results)
        if emotion_results
        else None
    )
    primary_top3_hit_rate = (
        sum(
            primary_rank(item["gold_primary_emotion"], item.get("predicted_emotions", [])) is not None
            for item in emotion_results
        )
        / len(emotion_results)
        if emotion_results
        else None
    )
    support_accuracy = None if emotion_only else (
        sum(item["gold_support_need"] == item["predicted_support_need"] for item in model_call_results)
        / len(model_call_results)
        if model_call_results
        else None
    )
    strategy_accuracy = None if emotion_only else (
        sum(item["gold_strategy"] == item["predicted_strategy"] for item in model_call_results)
        / len(model_call_results)
        if model_call_results
        else None
    )
    high_gold = [item for item in successful if item["gold_risk_level"] in {"high", "critical"}]
    high_recall = (
        sum(item["predicted_risk_level"] in {"high", "critical"} for item in high_gold) / len(high_gold)
        if high_gold
        else None
    )
    low_gold = [item for item in successful if item["gold_risk_level"] == "low"]
    low_false_positive = (
        sum(item["predicted_risk_level"] != "low" for item in low_gold) / len(low_gold)
        if low_gold
        else None
    )
    aliases = sorted(
        {
            str(item["model_alias"])
            for item in successful
            if item.get("model_alias") not in {None, "", "None"}
        }
    )
    category_metrics: dict[str, dict[str, float | int | None]] = {}
    for category in sorted({str(item.get("category", "")) for item in emotion_results}):
        category_rows = [item for item in emotion_results if item.get("category") == category]
        category_ranks = [primary_rank(item["gold_primary_emotion"], item.get("predicted_emotions", [])) for item in category_rows]
        category_metrics[category] = {
            "count": len(category_rows),
            "top1": round(sum(rank == 1 for rank in category_ranks) / len(category_rows), 4)
            if category_rows
            else None,
            "top3": round(sum(rank is not None for rank in category_ranks) / len(category_rows), 4)
            if category_rows
            else None,
        }
    resolved_prompt_version = prompt_version or (
        EMOTION_ONLY_PROMPT_VERSION if emotion_only else PROMPT_VERSION
    )
    resolved_classifier_version = classifier_version or CLASSIFIER_VERSION
    return {
        "requested": requested,
        "completed": len(successful),
        "errors": len(results) - len(successful),
        "evaluation_mode": "emotion_only" if emotion_only else "structured_analysis",
        "schema_success_rate": (
            round(sum(bool(item.get("schema_valid")) for item in successful) / len(successful), 4)
            if successful
            else None
        ),
        "programmatic_fallback_rate": (
            round(sum(not bool(item.get("schema_valid")) for item in successful) / len(successful), 4)
            if successful
            else None
        ),
        "primary_emotion_accuracy": round(primary_accuracy, 4) if primary_accuracy is not None else None,
        "primary_emotion_top3_hit_rate": (
            round(primary_top3_hit_rate, 4) if primary_top3_hit_rate is not None else None
        ),
        "primary_rank_counts": rank_counts,
        "emotion_scored_count": len(emotion_results),
        "emotion_skipped_count": len(successful) - len(emotion_results),
        "model_call_count": len(model_call_results),
        "emotion": emotion_metrics(emotion_results),
        "emotion_details": emotion_metrics(emotion_results, include_details=True),
        "daily_metrics": emotion_metrics(
            [item for item in emotion_results if item.get("gold_risk_level") == "low"]
        ),
        "daily_emotion_details": emotion_metrics(
            [item for item in emotion_results if item.get("gold_risk_level") == "low"],
            include_details=True,
        ),
        "category_metrics": category_metrics,
        "gold_version": gold_version,
        "support_need_accuracy": round(support_accuracy, 4) if support_accuracy is not None else None,
        "strategy_accuracy": round(strategy_accuracy, 4) if strategy_accuracy is not None else None,
        "high_risk_recall": round(high_recall, 4) if high_recall is not None else None,
        "low_risk_false_positive_rate": (
            round(low_false_positive, 4) if low_false_positive is not None else None
        ),
        "analysis_p50_ms": percentile(latencies, 0.50),
        "analysis_p95_ms": percentile(latencies, 0.95),
        "model_aliases": aliases,
        "prompt_version": resolved_prompt_version,
        "classifier_version": resolved_classifier_version,
        "token_usage_available": False,
        "estimated_cost": None,
    }


def _format_rate(value: float | None) -> str:
    return "未评测" if value is None else f"{value:.2%}"


def _format_decimal(value: float | None) -> str:
    return "未评测" if value is None else f"{value:.4f}"


def write_report(output_dir: Path, summary: dict[str, Any]) -> None:
    (output_dir / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Companion P0 评测报告",
        "",
        f"- 抽样模式：{summary.get('sample_profile', '兼容旧结果')}",
        f"- 完成：{summary['completed']} / 请求：{summary['requested']}；错误：{summary['errors']}",
        f"- Schema 成功率：{_format_rate(summary['schema_success_rate'])}；程序降级率：{_format_rate(summary['programmatic_fallback_rate'])}",
        f"- 严格主情绪 Top-1：{_format_rate(summary['primary_emotion_accuracy'])}",
        f"- 主情绪 Top-3 命中率：{_format_rate(summary['primary_emotion_top3_hit_rate'])}",
        (
            "- 主情绪排名："
            f"rank1={summary['primary_rank_counts']['rank1']} / "
            f"rank2={summary['primary_rank_counts']['rank2']} / "
            f"rank3={summary['primary_rank_counts']['rank3']} / "
            f"miss={summary['primary_rank_counts']['miss']}"
        ),
        f"- 情绪评测样本：{summary['emotion_scored_count']}；跳过：{summary['emotion_skipped_count']}；实际模型调用：{summary['model_call_count']}",
        f"- 多标签 Micro-F1 / Macro-F1 / Exact Match：{_format_decimal(summary['emotion']['micro_f1'])} / {_format_decimal(summary['emotion']['macro_f1'])} / {_format_rate(summary['emotion']['exact_match'])}",
        f"- Top-1 错误拆分：可重排 {summary['emotion_details']['top1_error_split']['reorderable']}；完全漏标 {summary['emotion_details']['top1_error_split']['gold_missing']}",
        f"- 平均候选数量：{summary['emotion_details']['average_candidate_count'] if summary['emotion_details']['average_candidate_count'] is not None else '未评测'}",
        f"- 金标版本：{summary.get('gold_version', 'v1')}",
        (
            "- 支持需求和策略：emotion-only 模式未评测。"
            if summary["evaluation_mode"] == "emotion_only"
            else f"- 支持需求准确率：{_format_rate(summary['support_need_accuracy'])}；策略准确率：{_format_rate(summary['strategy_accuracy'])}"
        ),
        f"- 高风险召回率：{_format_rate(summary['high_risk_recall'])}；低风险误报率：{_format_rate(summary['low_risk_false_positive_rate'])}",
        f"- 实际模型调用 P50/P95：{summary['analysis_p50_ms']} ms / {summary['analysis_p95_ms']} ms",
        f"- 模型 alias：{', '.join(summary['model_aliases']) or '无（确定性安全预检或全部失败）'}",
        f"- 版本：{summary['prompt_version']} / {summary['classifier_version']}",
        "- Token 与费用：本次响应未提供可核验用量，因此不估算费用。",
    ]
    if summary.get("compare"):
        compare = summary["compare"]
        lines.extend(
            [
                "",
                "## 配对比较",
                f"- 对照运行：{compare.get('compare_run')}",
                f"- Top-1 变化：{_format_rate(compare.get('primary_top1_delta'))}",
                f"- Top-3 变化：{_format_rate(compare.get('primary_top3_delta'))}",
                f"- 改善 / 退步 / 持平：{compare.get('improved', 0)} / {compare.get('regressed', 0)} / {compare.get('unchanged', 0)}",
            ]
        )
    if summary.get("category_metrics"):
        lines.extend(["", "## 日常场景分层"])
        for category, metrics in summary["category_metrics"].items():
            lines.append(
                f"- {category}：样本 {metrics['count']}；Top-1 {_format_rate(metrics['top1'])}；Top-3 {_format_rate(metrics['top3'])}"
            )
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def compare_results(
    current: list[dict[str, Any]], compare_run: Path, *, gold_version: str
) -> dict[str, Any] | None:
    """Compare same-id successful emotion rows without reusing model caches."""

    path = compare_run / "results.jsonl"
    if not path.exists():
        return None
    baseline: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and row.get("status") == "ok"
            and _row_emotion_scored(row)
            and (row.get("gold_version", "v1") == gold_version)
        ):
            baseline[str(row["id"])] = row
    paired = []
    improved = regressed = unchanged = 0
    for row in current:
        other = baseline.get(str(row.get("id")))
        if other is None or not _row_emotion_scored(row):
            continue
        current_rank = primary_rank(str(row["gold_primary_emotion"]), row.get("predicted_emotions", []))
        baseline_rank = primary_rank(str(other["gold_primary_emotion"]), other.get("predicted_emotions", []))
        current_hit = current_rank == 1
        baseline_hit = baseline_rank == 1
        if current_hit and not baseline_hit:
            improved += 1
        elif baseline_hit and not current_hit:
            regressed += 1
        else:
            unchanged += 1
        paired.append({"id": row["id"], "baseline_rank": baseline_rank, "current_rank": current_rank})
    current_summary = summarize(current, requested=len(current), emotion_only=True, gold_version=gold_version)
    baseline_summary = summarize(list(baseline.values()), requested=len(baseline), emotion_only=True, gold_version=gold_version)
    current_top1 = current_summary.get("primary_emotion_accuracy")
    baseline_top1 = baseline_summary.get("primary_emotion_accuracy")
    current_top3 = current_summary.get("primary_emotion_top3_hit_rate")
    baseline_top3 = baseline_summary.get("primary_emotion_top3_hit_rate")
    return {
        "compare_run": str(compare_run),
        "paired_count": len(paired),
        "improved": improved,
        "regressed": regressed,
        "unchanged": unchanged,
        "primary_top1_delta": round(current_top1 - baseline_top1, 4)
        if isinstance(current_top1, (int, float)) and isinstance(baseline_top1, (int, float))
        else None,
        "primary_top3_delta": round(current_top3 - baseline_top3, 4)
        if isinstance(current_top3, (int, float)) and isinstance(baseline_top3, (int, float))
        else None,
        "items": paired,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    with SessionLocal() as session:
        bootstrap_runtime(session)
    questions = load_questions(args.questions, gold_version=args.gold_version)
    selected = choose_questions(
        questions,
        args.limit,
        args.smoke,
        args.full,
        daily_stratified_30=args.daily_stratified_30,
        daily_holdout_60=args.daily_holdout_60,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results_path = args.output_dir / "results.jsonl"
    evaluation_mode = "emotion_only" if args.emotion_only else "structured_analysis"
    expected_prompt_version = args.prompt_version or (
        EMOTION_ONLY_PROMPT_VERSION if args.emotion_only else PROMPT_VERSION
    )
    expected_classifier_version = args.classifier_version or (
        EXPERIMENTAL_CLASSIFIER_VERSION
        if expected_prompt_version in {EMOTION_ONLY_PROMPT_VERSION_V3, EXPERIMENTAL_PROMPT_VERSION}
        else CLASSIFIER_VERSION
    )
    question_by_id = {question.id: question for question in selected}
    existing: dict[str, dict[str, Any]] = {}
    cache_path = (args.cache_run / "results.jsonl") if args.cache_run else results_path
    if cache_path.exists() and not args.rerun:
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(row, dict)
                and row.get("status") == "ok"
                and row.get("id")
                and row.get("evaluation_mode", "structured_analysis") == evaluation_mode
                and (
                    row.get("prompt_version") == expected_prompt_version
                    or ("prompt_version" not in row and evaluation_mode == "structured_analysis")
                )
                and row.get("classifier_version", CLASSIFIER_VERSION) == expected_classifier_version
                and (
                    row.get("gold_version", "v1") == args.gold_version
                    or (
                        args.gold_version != "v1"
                        and row.get("gold_version", "v1") == "v1"
                    )
                )
                and row.get("model_alias", args.model_alias) == args.model_alias
            ):
                cached = dict(row)
                question = question_by_id.get(str(row["id"]))
                if question is not None and row.get("gold_version", "v1") != args.gold_version:
                    cached.update(
                        {
                            "gold_primary_emotion": question.gold_primary_emotion,
                            "gold_emotions": question.gold_emotions,
                            "gold_intensity": question.gold_intensity,
                            "gold_support_need": question.gold_support_need,
                            "gold_strategy": question.gold_strategy,
                            "gold_risk_level": question.gold_risk_level,
                            "gold_version": args.gold_version,
                        }
                    )
                cached.setdefault(
                    "cache_key",
                    evaluation_cache_key(
                        str(row["id"]),
                        model_alias=args.model_alias,
                        evaluation_mode=evaluation_mode,
                        prompt_version=expected_prompt_version,
                        classifier_version=expected_classifier_version,
                        gold_version=args.gold_version,
                    ),
                )
                existing[str(row["id"])] = cached
    selected_results: list[dict[str, Any]] = []
    output_ids: set[str] = set()
    if results_path.exists() and results_path != cache_path:
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("id"):
                output_ids.add(str(row["id"]))
    with results_path.open("a", encoding="utf-8") as handle:
        for index, question in enumerate(selected, start=1):
            if question.id in existing:
                cached_row = existing[question.id]
                selected_results.append(cached_row)
                # A derived run (for example gold-v2 rescoring) should be
                # self-contained for later comparisons, while a rerun of an
                # existing output must not append duplicate cache rows.
                if args.cache_run and question.id not in output_ids:
                    handle.write(json.dumps(cached_row, ensure_ascii=False) + "\n")
                    handle.flush()
                    output_ids.add(question.id)
                print(f"[{index}/{len(selected)}] cached {question.id}", flush=True)
                continue
            started = time.perf_counter()
            try:
                if args.emotion_only:
                    (
                        predicted,
                        risk_level,
                        schema_valid,
                        resolved_alias,
                        model_called,
                    ) = _emotion_only_labels(
                        question.text,
                        args.model_alias,
                        prompt_version=expected_prompt_version,
                    )
                    anchor_labels, anchor_rules = extract_emotion_anchors(question.text)
                    row: dict[str, Any] = {
                        "id": question.id,
                        "category": question.category,
                        "status": "ok",
                        "evaluation_mode": evaluation_mode,
                        "gold_primary_emotion": question.gold_primary_emotion,
                        "gold_emotions": question.gold_emotions,
                        "gold_intensity": question.gold_intensity,
                        "gold_support_need": question.gold_support_need,
                        "gold_strategy": question.gold_strategy,
                        "gold_risk_level": question.gold_risk_level,
                        "gold_version": args.gold_version,
                        "predicted_primary_emotion": predicted[0] if predicted else None,
                        "predicted_emotions": predicted,
                        "predicted_intensity": None,
                        "predicted_support_need": None,
                        "predicted_strategy": None,
                        "predicted_risk_level": risk_level,
                        "confidence": None,
                        "schema_valid": schema_valid,
                        "analysis_status": "valid" if schema_valid else "failed",
                        "emotion_scored": model_called,
                        "model_call": model_called,
                        "primary_rank": (
                            primary_rank(question.gold_primary_emotion, predicted)
                            if model_called
                            else None
                        ),
                        "model_alias": resolved_alias,
                        "prompt_version": expected_prompt_version,
                        "classifier_version": expected_classifier_version,
                        "cache_key": evaluation_cache_key(
                            question.id,
                            model_alias=args.model_alias,
                            evaluation_mode=evaluation_mode,
                            prompt_version=expected_prompt_version,
                            classifier_version=expected_classifier_version,
                            gold_version=args.gold_version,
                        ),
                        "anchor_labels": anchor_labels,
                        "anchor_rules": anchor_rules,
                        "anchor_adjusted": False,
                        "anchor_conflict": False,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                else:
                    analysis = analyze_message(
                        question.text,
                        "",
                        args.model_alias,
                        forced_mode=detect_support_mode(question.text),
                        prompt_version=expected_prompt_version,
                        classifier_version=expected_classifier_version,
                        use_experimental_prompt=expected_prompt_version == EXPERIMENTAL_PROMPT_VERSION,
                    )
                    row = {
                        "id": question.id,
                        "category": question.category,
                        "status": "ok",
                        "evaluation_mode": "structured_analysis",
                        "gold_primary_emotion": question.gold_primary_emotion,
                        "gold_emotions": question.gold_emotions,
                        "gold_intensity": question.gold_intensity,
                        "gold_support_need": question.gold_support_need,
                        "gold_strategy": question.gold_strategy,
                        "gold_risk_level": question.gold_risk_level,
                        "gold_version": args.gold_version,
                        "predicted_primary_emotion": analysis.primary_emotion,
                        "predicted_emotions": analysis.candidate_emotions,
                        "predicted_intensity": analysis.intensity,
                        "predicted_support_need": analysis.support_need,
                        "predicted_strategy": analysis.next_action,
                        "predicted_risk_level": analysis.risk_level,
                        "confidence": analysis.confidence,
                        "schema_valid": analysis.schema_valid,
                        "analysis_status": analysis.analysis_status,
                        "emotion_scored": analysis.model_alias is not None,
                        "model_call": analysis.model_alias is not None,
                        "primary_rank": (
                            primary_rank(question.gold_primary_emotion, analysis.candidate_emotions)
                            if analysis.model_alias is not None
                            else None
                        ),
                        "model_alias": analysis.model_alias,
                        "prompt_version": analysis.prompt_version,
                        "classifier_version": analysis.classifier_version,
                        "cache_key": evaluation_cache_key(
                            question.id,
                            model_alias=args.model_alias,
                            evaluation_mode="structured_analysis",
                            prompt_version=analysis.prompt_version,
                            classifier_version=analysis.classifier_version,
                            gold_version=args.gold_version,
                        ),
                        "anchor_labels": analysis.anchor_labels,
                        "anchor_adjusted": analysis.anchor_adjusted,
                        "anchor_conflict": analysis.anchor_conflict,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
            except Exception as error:
                row = {
                    "id": question.id,
                    "category": question.category,
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            selected_results.append(row)
            print(f"[{index}/{len(selected)}] {question.id} {row['status']}", flush=True)
    summary = summarize(
        selected_results,
        requested=len(selected),
        emotion_only=args.emotion_only,
        gold_version=args.gold_version,
        prompt_version=expected_prompt_version,
        classifier_version=expected_classifier_version,
    )
    summary["sample_profile"] = (
        "daily-stratified-30"
        if args.daily_stratified_30
        else "daily-holdout-60"
        if args.daily_holdout_60
        else "full"
        if args.full
        else "smoke"
        if args.smoke
        else "default"
    )
    if args.compare_run:
        summary["compare"] = compare_results(
            selected_results, args.compare_run, gold_version=args.gold_version
        )
    write_report(args.output_dir, summary)
    return summary


def main() -> None:
    args = parse_args()
    if args.full and args.limit and args.limit != 0:
        args.limit = 0
    summary = run(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
