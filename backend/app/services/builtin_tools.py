"""Built-in tool definitions used by the unified tool registry."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool, tool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import KnowledgeBase
from app.domain.tool_types import ToolContext
from app.services.artifacts import ArtifactError, artifact_envelope, create_artifact
from app.services.context import (
    TOOL_RESULT_MAX_CHARS,
    limit_tool_content,
)
from app.services.extensions import ExtensionError, list_packages, load_skill_text
from app.services.extensions import read_skill_resource as read_skill_resource_file
from app.services.jobs import create_manga_download_job, delete_manga_artifact
from app.services.manga import manga_service
from app.services.retrieval import HybridRetriever

MAX_KNOWLEDGE_SEARCHES_PER_TURN = 4
MANGA_TOOL_ACTIONS = {
    "search_manga": "search",
    "request_manga_download": "download",
    "delete_manga_download": "delete",
}


@dataclass
class KnowledgeSearchGuard:
    max_unique_searches: int = MAX_KNOWLEDGE_SEARCHES_PER_TURN
    _seen: set[tuple[str, str]] = field(default_factory=set)
    _force_final: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _key(query: str, knowledge_base_id: str) -> tuple[str, str]:
        return (" ".join(query.casefold().split()), knowledge_base_id)

    def reserve(self, query: str, knowledge_base_id: str) -> str:
        with self._lock:
            key = self._key(query, knowledge_base_id)
            if key in self._seen:
                self._force_final = True
                return "duplicate"
            if len(self._seen) >= self.max_unique_searches:
                self._force_final = True
                return "limit"
            self._seen.add(key)
            if len(self._seen) >= self.max_unique_searches:
                self._force_final = True
            return "allowed"

    @property
    def force_final(self) -> bool:
        with self._lock:
            return self._force_final


def tool_envelope(
    data: object,
    *,
    pending_confirmation: bool = False,
    artifact_ids: list[str] | None = None,
) -> str:
    payload = json.dumps(
        {
            "kind": "tool_result",
            "data": data,
            "pending_confirmation": pending_confirmation,
            "artifact_ids": artifact_ids or [],
        },
        ensure_ascii=False,
    )
    return limit_tool_content(payload, TOOL_RESULT_MAX_CHARS)


def create_builtin_tools(
    session: Session,
    context: ToolContext,
    *,
    allowed_manga_actions: frozenset[str] = frozenset(),
    knowledge_search_guard: KnowledgeSearchGuard | None = None,
    manga_download_job_factory: Callable[..., Any] | None = None,
) -> list[BaseTool]:
    """Build the existing built-in tools without embedding them in graph code."""

    guard = knowledge_search_guard or KnowledgeSearchGuard()
    create_job = manga_download_job_factory or create_manga_download_job

    @tool
    def search_knowledge(query: str, knowledge_base_id: str = "") -> str:
        """在个人知识库中检索证据。每轮最多四个不同查询；重复查询会停止继续检索。"""
        if not knowledge_base_id:
            first = session.scalar(select(KnowledgeBase).order_by(KnowledgeBase.created_at))
            if first is None:
                return tool_envelope({"evidence": [], "message": "尚未创建知识库"})
            knowledge_base_id = first.id
        decision = guard.reserve(query, knowledge_base_id)
        if decision == "duplicate":
            return tool_envelope(
                {
                    "evidence": [],
                    "duplicate": True,
                    "message": "相同查询和知识库已检索过，请使用已有证据直接回答。",
                }
            )
        if decision == "limit":
            return tool_envelope(
                {
                    "evidence": [],
                    "limit_reached": True,
                    "message": "本轮知识检索已达到上限，请使用已有证据直接回答。",
                }
            )
        retriever = HybridRetriever(session)
        hits = retriever.search(knowledge_base_id, query)
        return tool_envelope(
            {
                "retrieval": {"reranker": retriever.reranker_status},
                "evidence": [
                    {
                        "content": hit.content,
                        "filename": hit.filename,
                        "heading_path": hit.heading_path,
                        "page_number": hit.page_number,
                        "vector_score": hit.vector_score,
                        "keyword_score": hit.keyword_score,
                        "rrf_score": hit.rrf_score,
                        "rerank_score": hit.rerank_score,
                    }
                    for hit in hits
                ],
            }
        )

    @tool
    def search_manga(query: str) -> str:
        """在程序确认用户明确请求搜索漫画后，根据关键词搜索并只返回候选。"""
        if "search" not in allowed_manga_actions:
            return tool_envelope({"error": "本轮没有明确的漫画搜索意图"})
        return tool_envelope({"results": manga_service.search(query)})

    @tool
    def request_manga_download(album_id: str) -> str:
        """在程序确认用户明确请求下载后，Owner 立即创建指定漫画 ID 的任务。"""
        if "download" not in allowed_manga_actions:
            return tool_envelope({"error": "本轮没有明确的漫画下载意图"})
        try:
            job = create_job(album_id, context.requester_id, context.conversation_id)
        except (PermissionError, ValueError) as error:
            return tool_envelope({"error": str(error)})
        delivery_message = (
            "下载成功后会自动发送到当前 Owner QQ 私聊。发送完成后如需清理，请明确要求删除并提供任务 ID。"
            if context.platform == "qq" and context.requester_id.isdigit()
            else "任务完成后可在 Web 任务页面下载产物。"
        )
        return tool_envelope(
            {
                "task": {"id": job.id, "type": job.type, "status": job.status},
                "message": delivery_message,
            }
        )

    @tool
    def delete_manga_download(job_id: str) -> str:
        """在程序确认明确删除意图后，删除已成功发送的漫画任务产物。"""
        if "delete" not in allowed_manga_actions:
            return tool_envelope({"error": "本轮没有明确的漫画删除意图"})
        if context.is_group:
            return tool_envelope({"error": "群聊禁止删除漫画产物"})
        try:
            result = delete_manga_artifact(job_id, context.requester_id)
        except (PermissionError, ValueError, OSError) as error:
            return tool_envelope({"error": str(error)})
        return tool_envelope(result)

    @tool
    def create_files(files: list[dict[str, str]]) -> str:
        """创建代码或文本文件。files 为 filename/content 对象数组；只能在私聊使用，不会执行文件。"""
        if context.is_group or context.platform == "group":
            return tool_envelope({"error": "群聊禁止创建文件"})
        try:
            pairs = [(str(item["filename"]), str(item["content"]).encode("utf-8")) for item in files]
            artifact = create_artifact(
                session,
                owner_id=context.requester_id,
                conversation_id=context.conversation_id,
                files=pairs,
            )
            return tool_envelope(
                {"artifact": artifact_envelope(artifact)}, artifact_ids=[artifact.id]
            )
        except (KeyError, TypeError, ArtifactError) as error:
            return tool_envelope({"error": str(error)})

    loaded_skills: set[str] = set()

    @tool
    def load_skill(name: str) -> str:
        """按需加载一个 Skill 的完整 SKILL.md；每轮最多加载三个。"""
        if name in loaded_skills:
            return tool_envelope({"name": name, "content": "该 Skill 已加载"})
        if len(loaded_skills) >= 3:
            return tool_envelope({"error": "每轮最多加载 3 个 Skill"})
        try:
            content = load_skill_text(session, name)
        except ExtensionError as error:
            return tool_envelope({"error": str(error)})
        loaded_skills.add(name)
        return tool_envelope({"name": name, "content": content})

    @tool
    def read_skill_resource(name: str, relative_path: str) -> str:
        """读取已启用 Skill 根目录内的 references/assets 文件，不执行 scripts。"""
        if name not in loaded_skills:
            return tool_envelope({"error": "请先调用 load_skill 加载该 Skill"})
        try:
            content = read_skill_resource_file(session, name, relative_path)
        except ExtensionError as error:
            return tool_envelope({"error": str(error)})
        return tool_envelope({"name": name, "path": relative_path, "content": content})

    enabled_builtins = {
        item.name for item in list_packages(session, "tool") if item.builtin and item.enabled
    }
    tools: list[BaseTool] = [load_skill, read_skill_resource]
    if "knowledge-search" in enabled_builtins:
        tools.append(search_knowledge)
    if "manga" in enabled_builtins:
        if "search" in allowed_manga_actions:
            tools.append(search_manga)
        if "download" in allowed_manga_actions:
            tools.append(request_manga_download)
        if "delete" in allowed_manga_actions:
            tools.append(delete_manga_download)
    if "create-files" in enabled_builtins:
        tools.append(create_files)
    return tools
