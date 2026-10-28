"""Memory candidate HTTP 路由 + approve → user_memory write-through."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import aiosqlite
import httpx2
import pytest
from bowhead_core.memory_notes import MAX_CONTENT_CHARS, MemoryNotesStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import UserMemoryStore
from bowhead_memory import MemoryCandidateStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def app_ctx(tmp_path: Path):
    db = tmp_path / "t.db"
    mem = UserMemoryStore(db)
    await mem.initialize()
    cs = MemoryCandidateStore(db_path=str(db))
    await cs.initialize()
    notes = MemoryNotesStore(db)
    await notes.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.user_memory_store = mem
    app.state.memory_candidate_store = cs
    app.state.memory_notes_store = notes
    app.state.orchestrator = None
    app.state.datarc = None
    return app


@pytest.mark.integration
async def test_list_empty(app_ctx) -> None:
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.get("/api/v1/memory/candidates")
        assert r.status_code == 200
        assert r.json()["items"] == []
        assert r.headers["cache-control"] == "no-store, private"


@pytest.mark.integration
async def test_list_returns_server_owned_source_and_target_contract(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user

    class _UserStore:
        async def list_workspaces_for_user(self, user_id: str):
            assert user_id == uid
            return [
                {
                    "workspace_id": "ws_retail",
                    "name": "零售分析",
                },
            ]

    app_ctx.state.user_store = _UserStore()
    cand = await cs.enqueue(
        user_id=uid,
        kind="note",
        key="",
        value="我负责东南亚零售业务",
        reason="这是一项稳定背景",
        source_turn_id="t1",
        workspace_id="ws_retail",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        response = await client.get("/api/v1/memory/candidates")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == cand.id
    assert item["source_space"] == {
        "type": "space",
        "space_id": "ws_retail",
        "space_name": "零售分析",
        "availability": "available",
        "label": "仅供你使用 · 零售分析",
        "requires_confirmation": False,
    }
    assert item["default_target"] == item["allowed_targets"][0]
    assert [target["type"] for target in item["allowed_targets"]] == [
        "space",
        "global",
    ]
    assert item["display_value"] == "我负责东南亚零售业务"
    assert item["display_reason"] == "这是一项稳定背景"
    assert item["canonical_state"] == "pending"
    assert item["capabilities"]["approve"] is True
    assert item["version"] == "1"
    assert item["expires_at"] is not None


@pytest.mark.integration
async def test_display_reason_names_the_conflicting_memory_instead_of_its_id(app_ctx) -> None:
    """116 1.5.19: the suggestion reason read 「…与已有记忆 mem_7c729bd92a5c 矛盾」 (a row written before the fix)."""
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    uid = app_ctx.state.settings.dev_default_user
    existing = await mem.upsert(user_id=uid, kind="preference", key="gmv_definition", value="GMV 指含税成交额")
    await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="gmv_definition",
        value="GMV 指不含税成交额",
        reason=f"与已有记忆 {existing.id} 矛盾，用户改口；另一条 mem_000000aaaaaa 已不存在",
        source_turn_id="t1",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/api/v1/memory/candidates")

    assert response.status_code == 200
    display_reason = response.json()["items"][0]["display_reason"]
    assert "mem_" not in display_reason
    assert display_reason == "与已有记忆「GMV 指含税成交额」矛盾，用户改口；另一条已不存在"


@pytest.mark.integration
async def test_display_reason_does_not_resurface_a_deleted_memory(app_ctx) -> None:
    """A trashed memory's text must not come back through a suggestion reason; the id is still never shown."""
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    uid = app_ctx.state.settings.dev_default_user
    existing = await mem.upsert(user_id=uid, kind="preference", key="secret_pref", value="不要再显示的旧偏好")
    assert await mem.soft_delete(user_id=uid, entry_id=existing.id)
    await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="secret_pref",
        value="新的偏好",
        reason=f"与已有记忆{existing.id}矛盾",
        source_turn_id="t1",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/api/v1/memory/candidates")

    display_reason = response.json()["items"][0]["display_reason"]
    assert display_reason == "与已有记忆矛盾"


@pytest.mark.integration
async def test_approve_writes_through(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    uid = app_ctx.state.settings.dev_default_user

    cand = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="prefer_qoq",
        value="always show QoQ",
        reason="user said",
        source_turn_id="t1",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/approve", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    entries = await mem.list_for_user(uid)
    entry = next(e for e in entries if e.key == "prefer_qoq")
    assert entry.memory_role == "product_memory"
    assert entry.memory_policy_version == 1
    decided = await cs.get(cand.id)
    assert decided is not None
    assert decided.version == 2
    assert decided.decision_kind == "user_approved"
    assert decided.approved_resource_type == "memory_entry"
    assert decided.approved_resource_id == entry.id


@pytest.mark.integration
async def test_approve_passes_through_workspace_id(app_ctx) -> None:
    """memory-revive S2 A3: 候选的 workspace_id 转正落 entries 时不再丢（同轴直传）。"""
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    uid = app_ctx.state.settings.dev_default_user

    cand = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="spaced_pref",
        value="v",
        reason=None,
        source_turn_id="t1",
        workspace_id="ws_retail",
    )
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/approve", json={})
        assert r.status_code == 200

    entry = next(e for e in await mem.list_for_user(uid) if e.key == "spaced_pref")
    assert entry.workspace_id == "ws_retail"
    # Recall only within that space too (hard-boundary filter).
    assert any(e.key == "spaced_pref" for e in await mem.list_for_user(uid, workspace_id="ws_retail"))
    assert not any(e.key == "spaced_pref" for e in await mem.list_for_user(uid, workspace_id="ws_other"))


@pytest.mark.integration
async def test_reject(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="k",
        value="v",
        reason=None,
        source_turn_id=None,
    )
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/reject", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    entries = await app_ctx.state.user_memory_store.list_for_user(uid)
    assert all(e.key != "k" for e in entries)


# ─── review-fix batch (memory-revive S1 three-perspective review) ──


@pytest.mark.integration
async def test_approve_write_failure_keeps_candidate_pending(app_ctx) -> None:
    """Target failure rolls back the candidate decision."""
    cs = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user
    notes: MemoryNotesStore = app_ctx.state.memory_notes_store
    await notes.upsert_content(user_id=uid, content="x" * MAX_CONTENT_CHARS)
    cand = await cs.enqueue(user_id=uid, kind="note", key="", value="要记的事", reason=None, source_turn_id="t1")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/approve?target=notes", json={})
        assert r.status_code == 409

    got = await cs.get(cand.id)
    assert got is not None and got.status == "pending"


@pytest.mark.integration
async def test_decide_resolves_badge_when_pending_cleared(app_ctx) -> None:
    """P1: pending 清零时解 memory.candidate 通知条目（badge 不许永久红）。"""
    cs = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user

    resolved: list[str] = []

    class _FakeNotify:
        async def resolve_memory_candidates(self, *, user_id):
            resolved.append(user_id)

    app_ctx.state.notification_service = _FakeNotify()
    cand = await cs.enqueue(user_id=uid, kind="preference", key="k", value="v", reason=None, source_turn_id="t1")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/reject", json={})
        assert r.status_code == 200
    assert resolved == [uid]


@pytest.mark.integration
async def test_decide_no_resolve_while_pending_remain(app_ctx) -> None:
    cs = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user

    resolved: list[str] = []

    class _FakeNotify:
        async def resolve_memory_candidates(self, *, user_id):
            resolved.append(user_id)

    app_ctx.state.notification_service = _FakeNotify()
    c1 = await cs.enqueue(user_id=uid, kind="preference", key="k1", value="v1", reason=None, source_turn_id="t1")
    await cs.enqueue(user_id=uid, kind="preference", key="k2", value="v2", reason=None, source_turn_id="t1")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        await c.post(f"/api/v1/memory/candidates/{c1.id}/reject", json={})
    assert resolved == []  # pending remains; retain badge.


@pytest.mark.integration
async def test_bulk_approve_routes_note_kind_to_notes(app_ctx) -> None:
    """review-fix: bulk approve 按 kind 分流——note 落 notes 而非塞进 memory_entries。"""
    cs = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(user_id=uid, kind="note", key="", value="老板叫张总", reason=None, source_turn_id="t1")

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        r = await c.post(
            "/api/v1/memory/candidates/bulk-decide",
            json={"cids": [cand.id], "action": "approve"},
        )
        assert r.status_code == 200
        assert r.json()["succeeded"] == 1
    note = await app_ctx.state.memory_notes_store.get(user_id=uid)
    assert note is not None
    assert note.content == "• 老板叫张总"
    got = await cs.get(cand.id)
    assert got is not None and got.status == "approved"


@pytest.mark.integration
async def test_space_note_candidate_keeps_source_scope(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(
        user_id=uid,
        kind="note",
        key="",
        value="我负责东南亚零售业务",
        reason=None,
        source_turn_id="t1",
        workspace_id="ws_retail",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        response = await client.post(
            f"/api/v1/memory/candidates/{cand.id}/approve?target=notes",
            json={},
        )

    assert response.status_code == 200
    assert await app_ctx.state.memory_notes_store.get(user_id=uid) is None
    entries = await mem.list_for_user(uid)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "note"
    assert entry.key.startswith("note_")
    assert entry.parsed_value() == "我负责东南亚零售业务"
    assert entry.workspace_id == "ws_retail"
    assert entry.memory_role == "product_memory"
    assert entry.value_schema == "note_text_v1"
    decided = await cs.get(cand.id)
    assert decided is not None
    assert decided.approved_resource_id == entry.id


@pytest.mark.integration
async def test_note_candidate_can_explicitly_upgrade_to_global_about_you(
    app_ctx,
) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(
        user_id=uid,
        kind="note",
        key="",
        value="我负责东南亚零售业务",
        reason=None,
        source_turn_id="t1",
        workspace_id="ws_retail",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        response = await client.post(
            f"/api/v1/memory/candidates/{cand.id}/approve",
            json={
                "target": {
                    "type": "global",
                    "space_id": None,
                },
            },
        )

    assert response.status_code == 200
    note = await app_ctx.state.memory_notes_store.get(user_id=uid)
    assert note is not None
    assert note.content == "• 我负责东南亚零售业务"
    assert await app_ctx.state.user_memory_store.list_for_user(uid) == []


@pytest.mark.integration
async def test_structured_candidate_cannot_upgrade_to_global(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(
        user_id=uid,
        kind="constraint",
        key="report_order",
        value="先结论后依据",
        reason=None,
        source_turn_id="t1",
        workspace_id="ws_retail",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://gw",
    ) as client:
        response = await client.post(
            f"/api/v1/memory/candidates/{cand.id}/approve",
            json={
                "target": {
                    "type": "global",
                },
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "memory_target_not_allowed"
    assert (await cs.get(cand.id)).status == "pending"


@pytest.mark.integration
async def test_approve_is_atomic_when_candidate_update_fails(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(
        user_id=uid,
        kind="preference",
        # ⚠ Do not use `*_key` as key: write gate defaults on since 2026-09-04, and `atomic_key=<value>`
        # matches criterion ④'s credential shape ⇒ contains_secret (422); this case tests atomicity, not the gate.
        key="atomic_pref",
        value="must_not_persist",
        reason=None,
        source_turn_id="t1",
    )
    async with aiosqlite.connect(cs.db_path) as db:
        await db.executescript(
            """
            CREATE TRIGGER fail_candidate_approval
            BEFORE UPDATE OF status ON memory_candidates
            WHEN NEW.status = 'approved'
            BEGIN
                SELECT RAISE(ABORT, 'injected candidate failure');
            END;
            """,
        )
        await db.commit()

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            f"/api/v1/memory/candidates/{cand.id}/approve",
            json={},
        )
    assert response.status_code == 503
    assert (await cs.get(cand.id)).status == "pending"
    assert all(entry.key != "atomic_pref" for entry in await mem.list_for_user(uid))


@pytest.mark.integration
async def test_approve_rejects_silent_cross_space_move(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    uid = app_ctx.state.settings.dev_default_user
    await mem.upsert(
        user_id=uid,
        kind="preference",
        key="sales",
        value="space-a",
        workspace_id="ws_a",
    )
    cand = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="sales",
        value="space-b",
        reason=None,
        source_turn_id="t1",
        workspace_id="ws_b",
    )

    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            f"/api/v1/memory/candidates/{cand.id}/approve",
            json={},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "memory_scope_conflict"
    entry = next(entry for entry in await mem.list_for_user(uid) if entry.key == "sales")
    assert entry.workspace_id == "ws_a"
    assert entry.parsed_value() == "space-a"
    assert (await cs.get(cand.id)).status == "pending"


@pytest.mark.integration
async def test_concurrent_note_approval_appends_once(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    uid = app_ctx.state.settings.dev_default_user
    cand = await cs.enqueue(
        user_id=uid,
        kind="note",
        key="",
        value="每周报告必须带环比",
        reason=None,
        source_turn_id="t1",
    )
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as client:
        responses = await asyncio.gather(
            client.post(
                f"/api/v1/memory/candidates/{cand.id}/approve?target=notes",
                json={},
            ),
            client.post(
                f"/api/v1/memory/candidates/{cand.id}/approve?target=notes",
                json={},
            ),
        )
    assert [response.status_code for response in responses] == [200, 200]
    note = await app_ctx.state.memory_notes_store.get(user_id=uid)
    assert note is not None
    assert note.content.splitlines() == ["• 每周报告必须带环比"]
