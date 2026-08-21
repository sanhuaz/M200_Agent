# ruff: noqa: E501
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.models import Chunk, Document, KnowledgeBase  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services.models import model_registry  # noqa: E402
from app.services.retrieval import HybridRetriever  # noqa: E402
from sqlalchemy import select  # noqa: E402

Category = Literal["high_frequency", "long_tail", "ambiguous"]
CATEGORY_LABELS: dict[Category, str] = {
    "high_frequency": "高频",
    "long_tail": "长尾",
    "ambiguous": "模糊",
}


class GeneratedQuestion(BaseModel):
    category: Category
    question: str = Field(min_length=4)
    reference_answer: str = Field(min_length=4)
    source_id: str
    gold_keywords: list[str] = Field(min_length=1, max_length=8)
    ambiguity_note: str = ""


class QuestionBatch(BaseModel):
    items: list[GeneratedQuestion]


class JudgeResult(BaseModel):
    retrieval_relevance: int = Field(ge=0, le=4)
    retrieval_sufficiency: int = Field(ge=0, le=4)
    answer_correctness: int = Field(ge=0, le=4)
    answer_groundedness: int = Field(ge=0, le=4)
    answer_completeness: int = Field(ge=0, le=4)
    citation_quality: int = Field(ge=0, le=4)
    ambiguity_handling: int = Field(ge=0, le=4)
    end_to_end_score: int = Field(ge=0, le=4)
    passed: bool
    critical_error: str = ""
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PersonalAgent 真实 RAG 评测")
    parser.add_argument("--knowledge-base", default="AI学习")
    parser.add_argument("--model-alias", default="default")
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / "workspace" / "evaluations" / "ai-study-150"
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="仅评测前 N 题，0 表示全部")
    parser.add_argument("--regenerate", action="store_true")
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
        except Exception as error:  # external providers expose several exception classes
            last_error = error
            if attempt == attempts:
                break
            time.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise last_error


def generate_question_batch(
    structured_model,
    prompt: str,
    expected_counts: Counter[str],
    required: int,
    source_ids: set[str],
) -> QuestionBatch:
    def call_model() -> QuestionBatch:
        raw = structured_model.invoke(
            [
                ("system", "你是严格的数据集设计专家，只输出符合结构的可核验题目。"),
                ("human", prompt),
            ]
        )
        batch = QuestionBatch.model_validate(raw)
        if any(item.source_id not in source_ids for item in batch.items):
            raise ValueError("模型返回了不存在的 source_id")
        normalized = ["".join(item.question.split()).lower() for item in batch.items]
        if len(normalized) != len(set(normalized)):
            raise ValueError("同一文档内存在重复题目")
        grouped = {
            category: [item for item in batch.items if item.category == category]
            for category in CATEGORY_LABELS
        }
        missing = {
            category: expected_counts[category] - len(items)
            for category, items in grouped.items()
            if len(items) < expected_counts[category]
        }
        if missing:
            raise ValueError(f"题型数量不足：{missing}")
        selected = [
            item
            for category, items in grouped.items()
            for item in items[: expected_counts[category]]
        ]
        if len(selected) != required:
            raise ValueError(f"裁剪后题目数量错误：{len(selected)} != {required}")
        return QuestionBatch(items=selected)

    return invoke_with_retry(call_model)


def allocate(total: int, count: int, offset: int = 0) -> list[int]:
    base, remainder = divmod(total, count)
    values = [base] * count
    for index in range(remainder):
        values[(index + offset) % count] += 1
    return values


def evenly_select(rows: list[Chunk], count: int) -> list[Chunk]:
    usable = [row for row in rows if len(row.content.strip()) >= 120]
    if len(usable) <= count:
        return usable
    if count == 1:
        return [usable[len(usable) // 2]]
    indices = {round(index * (len(usable) - 1) / (count - 1)) for index in range(count)}
    return [usable[index] for index in sorted(indices)]


def question_plan(document_count: int) -> list[dict[Category, int]]:
    high = allocate(60, document_count)
    long_tail = allocate(60, document_count, offset=5)
    ambiguous = allocate(30, document_count, offset=9)
    return [
        {
            "high_frequency": high[index],
            "long_tail": long_tail[index],
            "ambiguous": ambiguous[index],
        }
        for index in range(document_count)
    ]


def generate_questions(
    output_dir: Path, knowledge_base_name: str, model_alias: str, regenerate: bool
) -> list[dict[str, object]]:
    questions_path = output_dir / "questions.json"
    if questions_path.exists() and not regenerate:
        return json.loads(questions_path.read_text(encoding="utf-8"))

    output_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = output_dir / "question_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as session:
        knowledge_base = session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.name == knowledge_base_name)
        )
        if knowledge_base is None:
            raise RuntimeError(f"知识库不存在：{knowledge_base_name}")
        documents = list(
            session.scalars(
                select(Document)
                .where(Document.knowledge_base_id == knowledge_base.id, Document.status == "ready")
                .order_by(Document.created_at)
            )
        )
        chunks_by_document = {
            document.id: list(
                session.scalars(
                    select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.position)
                )
            )
            for document in documents
        }

    if len(documents) != 13:
        raise RuntimeError(f"预期 AI学习 库有 13 份 ready 文档，实际为 {len(documents)}")

    plans = question_plan(len(documents))
    generated: list[dict[str, object]] = []
    for document_index, (document, plan) in enumerate(zip(documents, plans, strict=True), start=1):
        part_path = parts_dir / f"{document_index:02d}.json"
        if part_path.exists() and not regenerate:
            part = json.loads(part_path.read_text(encoding="utf-8"))
            generated.extend(part)
            print(f"[questions {document_index:02d}/13] cached {document.filename}", flush=True)
            continue

        required = sum(plan.values())
        candidates = evenly_select(chunks_by_document[document.id], min(required * 2, 28))
        source_map = {chunk.id: chunk for chunk in candidates}
        sources = []
        for chunk in candidates:
            heading = " > ".join(json.loads(chunk.heading_path)) or "无标题"
            sources.append(
                f'<source id="{chunk.id}" position="{chunk.position}" heading="{heading}">\n'
                f"{chunk.content[:1100]}\n</source>"
            )
        prompt = f"""根据下面来自《{document.filename}》的真实片段，生成严格可追溯的中文 RAG 测试题。

数量必须精确为：高频题 {plan["high_frequency"]} 道，长尾题 {plan["long_tail"]} 道，模糊题 {plan["ambiguous"]} 道。

定义：
- high_frequency：该章最核心、真实用户最常问的概念或机制。
- long_tail：具体实现细节、条件、例外、数字、步骤或容易遗漏的区别。
- ambiguous：刻意省略一个限定词、使用简称或存在近义概念；仍应能根据片段给出谨慎回答，必要时指出假设。

要求：
1. 每题只绑定一个 source_id，问题和 reference_answer 必须完全由对应 source 支持。
2. 问题必须自然、互不重复，不能出现“根据材料”“本段”“上述内容”等测试提示语。
3. reference_answer 为 1-3 句可核验答案，不扩展片段外知识。
4. gold_keywords 给出 2-6 个判定答案的关键词。
5. 模糊题填写 ambiguity_note，说明歧义点及合格回答应如何处理；其他题留空。
6. 不得虚构 source_id。

真实片段：
{chr(10).join(sources)}"""

        structured = configured_model(model_alias).with_structured_output(
            QuestionBatch, method="function_calling"
        )
        expected_counts = Counter(plan)

        batch = generate_question_batch(
            structured,
            prompt,
            expected_counts,
            required,
            set(source_map),
        )
        part: list[dict[str, object]] = []
        for item in batch.items:
            chunk = source_map[item.source_id]
            part.append(
                {
                    **item.model_dump(),
                    "source_document_id": document.id,
                    "source_filename": document.filename,
                    "source_chunk_id": chunk.id,
                    "source_position": chunk.position,
                    "source_heading": json.loads(chunk.heading_path),
                    "source_excerpt": chunk.content,
                }
            )
        part_path.write_text(json.dumps(part, ensure_ascii=False, indent=2), encoding="utf-8")
        generated.extend(part)
        print(f"[questions {document_index:02d}/13] generated {document.filename}: {required}", flush=True)

    category_order: dict[Category, int] = {"high_frequency": 0, "long_tail": 1, "ambiguous": 2}
    generated.sort(
        key=lambda item: (category_order[item["category"]], item["source_filename"], item["question"])
    )
    counters: defaultdict[str, int] = defaultdict(int)
    prefixes = {"high_frequency": "HF", "long_tail": "LT", "ambiguous": "AM"}
    for item in generated:
        category = str(item["category"])
        counters[category] += 1
        item["id"] = f"{prefixes[category]}{counters[category]:03d}"

    if len(generated) != 150 or counters != {"high_frequency": 60, "long_tail": 60, "ambiguous": 30}:
        raise RuntimeError(f"最终题集分布错误：total={len(generated)}, categories={dict(counters)}")
    normalized_all = ["".join(str(item["question"]).split()).lower() for item in generated]
    if len(normalized_all) != len(set(normalized_all)):
        raise RuntimeError("最终题集存在跨文档重复题目")
    questions_path.write_text(json.dumps(generated, ensure_ascii=False, indent=2), encoding="utf-8")
    return generated


def format_evidence(hits) -> str:
    blocks: list[str] = []
    for index, hit in enumerate(hits, start=1):
        heading = " > ".join(hit.heading_path) or "无标题"
        location = f"第 {hit.page_number} 页" if hit.page_number else "位置未知"
        blocks.append(f"[E{index}] 文件：{hit.filename}；标题：{heading}；{location}\n{hit.content}")
    return "\n\n".join(blocks)


def answer_question(question: dict[str, object], evidence_text: str, model_alias: str) -> str:
    response = invoke_with_retry(
        lambda: configured_model(model_alias).invoke(
            [
                (
                    "system",
                    (
                        "你是知识库问答助手。只能依据给定证据回答；证据不足时明确说明。"
                        "每个关键结论必须引用对应的 [E数字]，不得使用证据外知识。"
                    ),
                ),
                (
                    "human",
                    f"问题：{question['question']}\n\n证据：\n{evidence_text}\n\n请给出准确、简洁的中文回答。",
                ),
            ]
        )
    )
    if isinstance(response.content, str):
        return response.content
    return json.dumps(response.content, ensure_ascii=False)


def judge_question(
    question: dict[str, object], evidence_text: str, answer: str, model_alias: str
) -> JudgeResult:
    rubric = """你是严格的 RAG 评测裁判。只根据参考答案、源片段、检索证据和待评答案打分，不补充外部知识。

所有分项使用 0-4 分：0 完全失败，1 严重不足，2 部分满足，3 基本正确，4 完整准确。
- retrieval_relevance：Top5 证据与问题的相关程度。
- retrieval_sufficiency：Top5 是否足够支持参考答案。
- answer_correctness：答案相对参考答案是否正确。
- answer_groundedness：答案是否都能由检索证据支持，不能把流畅度当忠实性。
- answer_completeness：是否覆盖问题要求的关键点。
- citation_quality：引用 [E数字] 是否存在且确实支撑对应结论。
- ambiguity_handling：模糊题是否指出合理假设、澄清歧义或给出有条件答案；非模糊题正常回答可给 4。
- end_to_end_score：综合从检索到回答的最终可用性。

passed 仅在 retrieval_sufficiency、answer_correctness、answer_groundedness 均不低于 3，citation_quality 不低于 2，且模糊题 ambiguity_handling 不低于 3 时为 true。critical_error 只写最关键的问题，没有则留空。reason 用一句中文说明。"""
    payload = {
        "category": question["category"],
        "question": question["question"],
        "ambiguity_note": question["ambiguity_note"],
        "reference_answer": question["reference_answer"],
        "gold_source_excerpt": question["source_excerpt"],
        "retrieved_evidence": evidence_text,
        "answer": answer,
    }
    structured = configured_model(model_alias).with_structured_output(JudgeResult, method="function_calling")
    raw = invoke_with_retry(
        lambda: structured.invoke([("system", rubric), ("human", json.dumps(payload, ensure_ascii=False))])
    )
    return JudgeResult.model_validate(raw)


def evaluate_one(question: dict[str, object], knowledge_base_id: str, model_alias: str) -> dict[str, object]:
    started = time.perf_counter()
    with SessionLocal() as session:
        retrieval_started = time.perf_counter()
        retriever = HybridRetriever(session)
        hits = invoke_with_retry(
            lambda: retriever.search(knowledge_base_id, str(question["question"]), recall=20, top_n=5)
        )
        retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000, 1)

    evidence_text = format_evidence(hits)
    generation_started = time.perf_counter()
    answer = answer_question(question, evidence_text, model_alias)
    generation_ms = round((time.perf_counter() - generation_started) * 1000, 1)
    judge_started = time.perf_counter()
    judge = judge_question(question, evidence_text, answer, model_alias)
    judge_ms = round((time.perf_counter() - judge_started) * 1000, 1)

    filenames = [hit.filename for hit in hits]
    chunk_ids = [hit.chunk_id for hit in hits]

    def first_rank(values: list[str], expected: str) -> int | None:
        try:
            return values.index(expected) + 1
        except ValueError:
            return None

    document_rank = first_rank(filenames, str(question["source_filename"]))
    chunk_rank = first_rank(chunk_ids, str(question["source_chunk_id"]))
    return {
        **question,
        "retrieval": {
            "latency_ms": retrieval_ms,
            "reranker_status": retriever.reranker_status,
            "document_rank": document_rank,
            "chunk_rank": chunk_rank,
            "hits": [asdict(hit) for hit in hits],
        },
        "generation": {"latency_ms": generation_ms, "answer": answer},
        "judge": judge.model_dump(),
        "judge_latency_ms": judge_ms,
        "total_latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percent * len(ordered)) - 1))
    return round(ordered[index], 2)


def metric_summary(results: list[dict[str, object]]) -> dict[str, object]:
    successful = [item for item in results if "error" not in item]
    score_keys = [
        "retrieval_relevance",
        "retrieval_sufficiency",
        "answer_correctness",
        "answer_groundedness",
        "answer_completeness",
        "citation_quality",
        "ambiguity_handling",
        "end_to_end_score",
    ]

    def summarize_group(items: list[dict[str, object]]) -> dict[str, object]:
        if not items:
            return {}
        document_ranks = [item["retrieval"]["document_rank"] for item in items]
        chunk_ranks = [item["retrieval"]["chunk_rank"] for item in items]
        judges = [item["judge"] for item in items]
        return {
            "count": len(items),
            "document_hit_at_1": round(sum(rank == 1 for rank in document_ranks) / len(items), 4),
            "document_hit_at_3": round(
                sum(rank is not None and rank <= 3 for rank in document_ranks) / len(items), 4
            ),
            "document_hit_at_5": round(
                sum(rank is not None and rank <= 5 for rank in document_ranks) / len(items), 4
            ),
            "document_mrr_at_5": round(
                sum(1 / rank if rank else 0 for rank in document_ranks) / len(items), 4
            ),
            "chunk_hit_at_1": round(sum(rank == 1 for rank in chunk_ranks) / len(items), 4),
            "chunk_hit_at_3": round(
                sum(rank is not None and rank <= 3 for rank in chunk_ranks) / len(items), 4
            ),
            "chunk_hit_at_5": round(
                sum(rank is not None and rank <= 5 for rank in chunk_ranks) / len(items), 4
            ),
            "llm_scores": {
                key: round(statistics.mean(float(judge[key]) for judge in judges), 3) for key in score_keys
            },
            "llm_pass_rate": round(sum(bool(judge["passed"]) for judge in judges) / len(items), 4),
            "hallucination_rate": round(
                sum(int(judge["answer_groundedness"]) < 3 for judge in judges) / len(items), 4
            ),
            "reranker_success_rate": round(
                sum(item["retrieval"]["reranker_status"] == "succeeded" for item in items) / len(items), 4
            ),
            "latency_ms": {
                "retrieval_p50": percentile([item["retrieval"]["latency_ms"] for item in items], 0.5),
                "retrieval_p95": percentile([item["retrieval"]["latency_ms"] for item in items], 0.95),
                "generation_p50": percentile([item["generation"]["latency_ms"] for item in items], 0.5),
                "generation_p95": percentile([item["generation"]["latency_ms"] for item in items], 0.95),
                "end_to_end_p50": percentile([item["total_latency_ms"] for item in items], 0.5),
                "end_to_end_p95": percentile([item["total_latency_ms"] for item in items], 0.95),
            },
        }

    by_category = {
        category: summarize_group([item for item in successful if item["category"] == category])
        for category in CATEGORY_LABELS
    }
    return {
        "total": len(results),
        "successful": len(successful),
        "errors": len(results) - len(successful),
        "overall": summarize_group(successful),
        "by_category": by_category,
    }


def write_outputs(
    output_dir: Path,
    results: list[dict[str, object]],
    summary: dict[str, object],
    metadata: dict[str, object],
) -> None:
    results_path = output_dir / "results.jsonl"
    results_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results),
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps({"metadata": metadata, "metrics": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (output_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "id",
            "category",
            "source_filename",
            "question",
            "reference_answer",
            "answer",
            "document_rank",
            "chunk_rank",
            "reranker_status",
            "retrieval_relevance",
            "retrieval_sufficiency",
            "answer_correctness",
            "answer_groundedness",
            "answer_completeness",
            "citation_quality",
            "ambiguity_handling",
            "end_to_end_score",
            "passed",
            "critical_error",
            "reason",
            "total_latency_ms",
            "error",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            retrieval = item.get("retrieval", {})
            generation = item.get("generation", {})
            judge = item.get("judge", {})
            writer.writerow(
                {
                    "id": item.get("id"),
                    "category": item.get("category"),
                    "source_filename": item.get("source_filename"),
                    "question": item.get("question"),
                    "reference_answer": item.get("reference_answer"),
                    "answer": generation.get("answer"),
                    "document_rank": retrieval.get("document_rank"),
                    "chunk_rank": retrieval.get("chunk_rank"),
                    "reranker_status": retrieval.get("reranker_status"),
                    "retrieval_relevance": judge.get("retrieval_relevance"),
                    "retrieval_sufficiency": judge.get("retrieval_sufficiency"),
                    "answer_correctness": judge.get("answer_correctness"),
                    "answer_groundedness": judge.get("answer_groundedness"),
                    "answer_completeness": judge.get("answer_completeness"),
                    "citation_quality": judge.get("citation_quality"),
                    "ambiguity_handling": judge.get("ambiguity_handling"),
                    "end_to_end_score": judge.get("end_to_end_score"),
                    "passed": judge.get("passed"),
                    "critical_error": judge.get("critical_error"),
                    "reason": judge.get("reason"),
                    "total_latency_ms": item.get("total_latency_ms"),
                    "error": item.get("error"),
                }
            )

    overall = summary.get("overall", {})
    lines = [
        "# AI学习知识库 150 题真实 RAG 评测",
        "",
        "## 口径",
        "",
        "- 题集：高频 60、长尾 60、模糊 30；每题绑定真实源文档和源 Chunk。",
        "- 检索：在线 Embedding + Chroma/FTS5 + RRF + 当前配置的 Reranker，Top 5。",
        "- 生成与裁判：当前默认 LLM；裁判是同模型自评，不等同于人工金标。",
        "- 通过：证据充分性、正确性、忠实性均不少于 3，引用不少于 2；模糊题处理不少于 3。",
        "- 端到端范围：问题 → HybridRetriever → Top 5 证据 → LLM 回答 → LLM Judge；不包含 LangGraph 是否主动选择知识库工具。",
        "",
        "## 解释边界",
        "",
        "- 题目和参考答案由当前 LLM 从源 Chunk 生成，属于文档内合成测试，不代表真实用户查询分布。",
        "- 出题、回答与裁判使用同一模型，分数可能存在自评偏高；关键结论仍需人工抽检或更换独立 Judge 复验。",
        "- 每题只绑定一个源 Chunk；若检索到同文档中的等价证据，回答可能正确，但源 Chunk Hit 仍会记为未命中。",
        "- 本轮不评估对话历史、Memory、SSE、LangGraph 工具选择及前端交互，因此不能外推为完整产品端到端通过率。",
        "",
        "## 总体结果",
        "",
        f"- 完成：{summary.get('successful', 0)}/{summary.get('total', 0)}；错误：{summary.get('errors', 0)}。",
    ]
    if overall:
        scores = overall["llm_scores"]
        lines.extend(
            [
                f"- 源文档 Hit@5：{overall['document_hit_at_5']:.2%}；MRR@5：{overall['document_mrr_at_5']:.3f}。",
                f"- 源 Chunk Hit@5：{overall['chunk_hit_at_5']:.2%}。",
                f"- 源文档未排第 1：{sum(item['retrieval']['document_rank'] != 1 for item in results if 'retrieval' in item)} 题；源 Chunk 未进入 Top 5：{sum(item['retrieval']['chunk_rank'] is None for item in results if 'retrieval' in item)} 题。",
                f"- 检索相关性/充分性：{scores['retrieval_relevance']:.3f} / {scores['retrieval_sufficiency']:.3f}（满分 4）。",
                f"- 正确性/忠实性/完整性/引用：{scores['answer_correctness']:.3f} / {scores['answer_groundedness']:.3f} / {scores['answer_completeness']:.3f} / {scores['citation_quality']:.3f}。",
                f"- 端到端平均分：{scores['end_to_end_score']:.3f}/4；LLM 通过率：{overall['llm_pass_rate']:.2%}；幻觉率：{overall['hallucination_rate']:.2%}。",
                f"- Reranker 成功率：{overall['reranker_success_rate']:.2%}。",
                f"- 端到端延迟 P50/P95：{overall['latency_ms']['end_to_end_p50']:.0f}/{overall['latency_ms']['end_to_end_p95']:.0f} ms。",
            ]
        )
    lines.extend(
        [
            "",
            "## 分类结果",
            "",
            "| 类型 | 数量 | 文档 Hit@5 | Chunk Hit@5 | 正确性 | 忠实性 | 端到端 | 通过率 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for category, label in CATEGORY_LABELS.items():
        item = summary.get("by_category", {}).get(category, {})
        if not item:
            continue
        scores = item["llm_scores"]
        lines.append(
            f"| {label} | {item['count']} | {item['document_hit_at_5']:.2%} | {item['chunk_hit_at_5']:.2%} | "
            f"{scores['answer_correctness']:.3f} | {scores['answer_groundedness']:.3f} | "
            f"{scores['end_to_end_score']:.3f} | {item['llm_pass_rate']:.2%} |"
        )

    failures = sorted(
        (item for item in results if "judge" in item and not item["judge"]["passed"]),
        key=lambda item: (item["judge"]["end_to_end_score"], item["id"]),
    )[:20]
    lines.extend(["", "## 最差样本（最多 20 条）", ""])
    if not failures:
        lines.append("无。")
    else:
        for item in failures:
            lines.append(f"- `{item['id']}` {item['question']} — {item['judge']['reason']}")
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    output_dir = args.output.resolve()
    questions = generate_questions(output_dir, args.knowledge_base, args.model_alias, args.regenerate)
    if args.limit > 0:
        questions = questions[: args.limit]

    with SessionLocal() as session:
        knowledge_base = session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.name == args.knowledge_base)
        )
        if knowledge_base is None:
            raise RuntimeError(f"知识库不存在：{args.knowledge_base}")
        knowledge_base_id = knowledge_base.id
        embedding_profile = knowledge_base.embedding_profile

    checkpoint_path = output_dir / "results.jsonl"
    completed: dict[str, dict[str, object]] = {}
    if checkpoint_path.exists() and not args.regenerate:
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if "error" not in item:
                    completed[str(item["id"])] = item
    pending = [item for item in questions if str(item["id"]) not in completed]
    print(
        f"[eval] questions={len(questions)} cached={len(completed)} pending={len(pending)} workers={args.workers}",
        flush=True,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    with checkpoint_path.open("a", encoding="utf-8") as checkpoint:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            future_map = {
                executor.submit(evaluate_one, question, knowledge_base_id, args.model_alias): question
                for question in pending
            }
            finished_count = len(completed)
            for future in as_completed(future_map):
                question = future_map[future]
                try:
                    result = future.result()
                except Exception as error:
                    result = {**question, "error": f"{type(error).__name__}: {error}"}
                completed[str(question["id"])] = result
                checkpoint.write(json.dumps(result, ensure_ascii=False) + "\n")
                checkpoint.flush()
                finished_count += 1
                status = "ok" if "error" not in result else "error"
                print(
                    f"[eval {finished_count:03d}/{len(questions):03d}] {question['id']} {status}", flush=True
                )

    ordered = [completed[str(question["id"])] for question in questions]
    summary = metric_summary(ordered)
    profile = model_registry.profile(args.model_alias)
    metadata = {
        "knowledge_base": args.knowledge_base,
        "knowledge_base_id": knowledge_base_id,
        "embedding_profile": embedding_profile,
        "model_alias": args.model_alias,
        "model": profile.model,
        "question_count": len(questions),
        "workers": args.workers,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_outputs(output_dir, ordered, summary, metadata)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"[done] {output_dir}", flush=True)
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
