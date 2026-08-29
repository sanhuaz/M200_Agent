from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.models import (
    Artifact,
    Conversation,
    EmotionAssessment,
    ExtensionPackage,
    Job,
    KnowledgeBase,
    Memory,
    Message,
    Persona,
    ProcessedEvent,
    ResponseFeedback,
)
from app.db.session import SessionLocal
from app.services.chat import chat_service
from app.services.companion import (
    COMPANION_ANALYSIS_RETRY_JOB_TYPE,
    EMOTION_LABEL_ZH,
    SUPPORT_NEED_ZH,
    assessment_dict,
    get_or_create_preference,
    parse_emotion_labels_strict,
    safety_redirect_text,
)
from app.services.confirmations import (
    create_extension_confirmation,
    is_owner,
    resolve_confirmation,
)
from app.services.context import estimate_text_tokens, load_pending_messages, pending_message_count
from app.services.jobs import (
    create_manga_download_job,
    delete_manga_artifact,
    job_worker,
    update_job_result,
)
from app.services.manga import manga_service
from app.services.models import model_registry
from app.services.operation_logs import operation_logs

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


class OneBotManager:
    def __init__(self) -> None:
        self.websocket: WebSocket | None = None
        self.self_id: str | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self.qq_status = "unknown"
        self.qq_nickname: str | None = None
        self._monitor_task: asyncio.Task[None] | None = None

    def status(self) -> dict[str, object]:
        return {
            "connection": "connected" if self.websocket is not None else "disconnected",
            "qq": self.qq_status,
            "self_id": self.self_id,
            "nickname": self.qq_nickname,
        }

    def start_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_login(), name="onebot-login-monitor")

    async def stop_monitor(self) -> None:
        task, self._monitor_task = self._monitor_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _monitor_login(self) -> None:
        while True:
            try:
                if self.websocket is None:
                    self.qq_status = "offline"
                else:
                    response = await self.action("get_login_info", {})
                    raw_data = response.get("data")
                    data: dict[str, object] = raw_data if isinstance(raw_data, dict) else {}
                    if response.get("status") == "ok" and int(str(response.get("retcode", -1))) == 0:
                        self.qq_status = "online"
                        self.self_id = str(data.get("user_id") or self.self_id or "")
                        self.qq_nickname = str(data.get("nickname") or "") or None
                    else:
                        self.qq_status = "offline"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.qq_status = "unknown" if self.websocket is not None else "offline"
                logger.debug("OneBot 登录状态检查失败：%s", type(error).__name__)
            await asyncio.sleep(30)

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
        self.qq_status = "unknown"
        operation_logs.emit(
            source="onebot", kind="started", title="OneBot 连接", message="OneBot WebSocket 已连接"
        )
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
                    operation_logs.emit(
                        source="onebot",
                        kind="message",
                        title="收到 QQ 消息",
                        message=str(payload.get("raw_message") or payload.get("message") or ""),
                        details={
                            "user_id": payload.get("user_id"),
                            "group_id": payload.get("group_id"),
                            "message_id": payload.get("message_id"),
                        },
                    )
                    asyncio.create_task(self._handle_message(payload))
        except WebSocketDisconnect:
            logger.info("NapCat OneBot 已断开")
            operation_logs.emit(
                source="onebot",
                level="warn",
                kind="failed",
                title="OneBot 连接断开",
                message="OneBot WebSocket 已断开",
            )
        finally:
            if self.websocket is websocket:
                self.websocket = None
                self.self_id = None
                self.qq_status = "offline"
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
                try:
                    await self.send_text(user_id, command_response, group_id)
                except Exception as error:
                    operation_logs.emit(
                        source="onebot",
                        kind="failed",
                        title="QQ 命令发送失败",
                        message="命令结果未送达",
                        details={"user_id": user_id, "error": type(error).__name__},
                    )
                    raise
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
                        model_alias=model_registry.default_alias(),
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
                try:
                    await self.send_text(user_id, final_text or f"处理失败：{error_text}", group_id)
                except Exception as error:
                    operation_logs.emit(
                        source="onebot",
                        kind="failed",
                        title="QQ 回复发送失败",
                        message="回复已写入数据库但未确认送达",
                        details={
                            "conversation_id": conversation.id,
                            "user_id": user_id,
                            "error": type(error).__name__,
                        },
                    )
                    raise
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
                "可用命令：\n\n"
                "【会话与模型】\n"
                "/new\n"
                "/reset-context\n"
                "/context\n"
                "/model list\n"
                "/model use <alias>\n\n"
                "【知识库与记忆】\n"
                "/kb\n"
                "/memory\n"
                "/memory delete <id>\n\n"
                "【人格】\n"
                "/persona\n"
                "/persona list\n"
                "/persona use <名称或ID>\n"
                "/persona off\n\n"
                "【情感陪伴（仅 Owner 私聊）】\n"
                "/support auto|listen|reflect|advice\n"
                "/emotion\n"
                "/emotion correct <情绪>\n"
                "/feedback helpful|unhelpful|no-advice\n"
                "/companion pause|resume\n"
                "/companion memory on|off\n\n"
                "/companion safety standard\n"
                "/companion safety unfiltered confirm\n"
                "/companion status\n\n"
                "【Tools 与 Skills】\n"
                "/tools\n"
                "/skills\n"
                "/skill <name> <请求>\n\n"
                "【漫画】\n"
                "/jm <关键词>\n"
                "/jm download <漫画ID>\n"
                "/jm delete <任务ID>\n\n"
                "【确认】\n"
                "/confirm <token>\n"
                "/cancel <token>"
            )
        if text == "/support" or text.startswith("/support "):
            if external_id.startswith("group:"):
                return "情感陪伴仅支持 QQ 私聊，群聊不会加载私人陪伴资料。"
            if not is_owner(user_id):
                return "只有 Owner 可以使用情感陪伴。"
            mode = text.removeprefix("/support").strip()
            if mode not in {"auto", "listen", "reflect", "advice"}:
                return "用法：/support auto|listen|reflect|advice"
            with SessionLocal.begin() as session:
                preference = get_or_create_preference(session, user_id)
                preference.support_mode = mode
            mode_labels = {
                "auto": "自动判断",
                "listen": "倾听",
                "reflect": "一起梳理",
                "advice": "建议",
            }
            return f"本次及后续 QQ 私聊支持方式已设为：{mode_labels[mode]}。"
        if text == "/emotion" or text.startswith("/emotion correct"):
            if external_id.startswith("group:"):
                return "情感陪伴仅支持 QQ 私聊，群聊没有个人情绪记录。"
            if not is_owner(user_id):
                return "只有 Owner 可以查看情绪分析。"
            correcting = text.startswith("/emotion correct")
            labels: list[str] = []
            invalid_labels: list[str] = []
            if correcting:
                raw_labels = text.removeprefix("/emotion correct").strip()
                labels, invalid_labels = parse_emotion_labels_strict(raw_labels)
                if invalid_labels or len(labels) > 3 or not labels:
                    options = "、".join(EMOTION_LABEL_ZH.values())
                    return f"情绪标签无效。请选择 1-3 个合法标签：{options}"
            with SessionLocal.begin() as session:
                assessment = session.scalar(
                    select(EmotionAssessment)
                    .where(EmotionAssessment.scope_id == user_id)
                    .order_by(desc(EmotionAssessment.created_at))
                    .limit(1)
                )
                if assessment is None:
                    return "还没有可查看的情绪分析记录。"
                if correcting:
                    assessment.correction = json.dumps(
                        {"emotions": labels, "corrected_at": datetime.now(UTC).isoformat()},
                        ensure_ascii=False,
                    )
                    return "已记录你的情绪纠正：" + "、".join(
                        EMOTION_LABEL_ZH.get(label, label) for label in labels
                    )
                view = assessment_dict(assessment, session)
                analysis_status = str(view["analysis_status"])
                corrected_emotions = view.get("effective_candidate_emotions")
                has_correction = bool(
                    isinstance(corrected_emotions, list)
                    and corrected_emotions
                    and view.get("correction")
                )
                if analysis_status == "retrying" and not has_correction:
                    return "最近一次情绪分析正在重试，暂时没有可靠结果。"
                if analysis_status == "safety_redirected" and not has_correction:
                    return (
                        "最近一次消息触发了安全转向，未执行情绪分类；"
                        "如需纠正请使用 /emotion correct。"
                    )
                if analysis_status == "failed" and not has_correction:
                    return (
                        "最近一次情绪分析失败，暂时无法可靠判断；"
                        "你可以使用 /emotion correct 进行纠正。"
                    )
                emotions = corrected_emotions if isinstance(corrected_emotions, list) else []
                display = "、".join(
                    EMOTION_LABEL_ZH.get(str(item), str(item)) for item in emotions
                )
                support_need_value = view.get("effective_support_need")
                support_need = (
                    SUPPORT_NEED_ZH.get(str(support_need_value), "尚不确定")
                    if isinstance(support_need_value, str)
                    else "尚不确定"
                )
                confidence = view.get("confidence")
                confidence_text = (
                    f"{confidence:.0%}"
                    if isinstance(confidence, (int, float))
                    else "—"
                )
                status_note = "；已采用你的纠正" if has_correction else ""
                return (
                    f"最近候选情绪：{display or '暂不确定'}；"
                    f"支持需要：{support_need}；"
                    f"置信度：{confidence_text}；"
                    f"当前策略：{assessment.next_action}{status_note}。"
                )
        if text == "/feedback" or text.startswith("/feedback "):
            if external_id.startswith("group:"):
                return "情感陪伴反馈仅支持 QQ 私聊。"
            if not is_owner(user_id):
                return "只有 Owner 可以提交陪伴反馈。"
            value = text.removeprefix("/feedback").strip()
            if value not in {"helpful", "unhelpful", "no-advice"}:
                return "用法：/feedback helpful|unhelpful|no-advice"
            with SessionLocal.begin() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is None:
                    return "当前还没有可反馈的 QQ 会话。"
                assistant = session.scalar(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation.id,
                        Message.role == "assistant",
                    )
                    .order_by(desc(Message.created_at))
                    .limit(1)
                )
                if assistant is None:
                    return "当前还没有可反馈的回复。"
                feedback = session.scalar(
                    select(ResponseFeedback).where(
                        ResponseFeedback.assistant_message_id == assistant.id
                    )
                )
                if feedback is None:
                    feedback = ResponseFeedback(
                        assistant_message_id=assistant.id,
                        scope_id=user_id,
                        feedback=value,
                    )
                    session.add(feedback)
                else:
                    feedback.feedback = value
                return "已记录本条回复反馈：" + value
        if text == "/companion" or text.startswith("/companion "):
            if external_id.startswith("group:"):
                return "情感陪伴仅支持 QQ 私聊，群聊不会启用。"
            if not is_owner(user_id):
                return "只有 Owner 可以管理情感陪伴。"
            action = text.removeprefix("/companion").strip()
            allowed_actions = {
                "pause",
                "resume",
                "memory on",
                "memory off",
                "safety standard",
                "safety unfiltered confirm",
                "status",
            }
            if action == "safety unfiltered":
                return "无过滤模式风险较高；如确认，请发送：/companion safety unfiltered confirm"
            if action not in allowed_actions:
                return (
                    "用法：/companion pause|resume|memory on|memory off；"
                    "/companion safety standard；"
                    "/companion safety unfiltered confirm；/companion status"
                )
            with SessionLocal.begin() as session:
                preference = get_or_create_preference(session, user_id)
                if action == "pause":
                    preference.companion_enabled = False
                    return "已暂停情感陪伴，后续私聊将回到通用 Agent。"
                if action == "resume":
                    preference.companion_enabled = True
                    return "已恢复情感陪伴。"
                if action == "status":
                    safety_mode = getattr(preference, "safety_mode", "standard") or "standard"
                    return (
                        f"情感陪伴：{'已启用' if preference.companion_enabled else '已暂停'}；"
                        f"关系记忆：{'已授权' if preference.memory_enabled else '未授权'}；"
                        "安全模式："
                        f"{'无过滤（仅关闭本机陪伴拦截）' if safety_mode == 'unfiltered' else '标准防护'}。"
                    )
                if action == "safety standard":
                    preference.safety_mode = "standard"
                    return "已切回标准防护模式。"
                if action == "safety unfiltered confirm":
                    preference.safety_mode = "unfiltered"
                    return (
                        "已启用无过滤模式（仅当前 Owner）。本机仍保留 Owner 权限、Tool 确认、"
                        "文件隔离和密钥脱敏；模型服务商仍可能自行拒答。"
                        "可用 /companion safety standard 关闭。"
                    )
                preference.memory_enabled = action == "memory on"
                return (
                    "已授权关系记忆；后续只保存低敏感、明确表达的关系事实。"
                    if preference.memory_enabled
                    else "已关闭记忆；后续陪伴私聊不召回或写入长期记忆。"
                )
        if text in {"/new", "/reset-context"}:
            if external_id.startswith("group:") and not is_owner(user_id):
                return "只有 Owner 可以轮换群聊上下文。"
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
                        model_alias=model_registry.default_alias(),
                        owner_id=user_id,
                    )
                    session.add(conversation)
                    return "已创建新的 QQ 会话，长期记忆保持不变。"
                conversation.external_id = f"archive:{conversation.id}"
                conversation.title = f"{conversation.title}（已归档）"
                new_conversation = Conversation(
                    platform="qq",
                    external_id=external_id,
                    conversation_type=conversation.conversation_type,
                    title=f"QQ {external_id}",
                    model_alias=conversation.model_alias,
                    persona_id=conversation.persona_id,
                    owner_id=user_id,
                )
                session.add(new_conversation)
            return "已开启新的 QQ 会话，旧消息已归档，长期记忆保持不变。"
        if text == "/context":
            with SessionLocal() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                if conversation is None:
                    return "当前还没有 QQ 会话。"
                pending = load_pending_messages(session, conversation)
                history_text = "\n".join(f"{item.role}: {item.content}" for item in pending)
                history_tokens = estimate_text_tokens(history_text)
                summary_tokens = estimate_text_tokens(conversation.summary or "")
                profile = model_registry.profile(conversation.model_alias)
                return (
                    f"当前会话：{conversation.id}\n"
                    f"待摘要消息：{pending_message_count(session, conversation)} 条\n"
                    f"历史摘要：{'已生成' if conversation.summary else '无'}（约 {summary_tokens} Token）\n"
                    f"近期历史估算：约 {history_tokens} Token\n"
                    f"输入软上限：{profile.input_soft_limit} Token\n"
                    f"上下文硬上限：{profile.context_window} Token\n"
                    "长期记忆不会因会话轮换而删除。"
                )
        if text in {"/persona", "/persona list"}:
            with SessionLocal() as session:
                conversation = session.scalar(
                    select(Conversation).where(
                        Conversation.platform == "qq", Conversation.external_id == external_id
                    )
                )
                current_id = conversation.persona_id if conversation else None
                personas = list(session.scalars(select(Persona).order_by(Persona.name)))
                lines = [f"当前人格：{current_id or '关闭'}"]
                lines.extend(f"- {item.name} ({item.id})" for item in personas)
                return "可用人格：\n" + ("\n".join(lines) if personas else "暂无已保存人格")
        if text == "/persona off" or text.startswith("/persona use "):
            if external_id.startswith("group:") and not is_owner(user_id):
                return "只有 Owner 可以切换群聊人格。"
            requested = None if text == "/persona off" else text.removeprefix("/persona use ").strip()
            if text != "/persona off" and not requested:
                return "用法：/persona use <人格名称或ID>，或 /persona off。"
            with SessionLocal.begin() as session:
                persona = None
                if requested is not None:
                    persona = session.scalar(
                        select(Persona).where((Persona.id == requested) | (Persona.name == requested))
                    )
                    if persona is None:
                        return "找不到该人格，请先使用 /persona list 查看。"
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
                        model_alias=model_registry.default_alias(),
                        owner_id=user_id,
                    )
                    session.add(conversation)
                    session.flush()
                if requested is not None:
                    assert persona is not None
                    conversation.persona_id = persona.id
                    return f"当前 QQ 会话已启用人格：{persona.name}。"
                conversation.persona_id = None
                return "当前 QQ 会话已关闭人格。"
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
                        model_alias=model_registry.default_alias(),
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
            scope_type = "group" if external_id.startswith("group:") else "user"
            scope_id = external_id if scope_type == "group" else user_id
            with SessionLocal() as session:
                items = session.scalars(
                    select(Memory).where(
                        Memory.scope_type == scope_type,
                        Memory.user_id == scope_id,
                        Memory.status == "active",
                    )
                ).all()
                return "当前记忆：\n" + (
                    "\n".join(f"- {item.id}: {item.content}" for item in items) or "暂无"
                )
        if text.startswith("/memory delete "):
            memory_id = text.removeprefix("/memory delete ").strip()
            scope_type = "group" if external_id.startswith("group:") else "user"
            scope_id = external_id if scope_type == "group" else user_id
            if scope_type == "group" and not is_owner(user_id):
                return "只有 Owner 可以删除群聊记忆。"
            with SessionLocal.begin() as session:
                item = session.get(Memory, memory_id)
                if item is None or item.scope_type != scope_type or item.user_id != scope_id:
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
        if job.type == COMPANION_ANALYSIS_RETRY_JOB_TYPE:
            if job.status == "succeeded" and job.result:
                result = json.loads(job.result)
                risk_level = result.get("risk_level")
                safety_mode = result.get("safety_mode", "standard")
                next_action = result.get("next_action")
                if (
                    safety_mode == "standard"
                    and risk_level in {"high", "critical"}
                    and next_action == "safety_redirect"
                    and job.requester_id.isdigit()
                    and is_owner(job.requester_id)
                ):
                    await self.send_text(
                        job.requester_id,
                        safety_redirect_text("critical" if risk_level == "critical" else "high"),
                    )
            return
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
