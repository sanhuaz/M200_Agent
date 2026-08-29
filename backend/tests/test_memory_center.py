from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db.models import AdminIdentity, Memory, Persona, RelationshipProfile
from app.db.session import SessionLocal
from app.main import app
from app.services.memories import memory_collection_name
from app.services.persona_store import get_persona_store
from app.services.personas import PersonaCard
from fastapi.testclient import TestClient


def _token() -> str:
    return f"memory-center-{uuid4().hex}"


def test_memory_center_unifies_filters_and_hides_non_owner_relationships() -> None:
    token = _token()
    owner_id = "10002"
    group_id = "group:90001"
    persona_id = f"center-persona-{uuid4().hex[:16]}"
    persona_name = f"中心人格-{uuid4().hex}"
    persona = Persona(id=persona_id, name=persona_name, raw_prompt="", card_json='{}')
    persona_store = get_persona_store()
    persona_store.write(
        persona_store.envelope_for(
            persona_id, persona_name, PersonaCard(identity={"role": "测试人格"})
        )
    )
    with SessionLocal.begin() as session:
        session.add(AdminIdentity(platform="qq", external_id=owner_id, enabled=True, created_by="test"))
        session.add(persona)
        session.flush()
        now = datetime.now(UTC).replace(tzinfo=None)
        session.add_all(
            [
                Memory(
                    scope_type="global", user_id=None, fact_key=f"global.{token}", content=f"全局 {token}"
                ),
                Memory(
                    scope_type="user", user_id=owner_id, fact_key=f"user.{token}", content=f"用户 {token}"
                ),
                Memory(
                    scope_type="group", user_id=group_id, fact_key=f"group.{token}", content=f"群组 {token}"
                ),
                Memory(
                    scope_type="user",
                    user_id=owner_id,
                    fact_key=f"archived.{token}",
                    content=f"归档 {token}",
                    status="archived",
                    last_seen_at=now - timedelta(days=1),
                ),
                RelationshipProfile(
                    scope_id=owner_id,
                    persona_key=persona.id,
                    persona_id=persona.id,
                    nickname=f"称呼-{token}",
                    shared_summary=f"一起经历 {token}",
                    boundaries='{"items":["不催促"]}',
                    version=2,
                    updated_at=now + timedelta(seconds=1),
                ),
                RelationshipProfile(
                    scope_id="20002",
                    persona_key="default",
                    nickname="不应泄漏",
                    shared_summary=token,
                    version=1,
                ),
            ]
        )

    try:
        with TestClient(app) as client:
            response = client.get(f"/api/v1/memory-center?keyword={token}")
            assert response.status_code == 200
            rows = response.json()
            assert {row["memory_type"] for row in rows} == {"fact", "relationship"}
            assert all("不应泄漏" not in str(row) for row in rows)
            assert rows == sorted(rows, key=lambda row: row["updated_at"], reverse=True)
            relationship = next(row for row in rows if row["memory_type"] == "relationship")
            assert relationship["persona_name"] == persona.name
            assert relationship["boundaries"] == {"items": ["不催促"]}

            group_rows = client.get(
                f"/api/v1/memory-center?memory_type=fact&scope_type=group&scope_id={group_id}"
            ).json()
            assert len(group_rows) == 1
            assert group_rows[0]["scope_type"] == "group"
            assert group_rows[0]["scope_id"] == group_id

            archived_rows = client.get(
                f"/api/v1/memory-center?status=archived&keyword={token}"
            ).json()
            assert len(archived_rows) == 1
            assert archived_rows[0]["memory_type"] == "fact"

            relationship_rows = client.get(
                f"/api/v1/memory-center?memory_type=relationship&persona_key={persona.id}"
            ).json()
            assert len(relationship_rows) == 1
            assert relationship_rows[0]["scope_id"] == owner_id
    finally:
        with SessionLocal.begin() as session:
            session.query(RelationshipProfile).filter(
                RelationshipProfile.scope_id.in_([owner_id, "20002"])
            ).delete(synchronize_session=False)
            session.query(Memory).filter(Memory.fact_key.contains(token)).delete(
                synchronize_session=False
            )
            session.query(AdminIdentity).filter(AdminIdentity.external_id == owner_id).delete(
                synchronize_session=False
            )
            session.delete(session.get(Persona, persona.id))
        persona_store.delete(persona.id)


def test_group_memory_management_uses_runtime_collection(monkeypatch) -> None:
    memory_id = str(uuid4())
    group_id = "group:90002"
    with SessionLocal.begin() as session:
        session.add(
            Memory(
                id=memory_id,
                scope_type="group",
                user_id=group_id,
                fact_key=f"group-management-{memory_id}",
                content="原始群组事实",
            )
        )

    calls: list[tuple[str, str]] = []

    def fake_delete(collection: str, ids: list[str]) -> None:
        calls.append(("delete", collection))

    def fake_upsert(collection: str, *_args, **_kwargs) -> None:
        calls.append(("upsert", collection))

    class FakeProvider:
        def embed_documents(self, _texts):
            return [[0.1, 0.2]]

    from app.api import routes

    monkeypatch.setattr(routes.vector_store, "delete_ids", fake_delete)
    monkeypatch.setattr(routes.vector_store, "upsert_documents", fake_upsert)
    monkeypatch.setattr(routes, "get_embedding_provider", lambda _profile: FakeProvider())
    expected = memory_collection_name("group", group_id, routes.settings.default_embedding_profile)

    try:
        with TestClient(app) as client:
            updated = client.put(
                f"/api/v1/memories/{memory_id}", json={"content": "更新群组事实"}
            )
            assert updated.status_code == 200
            assert client.post(f"/api/v1/memories/{memory_id}/archive").status_code == 200
            assert client.post(f"/api/v1/memories/{memory_id}/restore").status_code == 200
            assert client.delete(f"/api/v1/memories/{memory_id}").status_code == 200
        assert calls == [
            ("upsert", expected),
            ("delete", expected),
            ("upsert", expected),
            ("delete", expected),
        ]
    finally:
        with SessionLocal.begin() as session:
            item = session.get(Memory, memory_id)
            if item is not None:
                session.delete(item)
