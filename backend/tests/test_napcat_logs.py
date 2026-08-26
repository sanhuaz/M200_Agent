from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from app.services.napcat_logs import NapCatLogConnector, validate_webui_url


def test_napcat_url_requires_loopback_and_valid_port() -> None:
    assert validate_webui_url("http://127.0.0.1:6099/") == "http://127.0.0.1:6099"
    assert validate_webui_url("http://[::1]:6099") == "http://[::1]:6099"
    with pytest.raises(ValueError):
        validate_webui_url("https://example.com")
    with pytest.raises(ValueError):
        validate_webui_url("http://127.0.0.1:not-a-port")


def test_draft_connection_uses_mock_login_and_does_not_expose_credential() -> None:
    async def run() -> None:
        connector = NapCatLogConnector()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/login":
                payload = json.loads(request.content)
                assert payload["hash"] == hashlib.sha256(b"draft-secret.napcat").hexdigest()
                return httpx.Response(200, json={"Credential": "mock-credential"})
            assert request.url.path == "/api/Log/GetLogRealTime"
            assert request.headers["Authorization"] == "Bearer mock-credential"
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'data: {"level":"info","message":"mock log"}\n\n',
            )

        connector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await connector.test_connection("http://127.0.0.1:6099", " draft-secret ")
            assert result["ok"] is True
            assert connector._credential == ""
            assert "mock-credential" not in json.dumps(connector.public_status())
        finally:
            await connector._client.aclose()

    asyncio.run(run())
