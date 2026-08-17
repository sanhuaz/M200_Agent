from __future__ import annotations

import io
import json
import shutil
import zipfile

import pytest
from app.db.models import ExtensionPackage, Memory
from app.db.session import SessionLocal
from app.services.artifacts import ArtifactError, create_artifact
from app.services.chat import normalize_tool_result
from app.services.confirmations import create_extension_confirmation, resolve_confirmation
from app.services.extensions import ExtensionError, github_zip_url, import_zip
from app.services.memories import MemoryService


def test_tool_result_parser_handles_array_and_invalid_text() -> None:
    parsed_array = normalize_tool_result("[1, 2]")
    assert parsed_array["data"] == {"items": [1, 2]}
    parsed_text = normalize_tool_result("manga provider returned a list")
    assert parsed_text["data"] == {"raw": "manga provider returned a list"}
    parsed_dict = normalize_tool_result({"pending_confirmation": True})
    assert parsed_dict["pending_confirmation"] is True


def test_artifact_path_and_limits() -> None:
    with SessionLocal() as session:
        with pytest.raises(ArtifactError):
            create_artifact(
                session,
                owner_id="qq-user",
                conversation_id=None,
                files=[("../secret.py", b"no")],
            )
        item = create_artifact(
            session,
            owner_id="qq-user",
            conversation_id=None,
            files=[("main.py", b"print('ok')"), ("README.md", b"# demo")],
        )
        assert item.filename.endswith(".zip")
        assert "/workspace/generated/qq-user/" in item.path.replace("\\", "/")


def test_skill_zip_validation_and_disabled_import() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "demo-skill-v020/SKILL.md",
            "---\nname: demo-skill-v020\ndescription: A safe test skill\n---\nUse only for tests.",
        )
        archive.writestr("demo-skill-v020/references/example.txt", "reference")
    with SessionLocal() as session:
        item = import_zip(session, "skill", buffer.getvalue(), "demo-skill-v020.zip")
        assert item.enabled is False
        assert item.status == "installed_disabled"
        install_path = item.install_path
        session.delete(item)
        session.commit()
    shutil.rmtree(install_path, ignore_errors=True)

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    with SessionLocal() as session:
        with pytest.raises(ExtensionError):
            import_zip(session, "skill", unsafe.getvalue(), "unsafe.zip")


def test_memory_scope_isolated_between_users(monkeypatch) -> None:
    class Provider:
        def embed_query(self, _query: str) -> list[float]:
            return [1.0]

    monkeypatch.setattr("app.services.memories.get_embedding_provider", lambda _profile: Provider())
    monkeypatch.setattr(
        "app.services.memories.vector_store.query",
        lambda collection, _embedding, _limit: (
            [{"id": "global-memory"}, {"id": "alice-memory"}]
            if "alice" in collection or "global" in collection
            else []
        ),
    )
    with SessionLocal() as session:
        session.add_all(
            [
                Memory(
                    id="global-memory",
                    scope_type="global",
                    user_id=None,
                    fact_key="policy.language",
                    content="全局使用中文",
                ),
                Memory(
                    id="alice-memory",
                    scope_type="user",
                    user_id="alice",
                    fact_key="preference.color",
                    content="Alice 喜欢蓝色",
                ),
                Memory(
                    id="bob-memory",
                    scope_type="user",
                    user_id="bob",
                    fact_key="preference.color",
                    content="Bob 喜欢绿色",
                ),
            ]
        )
        session.commit()
        alice = MemoryService(session).recall("alice", "喜欢")
        assert {item.id for item in alice} == {"global-memory", "alice-memory"}


def test_github_source_is_restricted_and_supports_subdirectory() -> None:
    download_url, source_ref, subpath = github_zip_url("https://github.com/acme/demo/tree/main/skills/demo")
    assert download_url.endswith("/acme/demo/zip/refs/heads/main")
    assert source_ref == "acme/demo@main"
    assert subpath == ("skills", "demo")
    with pytest.raises(ExtensionError):
        github_zip_url("http://example.com/acme/demo")


def test_extension_management_requires_confirmation() -> None:
    with SessionLocal.begin() as session:
        session.add(
            ExtensionPackage(
                kind="tool",
                name="confirm-tool-v020",
                version="0.1",
                description="confirmation test",
                source_type="local",
                sha256="1" * 64,
                install_path="test-tool-path",
                manifest="{}",
                permissions="[]",
                access_policy="owner_only",
                status="installed_disabled",
                enabled=False,
                builtin=False,
            )
        )
    confirmation = create_extension_confirmation(
        "tool", "enable", "confirm-tool-v020", "local-owner", None
    )
    resolved, job = resolve_confirmation(confirmation.token, "local-owner", True)
    assert job is None
    assert resolved.status == "approved"
    assert json.loads(resolved.payload)["result"]["status"] == "ready"
