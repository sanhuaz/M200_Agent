from __future__ import annotations

from collections import Counter
from pathlib import Path

from scripts.companion_eval import (
    DAILY_HOLDOUT_60_QUOTAS,
    DAILY_STRATIFIED_30_QUOTAS,
    EMOTION_LABELS,
    EMOTION_ONLY_PROMPT_VERSION,
    EMOTION_ONLY_PROMPT_VERSION_V3,
    EMOTION_ONLY_SYSTEM_PROMPT,
    build_emotion_only_system_prompt,
    choose_questions,
    load_questions,
    summarize,
)


def test_emotion_only_prompt_reuses_shared_v2_guide() -> None:
    assert EMOTION_ONLY_PROMPT_VERSION == "companion-emotion-only-v2"
    assert "第一项必须是主情绪" in EMOTION_ONLY_SYSTEM_PROMPT
    assert "neutral 不得与其他标签并列" in EMOTION_ONLY_SYSTEM_PROMPT
    for label in EMOTION_LABELS:
        assert f"- {label}：" in EMOTION_ONLY_SYSTEM_PROMPT


def test_daily_stratified_30_selection_is_fixed_and_balanced() -> None:
    project_root = Path(__file__).resolve().parents[2]
    questions = load_questions(
        project_root / "workspace" / "evaluations" / "companion-p0" / "questions.json"
    )
    selected = choose_questions(questions, 12, False, False, daily_stratified_30=True)
    repeated = choose_questions(questions, 1, False, False, daily_stratified_30=True)

    assert len(selected) == 30
    assert [item.id for item in selected] == [item.id for item in repeated]
    assert all(item.gold_risk_level == "low" for item in selected)
    assert Counter(item.category for item in selected) == DAILY_STRATIFIED_30_QUOTAS


def test_summary_separates_strict_top1_top3_and_skips_safety() -> None:
    rows = [
        {
            "status": "ok",
            "schema_valid": True,
            "emotion_scored": True,
            "model_call": True,
            "gold_primary_emotion": "sadness",
            "gold_emotions": ["sadness"],
            "predicted_primary_emotion": "sadness",
            "predicted_emotions": ["sadness"],
            "gold_risk_level": "low",
            "predicted_risk_level": "low",
            "elapsed_ms": 10.0,
        },
        {
            "status": "ok",
            "schema_valid": True,
            "emotion_scored": True,
            "model_call": True,
            "gold_primary_emotion": "anxiety",
            "gold_emotions": ["anxiety"],
            "predicted_primary_emotion": "sadness",
            "predicted_emotions": ["sadness", "anxiety"],
            "gold_risk_level": "low",
            "predicted_risk_level": "low",
            "elapsed_ms": 20.0,
        },
        {
            "status": "ok",
            "schema_valid": True,
            "emotion_scored": False,
            "model_call": False,
            "gold_primary_emotion": "helplessness",
            "gold_emotions": ["helplessness"],
            "predicted_primary_emotion": "neutral",
            "predicted_emotions": ["neutral"],
            "gold_risk_level": "critical",
            "predicted_risk_level": "critical",
            "elapsed_ms": 0.1,
        },
    ]

    summary = summarize(rows, requested=3, emotion_only=True)

    assert summary["primary_emotion_accuracy"] == 0.5
    assert summary["primary_emotion_top3_hit_rate"] == 1.0
    assert summary["primary_rank_counts"] == {"rank1": 1, "rank2": 1, "rank3": 0, "miss": 0}
    assert summary["emotion_scored_count"] == 2
    assert summary["emotion_skipped_count"] == 1
    assert summary["model_call_count"] == 2
    assert summary["analysis_p50_ms"] == 10.0
    assert summary["high_risk_recall"] == 1.0


def test_summary_uses_null_when_no_emotion_denominator() -> None:
    summary = summarize(
        [
            {
                "status": "ok",
                "schema_valid": True,
                "emotion_scored": False,
                "model_call": False,
                "gold_primary_emotion": "fear",
                "gold_emotions": ["fear"],
                "predicted_primary_emotion": "neutral",
                "predicted_emotions": ["neutral"],
                "gold_risk_level": "critical",
                "predicted_risk_level": "critical",
                "elapsed_ms": 0.1,
            }
        ],
        requested=1,
        emotion_only=True,
    )

    assert summary["primary_emotion_accuracy"] is None
    assert summary["primary_emotion_top3_hit_rate"] is None
    assert summary["emotion"] == {"micro_f1": None, "macro_f1": None, "exact_match": None}
    assert summary["schema_success_rate"] == 1.0
    assert summary["analysis_p50_ms"] is None
    assert summary["high_risk_recall"] == 1.0


def test_v3_prompt_contains_contrastive_rules_and_anchor_hint_contract() -> None:
    assert sum(DAILY_HOLDOUT_60_QUOTAS.values()) == 60
    prompt = build_emotion_only_system_prompt(EMOTION_ONLY_PROMPT_VERSION_V3)
    assert "anxiety" in prompt
    assert "具体威胁" in prompt
    assert "confusion" in prompt
    assert "第一项必须是主情绪" in prompt
    assert EMOTION_ONLY_SYSTEM_PROMPT != prompt


def test_summary_exposes_diagnostics_without_breaking_legacy_emotion_metrics() -> None:
    rows = [
        {
            "status": "ok",
            "schema_valid": True,
            "emotion_scored": True,
            "model_call": True,
            "gold_primary_emotion": "sadness",
            "gold_emotions": ["sadness"],
            "predicted_emotions": ["anxiety", "sadness"],
            "gold_risk_level": "low",
            "predicted_risk_level": "low",
            "elapsed_ms": 10.0,
            "category": "人际关系",
        }
    ]
    summary = summarize(rows, requested=1, emotion_only=True, gold_version="companion-p0-gold-v2")
    assert summary["emotion"] == {"micro_f1": 0.6667, "macro_f1": 0.05, "exact_match": 0.0}
    assert summary["gold_version"] == "companion-p0-gold-v2"
    assert summary["emotion_details"]["top1_error_split"] == {"reorderable": 1, "gold_missing": 0}
    assert summary["emotion_details"]["average_candidate_count"] == 2.0
    assert summary["category_metrics"]["人际关系"]["top3"] == 1.0
