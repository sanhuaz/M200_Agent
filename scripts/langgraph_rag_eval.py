# ruff: noqa: E501
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.models import KnowledgeBase  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.chat import SYSTEM_PROMPT  # noqa: E402
from app.services.models import model_registry  # noqa: E402
from app.services.personas import persona_system_prompt  # noqa: E402
from app.workflows.agent import (  # noqa: E402
    build_agent_graph,
    close_checkpointer,
    final_ai_message,
    initialize_checkpointer,
    skill_descriptions,
)

Category = Literal["high_frequency", "long_tail", "ambiguous"]
SelectionOutcome = Literal["primary", "valid_overlap", "wrong", "no_tool"]
CATEGORY_LABELS: dict[Category, str] = {
    "high_frequency": "高频",
    "long_tail": "长尾",
    "ambiguous": "模糊",
}


class ChainJudgeResult(BaseModel):
    retrieval_relevance: int = Field(ge=0, le=4)
    retrieval_sufficiency: int = Field(ge=0, le=4)
    knowledge_base_selection: int = Field(ge=0, le=4)
    answer_correctness: int = Field(ge=0, le=4)
    answer_groundedness: int = Field(ge=0, le=4)
    answer_completeness: int = Field(ge=0, le=4)
    citation_quality: int = Field(ge=0, le=4)
    ambiguity_handling: int = Field(ge=0, le=4)
    end_to_end_score: int = Field(ge=0, le=4)
    selection_outcome: SelectionOutcome
    passed: bool
    critical_error: str = ""
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PersonalAgent 真实 LangGraph 知识库选路评测")
    parser.add_argument(
        "--questions",
        type=Path,
        default=PROJECT_ROOT / "workspace" / "evaluations" / "ai-study-150" / "questions.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "workspace" / "evaluations" / "langgraph-ai-study-150",
    )
    parser.add_argument("--model-alias", default="default")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fresh", action="store_true", help="忽略已有成功结果并重新评测")
    parser.add_argument("--rerun-ids", nargs="*", default=[], help="重新评测指定题号并替换旧结果")
    return parser.parse_args()


def configured_model(alias: str):
    model = model_registry.chat_model(alias).model_copy(update={"streaming": False, "temperature": 0})
    profile = model_registry.profile(alias)
    if "api.deepseek.com" in profile.base_url:
        model = model.model_copy(update={"extra_body": {"thinking": {"type": "disabled"}}})
    return model


def invoke_with_retry(callable_, *, attempts: int = 4):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return callable_()
        except Exception as error:
            last_error = error
            if attempt == attempts:
                break
            time.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise last_error


def build_system_prompt(knowledge_bases: list[KnowledgeBase], session, requester_id: str) -> str:
    knowledge_text = "\n".join(f"- {item.name}: {item.id}" for item in knowledge_bases) or "- 无"
    return (
        f"{SYSTEM_PROMPT}\n\n当前用户画像和长期记忆：\n- 无"
        f"\n\n可用知识库：\n{knowledge_text}"
        f"\n\n可按需加载的 Skill（只提供名称和描述）：\n{skill_descriptions(session)}"
        "\n\n历史摘要：\n无"
        f"\n\n{persona_system_prompt(None)}"
    )


def parse_tool_payload(content: object) -> dict[str, object]:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return {}
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def collect_chain_trace(
    messages: list[object], knowledge_bases: list[KnowledgeBase]
) -> tuple[list[dict[str, object]], str]:
    by_id = {item.id: item for item in knowledge_bases}
    first = knowledge_bases[0] if knowledge_bases else None
    search_calls: dict[str, dict[str, object]] = {}
    trace: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            if call.get("name") != "search_knowledge":
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            requested_id = str(args.get("knowledge_base_id") or "")
            effective = by_id.get(requested_id) if requested_id else first
            item = {
                "tool_call_id": str(call.get("id") or ""),
                "query": str(args.get("query") or ""),
                "requested_knowledge_base_id": requested_id,
                "knowledge_base_id": effective.id if effective else "",
                "knowledge_base_name": effective.name if effective else "",
                "evidence": [],
                "reranker": "not_run",
                "status": "unknown",
            }
            search_calls[item["tool_call_id"]] = item
            trace.append(item)

    evidence_blocks: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name != "search_knowledge":
            continue
        call = search_calls.get(str(message.tool_call_id))
        if call is None:
            continue
        payload = parse_tool_payload(message.content)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        retrieval = data.get("retrieval") if isinstance(data.get("retrieval"), dict) else {}
        evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
        if data.get("duplicate"):
            call["status"] = "duplicate_blocked"
        elif data.get("limit_reached"):
            call["status"] = "limit_blocked"
        else:
            call["status"] = "executed"
        call["reranker"] = str(retrieval.get("reranker") or "unknown")
        call["evidence"] = evidence
        for index, hit in enumerate(evidence, start=1):
            if not isinstance(hit, dict):
                continue
            heading = " > ".join(str(x) for x in hit.get("heading_path", [])) or "无标题"
            location = f"第 {hit['page_number']} 页" if hit.get("page_number") else "位置未知"
            evidence_blocks.append(
                f"[调用库：{call['knowledge_base_name']}；证据 {index}] 文件：{hit.get('filename', '')}；"
                f"标题：{heading}；{location}\n{hit.get('content', '')}"
            )
    return trace, "\n\n".join(evidence_blocks)


def judge_chain(
    question: dict[str, object],
    trace: list[dict[str, object]],
    evidence_text: str,
    answer: str,
    model_alias: str,
) -> ChainJudgeResult:
    rubric = """你是严格的多知识库 LangGraph RAG 裁判。只根据输入中的参考答案、金标源片段、真实工具轨迹、实际检索证据和最终回答评分，不补充外部知识。

重要判分原则：
1. 金标题目最初由 AI学习 库生成，但 AI学习 不是唯一合法目标库。
2. ALLinRAG 与 AI学习 可能有重合知识。若模型选择 ALLinRAG，且其实际证据相关、充分并能支撑正确答案，必须判为 valid_overlap，不得因为库名不同判错。
3. 只有所选库的实际证据无关或不足，并导致链路无法可靠回答时，才判为 wrong。选择 AI学习 且证据有效记为 primary。
4. 题目均来自知识库文档；完全未调用 search_knowledge 记为 no_tool，不能通过。
5. 不要把流畅度当正确性或忠实性；引用文件名、标题、页码或位置且能对应证据，才算有效引用。

所有分项 0-4：0 完全失败，1 严重不足，2 部分满足，3 基本正确，4 完整准确。
- retrieval_relevance：实际检索证据与问题的相关性。
- retrieval_sufficiency：实际证据是否足以支持参考答案。
- knowledge_base_selection：所选库是否提供了适用证据；有效重合命中可得 4。
- answer_correctness、answer_groundedness、answer_completeness：最终答案的正确性、证据忠实性、完整性。
- citation_quality：是否写明文件、标题及页码或位置，且引用确实支撑结论。
- ambiguity_handling：模糊题是否谨慎处理歧义；非模糊题正常回答可得 4。
- end_to_end_score：从选库、检索到回答的最终可用性。

passed 仅在调用了 search_knowledge，retrieval_sufficiency、knowledge_base_selection、answer_correctness、answer_groundedness 均不少于 3，citation_quality 不少于 2，且模糊题 ambiguity_handling 不少于 3 时为 true。critical_error 只写最关键问题，没有则留空。reason 用一句中文说明。"""
    payload = {
        "category": question["category"],
        "question": question["question"],
        "ambiguity_note": question["ambiguity_note"],
        "reference_answer": question["reference_answer"],
        "gold_source_filename": question["source_filename"],
        "gold_source_excerpt": question["source_excerpt"],
        "tool_trace": trace,
        "retrieved_evidence": evidence_text,
        "final_answer": answer,
    }
    structured = configured_model(model_alias).with_structured_output(
        ChainJudgeResult, method="function_calling"
    )
    raw = invoke_with_retry(
        lambda: structured.invoke(
            [("system", rubric), ("human", json.dumps(payload, ensure_ascii=False))]
        )
    )
    return ChainJudgeResult.model_validate(raw)


async def evaluate_one(
    question: dict[str, object],
    model_alias: str,
    knowledge_bases: list[KnowledgeBase],
    run_id: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, object]:
    async with semaphore:
        started = time.perf_counter()
        requester_id = "langgraph-rag-eval"
        conversation_id = f"eval-{run_id}-{question['id']}"
        with SessionLocal() as session:
            system = build_system_prompt(knowledge_bases, session, requester_id)
            graph = build_agent_graph(session, model_alias, requester_id, conversation_id)
            config: RunnableConfig = {
                "configurable": {"thread_id": f"langgraph-eval:{run_id}:{question['id']}"}
            }
            chain_started = time.perf_counter()
            state = await graph.ainvoke(
                {
                    "messages": [
                        SystemMessage(content=system),
                        HumanMessage(content=str(question["question"])),
                    ]
                },
                config=config,
            )
            chain_ms = round((time.perf_counter() - chain_started) * 1000, 1)
            messages = list(state["messages"])
            final = final_ai_message(messages)
            answer = final.content if isinstance(final.content, str) else json.dumps(final.content, ensure_ascii=False)
            trace, evidence_text = collect_chain_trace(messages, knowledge_bases)

        judge_started = time.perf_counter()
        judge = await asyncio.to_thread(
            judge_chain, question, trace, evidence_text, answer, model_alias
        )
        judge_ms = round((time.perf_counter() - judge_started) * 1000, 1)
        return {
            **question,
            "chain": {
                "latency_ms": chain_ms,
                "search_call_count": len(trace),
                "executed_search_count": sum(item["status"] == "executed" for item in trace),
                "blocked_search_count": sum(item["status"] != "executed" for item in trace),
                "selected_knowledge_bases": [str(item["knowledge_base_name"]) for item in trace],
                "tool_trace": trace,
                "evidence_text": evidence_text,
                "answer": answer,
            },
            "judge": judge.model_dump(),
            "judge_latency_ms": judge_ms,
            "total_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }


async def evaluate_safely(
    question: dict[str, object],
    model_alias: str,
    knowledge_bases: list[KnowledgeBase],
    run_id: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, object]:
    try:
        return await evaluate_one(question, model_alias, knowledge_bases, run_id, semaphore)
    except Exception as error:
        return {**question, "error": f"{type(error).__name__}: {error}"}


def summarize(results: list[dict[str, object]]) -> dict[str, object]:
    for item in results:
        chain = item.get("chain", {})
        judge = item.get("judge", {})
        if (
            isinstance(chain, dict)
            and isinstance(judge, dict)
            and chain.get("search_call_count", 0)
            and judge.get("selection_outcome") == "no_tool"
        ):
            judge["selection_outcome"] = "wrong"
            judge["passed"] = False
            judge["reason"] = f"已调用检索但未取得有效证据，归类为选路失败。{judge.get('reason', '')}"
    successful = [item for item in results if "error" not in item]
    score_keys = [
        "retrieval_relevance",
        "retrieval_sufficiency",
        "knowledge_base_selection",
        "answer_correctness",
        "answer_groundedness",
        "answer_completeness",
        "citation_quality",
        "ambiguity_handling",
        "end_to_end_score",
    ]

    def group(items: list[dict[str, object]]) -> dict[str, object]:
        if not items:
            return {}
        judges = [item["judge"] for item in items]
        return {
            "count": len(items),
            "tool_invocation_rate": round(
                sum(item["chain"]["search_call_count"] > 0 for item in items) / len(items), 4
            ),
            "search_calls": {
                "requested_mean": round(
                    statistics.mean(item["chain"]["search_call_count"] for item in items), 3
                ),
                "executed_mean": round(
                    statistics.mean(
                        item["chain"].get("executed_search_count", item["chain"]["search_call_count"])
                        for item in items
                    ),
                    3,
                ),
                "executed_max": max(
                    item["chain"].get("executed_search_count", item["chain"]["search_call_count"])
                    for item in items
                ),
            },
            "selection_outcomes": dict(Counter(judge["selection_outcome"] for judge in judges)),
            "selection_distribution": dict(
                Counter(
                    name
                    for item in items
                    for name in set(item["chain"]["selected_knowledge_bases"])
                )
            ),
            "scores": {
                key: round(statistics.mean(float(judge[key]) for judge in judges), 3)
                for key in score_keys
            },
            "pass_rate": round(sum(bool(judge["passed"]) for judge in judges) / len(items), 4),
            "wrong_selection_rate": round(
                sum(judge["selection_outcome"] == "wrong" for judge in judges) / len(items), 4
            ),
            "valid_overlap_rate": round(
                sum(judge["selection_outcome"] == "valid_overlap" for judge in judges) / len(items), 4
            ),
            "no_tool_rate": round(
                sum(judge["selection_outcome"] == "no_tool" for judge in judges) / len(items), 4
            ),
            "latency_ms": {
                "chain_mean": round(statistics.mean(item["chain"]["latency_ms"] for item in items), 1),
                "total_mean": round(statistics.mean(item["total_latency_ms"] for item in items), 1),
            },
        }

    guarded = [
        item
        for item in successful
        if "executed_search_count" in item.get("chain", {})
    ]
    return {
        "total": len(results),
        "successful": len(successful),
        "errors": len(results) - len(successful),
        "overall": group(successful),
        "by_category": {
            category: group([item for item in successful if item["category"] == category])
            for category in CATEGORY_LABELS
        },
        "guarded_subset": {
            "count": len(guarded),
            "executed_search_mean": (
                round(
                    statistics.mean(item["chain"]["executed_search_count"] for item in guarded),
                    3,
                )
                if guarded
                else 0.0
            ),
            "executed_search_max": (
                max(item["chain"]["executed_search_count"] for item in guarded) if guarded else 0
            ),
            "limit": 4,
        },
    }


def write_outputs(
    output_dir: Path,
    results: list[dict[str, object]],
    summary: dict[str, object],
    metadata: dict[str, object],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results), encoding="utf-8"
    )
    (output_dir / "summary.json").write_text(
        json.dumps({"metadata": metadata, "metrics": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    fields = [
        "id", "category", "question", "source_filename", "selected_knowledge_bases",
        "search_call_count", "executed_search_count", "blocked_search_count", "answer",
        "selection_outcome", "retrieval_relevance",
        "retrieval_sufficiency", "knowledge_base_selection", "answer_correctness",
        "answer_groundedness", "answer_completeness", "citation_quality",
        "ambiguity_handling", "end_to_end_score", "passed", "critical_error", "reason",
        "chain_latency_ms", "total_latency_ms", "error",
    ]
    with (output_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            chain = item.get("chain", {})
            judge = item.get("judge", {})
            writer.writerow({
                "id": item.get("id"), "category": item.get("category"),
                "question": item.get("question"), "source_filename": item.get("source_filename"),
                "selected_knowledge_bases": " | ".join(chain.get("selected_knowledge_bases", [])),
                "search_call_count": chain.get("search_call_count"), "answer": chain.get("answer"),
                "executed_search_count": chain.get("executed_search_count"),
                "blocked_search_count": chain.get("blocked_search_count"),
                "selection_outcome": judge.get("selection_outcome"),
                "retrieval_relevance": judge.get("retrieval_relevance"),
                "retrieval_sufficiency": judge.get("retrieval_sufficiency"),
                "knowledge_base_selection": judge.get("knowledge_base_selection"),
                "answer_correctness": judge.get("answer_correctness"),
                "answer_groundedness": judge.get("answer_groundedness"),
                "answer_completeness": judge.get("answer_completeness"),
                "citation_quality": judge.get("citation_quality"),
                "ambiguity_handling": judge.get("ambiguity_handling"),
                "end_to_end_score": judge.get("end_to_end_score"), "passed": judge.get("passed"),
                "critical_error": judge.get("critical_error"), "reason": judge.get("reason"),
                "chain_latency_ms": chain.get("latency_ms"),
                "total_latency_ms": item.get("total_latency_ms"), "error": item.get("error"),
            })

    overall = summary.get("overall", {})
    scores = overall.get("scores", {})
    lines = [
        "# 150题真实 LangGraph 多知识库链路评测", "", "## 口径", "",
        "- 链路：问题 → LangGraph Agent → 模型自主决定是否调用 search_knowledge 及选择知识库 → 真实混合检索 → 最终回答 → LLM Judge。",
        "- AI学习是题目来源库，但不是唯一合法命中；ALLinRAG 的重合证据若相关、充分，记为有效重合命中。",
        "- wrong 只表示所选库的实际证据无关或不足；不会按知识库名称机械判错。",
        "- 停止/去重策略在异常复测4题和后续17题中启用，共21题；此前正常题保留原基线结果。",
        "- 裁判使用当前模型自评，仍建议对 wrong、valid_overlap 和低分样本做独立 Judge 或人工抽检。", "",
        "## 总体结果", "",
        f"- 完成：{summary.get('successful', 0)}/{summary.get('total', 0)}；错误：{summary.get('errors', 0)}。",
    ]
    if overall:
        guarded = summary["guarded_subset"]
        lines.extend([
            f"- 知识检索工具调用率：{overall['tool_invocation_rate']:.2%}；无工具率：{overall['no_tool_rate']:.2%}。",
            f"- 平均检索请求/实际执行：{overall['search_calls']['requested_mean']:.3f} / {overall['search_calls']['executed_mean']:.3f}；单题实际执行最大值：{overall['search_calls']['executed_max']}。",
            f"- 证据判定选库错误率：{overall['wrong_selection_rate']:.2%}；ALLinRAG 有效重合命中率：{overall['valid_overlap_rate']:.2%}。",
            f"- 选库分布：{json.dumps(overall['selection_distribution'], ensure_ascii=False)}。",
            f"- 检索相关性/充分性/选库合理性：{scores['retrieval_relevance']:.3f} / {scores['retrieval_sufficiency']:.3f} / {scores['knowledge_base_selection']:.3f}（满分4）。",
            f"- 回答正确性/忠实性/完整性/引用：{scores['answer_correctness']:.3f} / {scores['answer_groundedness']:.3f} / {scores['answer_completeness']:.3f} / {scores['citation_quality']:.3f}。",
            f"- 端到端平均分：{scores['end_to_end_score']:.3f}/4；通过率：{overall['pass_rate']:.2%}。",
            f"- LangGraph 链路平均延迟：{overall['latency_ms']['chain_mean']:.0f} ms；含 Judge 平均延迟：{overall['latency_ms']['total_mean']:.0f} ms。",
            f"- 新停止策略子集：{guarded['count']}题，实际检索均值/最大值：{guarded['executed_search_mean']:.3f} / {guarded['executed_search_max']}（上限 {guarded['limit']}）。",
        ])
    lines.extend(["", "## 分类结果", "", "| 类型 | 数量 | 工具调用率 | 错误选库率 | 有效重合率 | 正确性 | 端到端 | 通过率 |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for category, label in CATEGORY_LABELS.items():
        item = summary.get("by_category", {}).get(category, {})
        if item:
            lines.append(
                f"| {label} | {item['count']} | {item['tool_invocation_rate']:.2%} | {item['wrong_selection_rate']:.2%} | "
                f"{item['valid_overlap_rate']:.2%} | {item['scores']['answer_correctness']:.3f} | "
                f"{item['scores']['end_to_end_score']:.3f} | {item['pass_rate']:.2%} |"
            )
    failures = sorted(
        (item for item in results if "judge" in item and not item["judge"]["passed"]),
        key=lambda item: (item["judge"]["end_to_end_score"], item["id"]),
    )[:20]
    lines.extend(["", "## 最差样本（最多20条）", ""])
    lines.extend(
        f"- `{item['id']}` [{item['judge']['selection_outcome']}] {item['question']} — {item['judge']['reason']}"
        for item in failures
    )
    if not failures:
        lines.append("无。")
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def async_main() -> int:
    args = parse_args()
    all_questions = json.loads(args.questions.resolve().read_text(encoding="utf-8"))
    questions = all_questions
    rerun_ids = set(args.rerun_ids)
    if rerun_ids:
        questions = [item for item in all_questions if str(item["id"]) in rerun_ids]
        found_ids = {str(item["id"]) for item in questions}
        if missing := rerun_ids - found_ids:
            raise RuntimeError(f"题号不存在：{', '.join(sorted(missing))}")
    if args.limit > 0:
        questions = questions[: args.limit]
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "results.jsonl"
    completed: dict[str, dict[str, object]] = {}
    if checkpoint_path.exists() and not args.fresh:
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if "error" not in item:
                    completed[str(item["id"])] = item
    for question_id in rerun_ids:
        completed.pop(question_id, None)

    with SessionLocal() as session:
        knowledge_bases = list(session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at)))
    if len(knowledge_bases) < 2:
        raise RuntimeError(f"多知识库选路评测至少需要2个知识库，实际为 {len(knowledge_bases)}")
    pending = [item for item in questions if str(item["id"]) not in completed]
    run_id = uuid.uuid4().hex[:12]
    print(
        f"[langgraph-eval] questions={len(questions)} cached={len(completed)} pending={len(pending)} "
        f"workers={args.workers} knowledge_bases={[item.name for item in knowledge_bases]}",
        flush=True,
    )
    await initialize_checkpointer()
    semaphore = asyncio.Semaphore(max(1, args.workers))
    tasks = [
        asyncio.create_task(
            evaluate_safely(item, args.model_alias, knowledge_bases, run_id, semaphore)
        )
        for item in pending
    ]
    try:
        with checkpoint_path.open("a", encoding="utf-8") as checkpoint:
            finished = len(completed)
            for task in asyncio.as_completed(tasks):
                result = await task
                completed[str(result["id"])] = result
                checkpoint.write(json.dumps(result, ensure_ascii=False) + "\n")
                checkpoint.flush()
                finished += 1
                status = "ok" if "error" not in result else "error"
                chain = result.get("chain", {})
                selected = ",".join(chain.get("selected_knowledge_bases", [])) or "none"
                executed = chain.get("executed_search_count", "-")
                print(
                    f"[eval {finished:03d}] {result['id']} {status} "
                    f"executed={executed} selected={selected}",
                    flush=True,
                )
    finally:
        await close_checkpointer()

    ordered = [
        completed[str(question["id"])]
        for question in all_questions
        if str(question["id"]) in completed
    ]
    summary = summarize(ordered)
    profile = model_registry.profile(args.model_alias)
    metadata = {
        "model_alias": args.model_alias, "model": profile.model,
        "knowledge_bases": [{"id": item.id, "name": item.name} for item in knowledge_bases],
        "question_count": len(ordered), "workers": args.workers,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "judging_policy": "evidence_based_overlap_aware",
    }
    write_outputs(output_dir, ordered, summary, metadata)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"[done] {output_dir}", flush=True)
    target_ids = {str(question["id"]) for question in questions}
    return 0 if summary["errors"] == 0 and target_ids.issubset(completed) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
