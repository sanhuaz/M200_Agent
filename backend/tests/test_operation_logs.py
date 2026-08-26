from __future__ import annotations

import json

from app.services.operation_logs import scrub_text, scrub_value


def test_log_scrubbing_removes_credentials_but_keeps_safe_details() -> None:
    text = "api_key=sk-test-secret authorization: Bearer abc123 password='hidden'"
    cleaned = scrub_text(text)
    assert "sk-test-secret" not in cleaned
    assert "abc123" not in cleaned
    assert "hidden" not in cleaned
    value = scrub_value(
        {"token": "secret", "qq": "123456", "nested": {"api_key": "x", "apiKey": "y", "napcatToken": "z"}}
    )
    assert value["token"] == "***"
    assert value["qq"] == "123456"
    assert value["nested"]["api_key"] == "***"
    assert value["nested"]["apiKey"] == "***"
    assert value["nested"]["napcatToken"] == "***"
    assert scrub_value(b"binary payload") == "[二进制内容已省略]"


def test_operation_lifecycle_persists_and_restores(tmp_path) -> None:
    # 通过环境变量构造真实实例，避免污染项目运行会话。
    import os

    previous_session = os.environ.get("PERSONAL_AGENT_LOG_SESSION_ID")
    previous_dir = os.environ.get("PERSONAL_AGENT_LOG_DIR")
    os.environ["PERSONAL_AGENT_LOG_SESSION_ID"] = "test-session"
    os.environ["PERSONAL_AGENT_LOG_DIR"] = str(tmp_path / "test-session")
    try:
        from app.services.operation_logs import OperationLogCenter as Center

        first = Center()
        operation_id = first.start_operation(source="test", title="测试操作", details={"api_key": "secret"})
        first.update_operation(
            operation_id,
            source="test",
            title="测试进度",
            progress={"current": 1, "total": 2, "percent": 50, "unit": "项"},
        )
        assert len(first.active_operations()) == 1
        first.finish_operation(operation_id, source="test", title="测试完成")
        assert first.active_operations() == []
        first.close()

        second = Center()
        events = second.snapshot(limit=20)
        assert any(item["title"] == "测试完成" for item in events)
        assert all("secret" not in json.dumps(item, ensure_ascii=False) for item in events)
        second.close()
    finally:
        if previous_session is None:
            os.environ.pop("PERSONAL_AGENT_LOG_SESSION_ID", None)
        else:
            os.environ["PERSONAL_AGENT_LOG_SESSION_ID"] = previous_session
        if previous_dir is None:
            os.environ.pop("PERSONAL_AGENT_LOG_DIR", None)
        else:
            os.environ["PERSONAL_AGENT_LOG_DIR"] = previous_dir


def test_event_is_bounded_and_marked_when_payload_is_large(tmp_path, monkeypatch) -> None:
    from app.services.operation_logs import MAX_EVENT_BYTES, OperationLogCenter

    monkeypatch.setenv("PERSONAL_AGENT_LOG_SESSION_ID", "bounded-session")
    monkeypatch.setenv("PERSONAL_AGENT_LOG_DIR", str(tmp_path / "bounded"))
    center = OperationLogCenter()
    event = center.emit(
        source="s" * 20_000,
        title="t" * 20_000,
        message="m" * 20_000,
        details={"payload": "x" * 200_000},
    )
    assert event["details"]["truncated"] is True
    assert len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= MAX_EVENT_BYTES
    center.close()
