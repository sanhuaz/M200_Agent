from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import httpx

from app.core.config import PROJECT_ROOT
from app.services.env_file import write_env_value
from app.services.operation_logs import operation_logs, scrub_text

logger = logging.getLogger(__name__)
DEFAULT_WEBUI_URL = "http://127.0.0.1:6099"
WEBUI_URL_ENV = "NAPCAT_WEBUI_URL"
WEBUI_TOKEN_ENV = "NAPCAT_WEBUI_TOKEN"


def validate_webui_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("NapCat WebUI 地址必须是 HTTP/HTTPS 回环地址")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("NapCat WebUI 地址端口无效") from error
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("NapCat WebUI 地址端口无效")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("NapCat WebUI 地址不能包含凭据、查询参数或片段")
    return url


def _level(value: object) -> str:
    text = str(value or "info").casefold()
    return {"warning": "warn", "log": "info", "critical": "error"}.get(text, text)


class NapCatLogConnector:
    """NapCat WebUI realtime log bridge. Credentials never leave this process."""

    def __init__(self) -> None:
        import os

        self.url = validate_webui_url(os.getenv(WEBUI_URL_ENV, DEFAULT_WEBUI_URL))
        self._token = os.getenv(WEBUI_TOKEN_ENV, "")
        self._credential = ""
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()
        self.status = "not_configured" if not self._token else "disconnected"
        self.last_error: str | None = None
        self.two_factor = False
        self.last_log_at: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def public_status(self) -> dict[str, object]:
        return {
            "url": self.url,
            "configured": self.configured,
            "status": self.status,
            "two_factor": self.two_factor,
            "last_error": self.last_error,
            "last_log_at": self.last_log_at,
        }

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        if not self._token:
            self.status = "not_configured"
            return
        self._task = asyncio.create_task(self._run(), name="napcat-log-connector")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._credential = ""
        self.status = "stopped"

    async def _client_for(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=None), follow_redirects=False)
        return self._client

    async def _login(self, url: str, token: str) -> str:
        digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
        response = await (await self._client_for()).post(f"{url}/api/auth/login", json={"hash": digest})
        if response.status_code >= 400:
            raise RuntimeError(f"NapCat 登录失败（HTTP {response.status_code}）")
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError("NapCat 登录响应不是有效 JSON") from error
        if payload.get("require2FA") or payload.get("Require2FA"):
            self.two_factor = True
            raise RuntimeError("NapCat 已启用 2FA，日志中心不支持自动续期")
        credential = payload.get("Credential") or payload.get("credential")
        if not isinstance(credential, str) or not credential:
            raise RuntimeError("NapCat 登录响应缺少有效凭据")
        self.two_factor = False
        return credential

    async def _iter_sse(self, credential: str) -> AsyncIterator[dict[str, object]]:
        client = await self._client_for()
        headers = {"Authorization": f"Bearer {credential}", "Accept": "text/event-stream"}
        async with client.stream("GET", f"{self.url}/api/Log/GetLogRealTime", headers=headers) as response:
            if response.status_code in {401, 403}:
                raise PermissionError("NapCat 日志接口认证失败")
            response.raise_for_status()
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif not line.strip() and data_lines:
                    raw = "\n".join(data_lines)
                    data_lines = []
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        value = {"message": raw}
                    if isinstance(value, dict):
                        yield {str(key): item for key, item in value.items()}

    async def _run(self) -> None:
        delay = 1
        while self._stop is not None and not self._stop.is_set():
            try:
                self.status = "connecting"
                self.last_error = None
                self._credential = await self._login(self.url, self._token)
                self.status = "connected"
                delay = 1
                operation_logs.emit(
                    source="napcat", kind="started", title="NapCat 日志流", message="已连接 NapCat 实时日志"
                )
                async for payload in self._iter_sse(self._credential):
                    message = str(payload.get("message") or payload.get("msg") or payload)
                    self.last_log_at = operation_logs.emit(
                        source="napcat",
                        level=_level(payload.get("level")),
                        kind="message",
                        title="NapCat",
                        message=message,
                        details={"raw": scrub_text(message)},
                    )["timestamp"]
                    if self._stop is None or self._stop.is_set():
                        break
                if self._stop is not None and not self._stop.is_set():
                    raise ConnectionError("NapCat 日志流已断开")
            except asyncio.CancelledError:
                raise
            except PermissionError as error:
                self.status = "auth_failed"
                self.last_error = str(error)
                operation_logs.emit(
                    source="napcat",
                    level="error",
                    kind="failed",
                    title="NapCat 日志认证失败",
                    message=str(error),
                )
                delay = min(delay * 2, 30)
            except Exception as error:
                self.status = "error"
                self.last_error = f"{type(error).__name__}: {error}"
                logger.warning("NapCat 日志流断开：%s", type(error).__name__)
                operation_logs.emit(
                    source="napcat",
                    level="warn",
                    kind="failed",
                    title="NapCat 日志流断开",
                    message=self.last_error,
                )
                delay = min(delay * 2, 30)
            if self._stop is not None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def test_connection(self, url: str | None = None, token: str | None = None) -> dict[str, object]:
        target = validate_webui_url(url or self.url)
        secret = (token if token is not None else self._token).strip()
        if not secret:
            raise ValueError("请提供 NapCat WebUI Token")
        previous_credential, previous_two_factor = self._credential, self.two_factor
        try:
            credential = await self._login(target, secret)
            client = await self._client_for()
            async with client.stream(
                "GET",
                f"{target}/api/Log/GetLogRealTime",
                headers={"Authorization": f"Bearer {credential}", "Accept": "text/event-stream"},
                timeout=5.0,
            ) as response:
                if response.status_code in {401, 403}:
                    raise RuntimeError("NapCat 日志接口认证失败")
                response.raise_for_status()
                status_code = response.status_code
            return {"ok": True, "status": status_code, "message": "NapCat 登录和日志接口可访问"}
        finally:
            self._credential, self.two_factor = previous_credential, previous_two_factor

    async def configure(
        self, url: str | None, token: str | None, clear_token: bool = False
    ) -> dict[str, object]:
        target = validate_webui_url(url or self.url)
        if clear_token:
            new_token = ""
        elif token is None:
            new_token = self._token
        else:
            new_token = token.strip()
        if new_token and (target != self.url or new_token != self._token):
            await self.test_connection(target, new_token)
        old_url, old_token = self.url, self._token
        env_path = PROJECT_ROOT / ".env"
        previous_env_bytes = env_path.read_bytes() if env_path.exists() else None
        try:
            write_env_value(env_path, WEBUI_URL_ENV, target)
            write_env_value(env_path, WEBUI_TOKEN_ENV, new_token)
        except Exception:
            if previous_env_bytes is None:
                env_path.unlink(missing_ok=True)
            else:
                env_path.write_bytes(previous_env_bytes)
            import os

            if old_url:
                os.environ[WEBUI_URL_ENV] = old_url
            if old_token:
                os.environ[WEBUI_TOKEN_ENV] = old_token
            else:
                os.environ.pop(WEBUI_TOKEN_ENV, None)
            self.url, self._token = old_url, old_token
            raise
        self.url, self._token = target, new_token
        await self.stop()
        self.status = "not_configured" if not self._token else "disconnected"
        await self.start()
        return self.public_status()

    async def clear(self) -> dict[str, object]:
        return await self.configure(self.url, "", clear_token=True)


napcat_connector = NapCatLogConnector()
