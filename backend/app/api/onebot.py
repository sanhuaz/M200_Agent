from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.models import Artifact, Conversation, ExtensionPackage, Job, KnowledgeBase, Memory, ProcessedEvent
from app.db.session import SessionLocal
from app.services.chat import chat_service
from app.services.confirmations import (
    create_extension_confirmation,
    is_owner,
    resolve_confirmation,
)
from app.services.jobs import (
    create_manga_download_job,
    delete_manga_artifact,
    job_worker,
    update_job_result,
)
from app.services.manga import manga_service
from app.services.models import model_registry

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


class OneBotManager:
    def __init__(self) -> None:
        self.websocket: WebSocket | None = None
        self.self_id: str | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}

    async def serve(self, websocket: WebSocket) -> None:
        token = self._extract_token(websocket)
        if (
            not settings.onebot_token
            or settings.onebot_token == "change-me"
            or token != settings.onebot_token
        ):
            await websocket.close(code=1008, reason="OneBot Token 未配置或不匹配")
            return
        await websocket.accept()
        self.websocket = websocket
        try:
            while True:
                payload = await websocket.receive_json()
                echo = payload.get("echo")
                if echo is not None and str(echo) in self._pending:
                    future = self._pending.pop(str(echo))
                    if not future.done():
                        future.set_result(payload)
                    continue
                if payload.get("post_type") == "message":
                    asyncio.create_task(self._handle_message(payload))
        except WebSocketDisconnect:
            logger.info("NapCat OneBot 已断开")
        finally:
            if self.websocket is websocket:
                self.websocket = None
                self.self_id = None
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("OneBot 连接已断开"))
            self._pending.clear()

    def _extract_token(self, websocket: WebSocket) -> str:
        query_token = websocket.query_params.get("access_token") or websocket.query_params.get("token")
        if query_token:
            return query_token
        authorization = websocket.headers.get("authorization", "")
        return authorization.removeprefix("Bearer ").strip()

    async def action(
        self,
        action: str,
        params: dict[str, object],
        *,
        timeout_seconds: float = 30,
    ) -> dict[str, object]:
        if self.websocket is None:
            raise ConnectionError("NapCat 未连接")
        echo = uuid.uuid4().hex
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        async with self._send_lock:
            await self.websocket.send_json({"action": action, "params": params, "echo": echo})
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        finally:
            self._pending.pop(echo, None)

    async def send_text(self, user_id: str, text: str, group_id: str | None = None) -> None:
        if group_id:
            response = await self.action(
                "send_group_msg", {"group_id": int(group_id), "message": text}
            )
        else:
            response = await self.action(
                "send_private_msg", {"user_id": int(user_id), "message": text}
            )
        if response.get("status") != "ok" or int(str(response.get("retcode", -1))) != 0:
            raise RuntimeError(f"QQ 文本发送失败: {response.get('message') or response.get('wording')}")

    async def send_private_file(self, user_id: str, path: Path) -> None:
        response = await self.action(
            "upload_private_file",
            {"user_id": int(user_id), "file": str(path.resolve()), "name": path.name},
            timeout_seconds=300,
        )
        if response.get("status") != "ok" or int(str(response.get("retcode", -1))) != 0:
            raise RuntimeError(f"QQ 文件上传失败: {response.get('message') or response.get('wording')}")

    async def _handle_message(self, event: dict[str, object]) -> None:
        message_id = str(event.get("message_id", ""))
        if not message_id:
            return
        with SessionLocal.begin() as session:
            if session.get(ProcessedEvent, message_id):
                return
            session.add(ProcessedEvent(message_id=message_id))
        self.self_id = str(event.get("self_id", self.self_id or ""))
        message_type = str(event.get("message_type", "private"))
        user_id = str(event.get("user_id", ""))
        group_id = str(event.get("group_id", "")) if message_type == "group" else None
        raw = str(event.get("raw_message") or event.get("message") or "").strip()
        text = self._extract_triggered_text(raw, message_type)
        if text is None or not user_id:
            return
        text = self._expand_manual_extension_request(text)
        external_id = f"group:{group_id}" if group_id else f"private:{user_id}"
        try:
            command_response = await self._command(text, user_id, external_id)
            if command_response is not None:
                await self.send_text(user_id, command_response, group_id)
                return
            with SessionLocal() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is None:
                    conversation = Conversation(
                        platform="qq",
                        external_id=external_id,
                        conversation_type="group" if group_id else "private",
                        title=f"QQ {external_id}",
                        owner_id=user_id,
                    )
                    session.add(conversation)
                    try:
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        conversation = session.scalar(
                            select(Conversation).where(
                                Conversation.platform == "qq",
                                Conversation.external_id == external_id,
                            )
                        )
                    if conversation is None:
                        raise RuntimeError("无法创建 QQ 会话")
                final_text = ""
                error_text = ""
                artifact_ids: list[str] = []
                async for item in chat_service.stream(
                    session,
                    conversation,
                    user_id,
                    text,
                    message_id,
                    platform="qq",
                    is_group=bool(group_id),
                ):
                    event_data = item["data"]
                    if not isinstance(event_data, dict):
                        continue
                    if item["event"] == "final":
                        final_text = str(event_data.get("text", ""))
                    elif item["event"] == "error":
                        error_text = str(event_data.get("message", ""))
                    elif item["event"] == "artifact_created":
                        artifact_id = str(event_data.get("artifact_id", ""))
                        if artifact_id:
                            artifact_ids.append(artifact_id)
                await self.send_text(user_id, final_text or f"处理失败：{error_text}", group_id)
                if not group_id:
                    with SessionLocal() as artifact_session:
                        artifacts = [
                            artifact_session.get(Artifact, artifact_id)
                            for artifact_id in artifact_ids
                        ]
                    for artifact in artifacts:
                        if artifact is None or not Path(artifact.path).is_file():
                            continue
                        path = Path(artifact.path)
                        if path.stat().st_size <= settings.qq_upload_limit_mb * 1024 * 1024:
                            try:
                                await self.send_private_file(user_id, path)
                            except Exception as error:
                                await self.send_text(
                                    user_id,
                                    f"文件发送失败：{error}\n本地路径：{path.resolve()}",
                                )
                        else:
                            await self.send_text(
                                user_id,
                                f"文件超过 QQ 上传阈值，本地路径：{path.resolve()}",
                            )
        except Exception as error:
            logger.exception("处理 QQ 消息失败")
            try:
                await self.send_text(user_id, f"处理失败：{type(error).__name__}: {error}", group_id)
            except Exception:
                logger.exception("发送 QQ 错误回复失败")

    def _extract_triggered_text(self, raw: str, message_type: str) -> str | None:
        if message_type != "group":
            return raw
        at_pattern = rf"\[CQ:at,qq={re.escape(self.self_id or '')}[^\]]*\]"
        mentioned = bool(self.self_id and re.search(at_pattern, raw))
        prefixed = raw.startswith(settings.group_command_prefix)
        if not mentioned and not prefixed:
            return None
        value = re.sub(at_pattern, "", raw).strip()
        if value.startswith(settings.group_command_prefix):
            value = value[len(settings.group_command_prefix) :].strip()
        return value

    @staticmethod
    def _expand_manual_extension_request(text: str) -> str:
        for prefix, label in (("/skill ", "Skill"), ("/tool ", "Tool")):
            if not text.startswith(prefix):
                continue
            rest = text[len(prefix) :].strip()
            if not rest or rest.startswith(("install ", "enable ", "disable ", "remove ")):
                return text
            name, separator, request = rest.partition(" ")
            if separator and request.strip():
                return f"请明确调用 {label} {name}，完成以下请求：{request.strip()}"
        return text

    async def _command(self, text: str, user_id: str, external_id: str) -> str | None:
        if text == "/help":
            return (
                "命令：/model list、/model use <alias>、/kb、/memory、"
                "/memory delete <id>、/tools、/skills、/skill <name> <请求>、"
                "/jm <关键词>、/jm download <漫画ID>、/jm delete <任务ID>、"
                "/confirm <token>、/cancel <token>"
            )
        if text == "/model list":
            return "可用模型：\n" + "\n".join(
                f"- {item['alias']} ({'已配置' if item['configured'] else '未配置'})"
                for item in model_registry.list()
            )
        if text.startswith("/model use "):
            if not is_owner(user_id):
                return "只有 Owner 可以切换模型。"
            alias = text.removeprefix("/model use ").strip()
            model_registry.profile(alias)
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is None:
                    conversation = Conversation(
                        platform="qq",
                        external_id=external_id,
                        conversation_type="group" if external_id.startswith("group:") else "private",
                        title=f"QQ {external_id}",
                        owner_id=user_id,
                    )
                    session.add(conversation)
                conversation.model_alias = alias
            return f"已将当前 QQ 会话模型切换为 {alias}。"
        if text == "/kb":
            with SessionLocal() as session:
                items = session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name)).all()
                return "知识库：\n" + ("\n".join(f"- {item.name}: {item.id}" for item in items) or "暂无")
        if text == "/memory":
            with SessionLocal() as session:
                items = session.scalars(
                    select(Memory).where(
                        Memory.scope_type == "user", Memory.user_id == user_id, Memory.status == "active"
                    )
                ).all()
                return "当前记忆：\n" + (
                    "\n".join(f"- {item.id}: {item.content}" for item in items) or "暂无"
                )
        if text.startswith("/memory delete "):
            memory_id = text.removeprefix("/memory delete ").strip()
            with SessionLocal.begin() as session:
                item = session.get(Memory, memory_id)
                if item is None or item.scope_type != "user" or item.user_id != user_id:
                    return "找不到属于你的这条记忆。"
                item.status = "archived"
            return "已删除指定记忆。"
        if text == "/tools":
            with SessionLocal() as session:
                items = session.scalars(
                    select(ExtensionPackage).where(ExtensionPackage.kind == "tool")
                ).all()
                return "Tools：\n" + (
                    "\n".join(
                        f"- {item.name} ({'启用' if item.enabled else '停用'})：{item.description}"
                        for item in items
                    )
                    or "暂无"
                )
        if text == "/skills":
            with SessionLocal() as session:
                items = session.scalars(
                    select(ExtensionPackage).where(ExtensionPackage.kind == "skill")
                ).all()
                return "Skills：\n" + (
                    "\n".join(
                        f"- {item.name} ({'启用' if item.enabled else '停用'})：{item.description}"
                        for item in items
                    )
                    or "暂无"
                )
        management_prefixes = (
            "/skill install ",
            "/skill enable ",
            "/skill disable ",
            "/skill remove ",
        )
        if text.startswith(management_prefixes):
            if not is_owner(user_id) or external_id.startswith("group:"):
                return "只有 Owner 私聊可以管理 Skill。"
            operation, _, name_or_url = text.removeprefix("/skill ").partition(" ")
            confirmation = create_extension_confirmation(
                "skill", operation, name_or_url.strip(), user_id, None
            )
            return f"Skill 操作待确认：/confirm {confirmation.token}（十分钟内有效）"
        tool_management_prefixes = (
            "/tool install ",
            "/tool enable ",
            "/tool disable ",
            "/tool remove ",
        )
        if text.startswith(tool_management_prefixes):
            if not is_owner(user_id) or external_id.startswith("group:"):
                return "只有 Owner 私聊可以管理 Tool。"
            operation, _, name_or_url = text.removeprefix("/tool ").partition(" ")
            confirmation = create_extension_confirmation(
                "tool", operation, name_or_url.strip(), user_id, None
            )
            return f"Tool 操作待确认：/confirm {confirmation.token}（十分钟内有效）"
        if text.startswith("/jm download "):
            if external_id.startswith("group:"):
                return "群聊禁止创建漫画下载任务。"
            album_id = text.removeprefix("/jm download ").strip()
            try:
                job = create_manga_download_job(album_id, user_id, None)
            except (PermissionError, ValueError) as error:
                return str(error)
            return (
                f"已创建下载任务：{job.id}。下载成功后会自动发送文件；"
                f"发送完成后可用 /jm delete {job.id} 删除本地产物。"
            )
        if text.startswith("/jm delete "):
            if external_id.startswith("group:"):
                return "群聊禁止删除漫画产物。"
            job_id = text.removeprefix("/jm delete ").strip()
            try:
                result = delete_manga_artifact(job_id, user_id)
            except (PermissionError, ValueError, OSError) as error:
                return str(error)
            return f"已删除漫画任务 {result['job_id']} 的本地产物，任务审计记录已保留。"
        if text.startswith("/jm "):
            results = await asyncio.wait_for(
                asyncio.to_thread(manga_service.search, text.removeprefix("/jm ").strip()),
                timeout=45,
            )
            return "搜索结果：\n" + "\n".join(
                f"- JM{item['album_id']} {item['title']}" for item in results[:10]
            )
        if text.startswith(("/confirm ", "/cancel ")):
            approve = text.startswith("/confirm ")
            token = text.split(maxsplit=1)[1].strip()
            confirmation, job = resolve_confirmation(token, user_id, approve)
            if job:
                return f"已创建下载任务：{job.id}"
            if confirmation.action == "extension_manage" and confirmation.status == "approved":
                return "已执行扩展管理操作。"
            return "已取消该确认请求。"
        return None

    async def notify_job(self, job: Job) -> None:
        if not job.requester_id.isdigit() or not is_owner(job.requester_id):
            return
        if job.status != "succeeded" or not job.result:
            await self.send_text(job.requester_id, f"任务 {job.id} {job.status}：{job.error or '无详细信息'}")
            return
        result = json.loads(job.result)
        path = Path(str(result.get("path", "")))
        if not path.is_file():
            await self.send_text(job.requester_id, f"任务完成，但产物不存在：{path}")
            return
        limit = settings.qq_upload_limit_mb * 1024 * 1024
        if path.stat().st_size > limit:
            update_job_result(
                job.id,
                {
                    "delivery_status": "skipped_oversize",
                    "delivery_error": "文件超过 QQ 上传阈值",
                },
            )
            await self.send_text(job.requester_id, f"任务完成，文件超过上传阈值：{path.resolve()}")
            return
        try:
            await self.send_private_file(job.requester_id, path)
            update_job_result(
                job.id,
                {
                    "delivery_status": "sent",
                    "delivered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "delivery_error": None,
                },
            )
            await self.send_text(
                job.requester_id,
                f"文件已发送。确认收到后，如需清理本地文件请发送：/jm delete {job.id}",
            )
        except Exception as error:
            error_text = f"{type(error).__name__}: {error}".rstrip()
            update_job_result(
                job.id,
                {"delivery_status": "failed", "delivery_error": error_text},
            )
            await self.send_text(
                job.requester_id,
                f"QQ 文件发送失败：{error_text}\n本地路径：{path.resolve()}",
            )


onebot_manager = OneBotManager()
job_worker.notifier = onebot_manager.notify_job


@router.websocket("/onebot/ws")
async def onebot_websocket(websocket: WebSocket) -> None:
    await onebot_manager.serve(websocket)
