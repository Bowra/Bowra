"""V3 PRD-B · /api/v1/memory routes — comprehensive e2e (~30+ cases).

Covers:
  - GET /memory/snapshot (unified DTO)
  - GET /memory/trash
  - DELETE /memory/{entry_id} (unified, routes memory entries)
  - POST /memory/{entry_id}/restore
  - POST /memory/reset (with snapshot)
  - GET /memory/export (streaming JSON)
  - GET /memory/dreaming + POST /memory/dreaming/{pause,resume}
  - candidates ownership fix + status filter + audit/telemetry
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx2
import pytest
from bowhead_core.memory_note_anchors import MemoryNoteAnchorsStore
from bowhead_core.memory_notes import MemoryNotesStore
from bowhead_core.memory_privacy import (
    MemoryDestructiveOperationStore,
    PrivacyDeletionLedger,
)
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.personal_memory_governance import (
    PersonalMemoryGovernanceStore,
)
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from bowhead_memory.candidates import PauseStateCache

from bowhead_core import UserMemoryStore
from bowhead_memory import MemoryCandidateStore

if TYPE_CHECKING:
    from pathlib import Path

# ─── fixtures ────────────────────────────────────────────


@pytest.fixture
async def app_ctx(tmp_path: Path):
    db = tmp_path / "t.db"
    mem = UserMemoryStore(db)
    await mem.initialize()
    candidates = MemoryCandidateStore(db_path=str(db))
    await candidates.initialize()
    settings_store = UserSettingsStore(db)
    await settings_store.init()
    metrics = MetricsLog(db)
    await metrics.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()
    pause_cache = PauseStateCache(settings_store=settings_store, ttl_seconds=60.0)

    app = create_app()
    app.state.settings = Settings()
    app.state.user_memory_store = mem
    app.state.memory_candidate_store = candidates
    app.state.user_settings_store = settings_store
    app.state.metrics_log = metrics
    app.state.user_store = user_store
    app.state.pause_state_cache = pause_cache
    app.state.orchestrator = None
    app.state.datarc = None
    return app


def _client(app):
    transport = httpx2.ASGITransport(app=app)
    return httpx2.AsyncClient(transport=transport, base_url="http://gw")


def _hdrs(uid: str | None = None) -> dict[str, str]:
    return {"X-Bowhead-User-Id": uid} if uid else {}


def _idempotency_headers() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


async def _seed_memory(app, *, user_id: str, key: str, value: str, **kw) -> str:
    mem: UserMemoryStore = app.state.user_memory_store
    e = await mem.upsert(user_id=user_id, kind="preference", key=key, value=value, **kw)
    return e.id


async def _seed_product_memory(
    app,
    *,
    user_id: str,
    key: str,
    value: str,
    **kw,
) -> str:
    mem: UserMemoryStore = app.state.user_memory_store
    entry = await mem.upsert_product_memory(
        user_id=user_id,
        kind="preference",
        key=key,
        value=value,
        **kw,
    )
    return entry.id


async def _enable_memory_v2_stores(app) -> None:
    candidates: MemoryCandidateStore = app.state.memory_candidate_store
    notes = MemoryNotesStore(candidates.db_path)
    await notes.initialize()
    anchors = MemoryNoteAnchorsStore(candidates.db_path)
    await anchors.initialize()
    app.state.memory_notes_store = notes
    app.state.memory_note_anchors_store = anchors
    app.state.personal_memory_governance_store = PersonalMemoryGovernanceStore(candidates.db_path)


async def _enable_permanent_delete_store(
    app,
    tmp_path: Path,
) -> MemoryDestructiveOperationStore:
    ledger = PrivacyDeletionLedger(
        tmp_path / "privacy-ledger.db",
        hmac_key="test-ledger-key",
    )
    await ledger.initialize()
    store = MemoryDestructiveOperationStore(
        app.state.memory_candidate_store.db_path,
        ledger=ledger,
        fingerprint_key="test-operation-key",
        require_secure_delete_baseline=False,
    )
    await store.initialize()
    app.state.privacy_deletion_ledger = ledger
    app.state.memory_destructive_operation_store = store
    return store


# ─── /memory/capabilities ───────────────────────────────


@pytest.mark.integration
async def test_capabilities_expose_v2_when_readiness_passes(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)

    async with _client(app_ctx) as client:
        response = await client.get("/api/v1/memory/capabilities")

    assert response.status_code == 200
    assert response.json()["v2_enabled"] is True
    assert response.json()["v2_ready"] is True
    assert response.json()["presentation"] == "v2"
    assert response.json()["actions"]["edit"] is True
    assert response.json()["actions"]["permanent_delete"] is False
    assert response.headers["cache-control"] == "no-store, private"


@pytest.mark.integration
async def test_capabilities_fail_closed_for_unclassified_rows(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    await app_ctx.state.user_memory_store.upsert(
        user_id=uid,
        kind="preference",
        key="legacy",
        value="legacy",
    )

    async with _client(app_ctx) as client:
        capabilities = await client.get("/api/v1/memory/capabilities")
        overview = await client.get("/api/v1/memory/overview")

    assert capabilities.status_code == 200
    assert capabilities.json()["v2_enabled"] is True
    assert capabilities.json()["v2_ready"] is False
    assert capabilities.json()["presentation"] == "v2"
    assert capabilities.json()["actions"]["edit"] is False
    assert capabilities.json()["reason"] == "memory_policy_backfill_incomplete"
    assert overview.status_code == 503
    assert overview.json()["detail"] == {
        "code": "memory_v2_not_ready",
        "reason": "memory_policy_backfill_incomplete",
    }


@pytest.mark.integration
async def test_v2_overview_separates_product_and_operational_memory(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.upsert_product_memory(
        user_id=uid,
        kind="constraint",
        key="report_order",
        value="先结论后依据",
    )
    await mem.upsert_operational_memory(
        user_id=uid,
        kind="satisfaction",
        key="t1",
        value={"score": 5},
        source="turn_feedback",
    )
    await app_ctx.state.memory_notes_store.upsert_content(
        user_id=uid,
        content="我负责东南亚零售业务",
    )
    await app_ctx.state.memory_candidate_store.enqueue(
        user_id=uid,
        kind="preference",
        key="chart_style",
        value="默认使用折线图",
        reason="多次选择折线图",
        source_turn_id=None,
    )

    async with _client(app_ctx) as client:
        response = await client.get("/api/v1/memory/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["about_you"]["content"] == "我负责东南亚零售业务"
    assert body["stats"]["saved_entries"] == 1
    assert body["stats"]["notes_configured"] is True
    assert body["stats"]["pending_candidates"] == 1
    assert body["suggestions"][0]["display_value"] == "默认使用折线图"
    assert body["capabilities"]["presentation"] == "v2"
    assert body["about_you"]["capabilities"]["edit"] is True
    assert response.headers["cache-control"] == "no-store, private"


@pytest.mark.integration
async def test_v2_about_you_edit_uses_owner_lease_and_version_cas(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)

    async with _client(app_ctx) as client:
        lease = await client.post(
            "/api/v1/memory/about-you/edit-session",
            json={"expected_version": "0"},
        )
        blocked_append = await client.post(
            "/api/v1/memory/notes/append",
            json={"bullet": "must not win", "ignore_lock": True},
        )
        wrong_owner = await client.patch(
            "/api/v1/memory/about-you",
            json={
                "content": "wrong",
                "version": "0",
                "edit_token": "x" * 43,
            },
        )
        saved = await client.patch(
            "/api/v1/memory/about-you",
            json={
                "content": "我负责东南亚零售业务",
                "version": lease.json()["version"],
                "edit_token": lease.json()["edit_token"],
            },
        )
        stale = await client.post(
            "/api/v1/memory/about-you/edit-session",
            json={"expected_version": "0"},
        )

    assert lease.status_code == 200
    assert len(lease.json()["edit_token"]) >= 32
    assert lease.headers["cache-control"] == "no-store, private"
    assert blocked_append.status_code == 423
    assert blocked_append.json()["detail"]["code"] == "memory_about_you_editing"
    assert wrong_owner.status_code == 423
    assert saved.status_code == 200
    assert saved.json()["content"] == "我负责东南亚零售业务"
    assert saved.json()["version"] == "1"
    assert saved.json()["capabilities"]["edit"] is True
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_version"] == "1"


@pytest.mark.integration
async def test_v2_about_you_delete_is_independently_recoverable(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    await app_ctx.state.memory_notes_store.upsert_content(
        user_id=uid,
        content="📌 我负责东南亚零售业务",
    )
    await app_ctx.state.memory_note_anchors_store.insert_or_revive(
        user_id=uid,
        bullet_text="我负责东南亚零售业务",
        canvas_id="c_1",
        section_id="sec_1",
        verifier_result_hash="hash",
    )

    async with _client(app_ctx) as client:
        before = await client.get("/api/v1/memory/overview")
        deleted = await client.delete(
            "/api/v1/memory/about-you",
            headers={"If-Match": f'"{before.json()["about_you"]["version"]}"'},
        )
        recovery_id = deleted.json()["recovery_id"]
        after_delete = await client.get("/api/v1/memory/overview")
        trash = await client.get(
            "/api/v1/memory/items",
            params={"state": "trashed"},
        )
        restored = await client.post(f"/api/v1/memory/{recovery_id}/restore")
        after_restore = await client.get("/api/v1/memory/overview")

    assert deleted.status_code == 200
    assert recovery_id.startswith("snap_")
    assert after_delete.json()["about_you"]["content"] == ""
    assert after_delete.json()["stats"]["trash_count"] == 1
    assert after_delete.json()["stats"]["recoverable_resets"] == 0
    recovery = trash.json()["items"][0]
    assert recovery["id"] == recovery_id
    assert recovery["resource_type"] == "memory_note_recovery"
    assert recovery["display_value"] == "📌 我负责东南亚零售业务"
    assert recovery["capabilities"]["restore"] is True
    assert restored.status_code == 200
    assert after_restore.json()["about_you"]["content"] == "📌 我负责东南亚零售业务"
    assert (
        len(
            await app_ctx.state.memory_note_anchors_store.list_for_user(
                user_id=uid,
            ),
        )
        == 1
    )


@pytest.mark.integration
async def test_v2_about_you_restore_never_overwrites_newer_content(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    note = await app_ctx.state.memory_notes_store.upsert_content(
        user_id=uid,
        content="旧版本",
    )

    async with _client(app_ctx) as client:
        deleted = await client.delete(
            "/api/v1/memory/about-you",
            headers={"If-Match": f'"{note.version}"'},
        )
        await app_ctx.state.memory_notes_store.upsert_content(
            user_id=uid,
            content="清空后创建的新版本",
        )
        restore = await client.post(
            f"/api/v1/memory/{deleted.json()['recovery_id']}/restore",
        )
        overview = await client.get("/api/v1/memory/overview")
        trash = await client.get(
            "/api/v1/memory/items",
            params={"state": "trashed"},
        )

    assert restore.status_code == 409
    assert restore.json()["detail"]["code"] == "memory_restore_conflict"
    assert overview.json()["about_you"]["content"] == "清空后创建的新版本"
    assert len(trash.json()["items"]) == 1


@pytest.mark.integration
async def test_v2_items_return_canonical_content_without_raw_key(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    entry = await app_ctx.state.user_memory_store.upsert_product_memory(
        user_id=uid,
        kind="constraint",
        key="internal_key_must_not_leak",
        value="先结论后依据",
        workspace_id="ws_inaccessible",
    )

    async with _client(app_ctx) as client:
        response = await client.get("/api/v1/memory/items")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == entry.id
    assert item["display_type"] == "work_rule"
    assert item["display_value"] == "先结论后依据"
    assert item["display_schema"]["payload"] == {
        "content": "先结论后依据",
    }
    assert item["scope"] == {
        "type": "space",
        "space_id": None,
        "space_name": None,
        "availability": "unavailable",
        "label": "仅供你使用 · 不可用的空间",
    }
    assert "key" not in item
    assert "source" not in item
    assert "workspace_id" not in item
    assert item["version"] == "1"
    assert item["capabilities"]["edit"] is True
    assert item["display_schema"]["editable_fields"] == ["content"]


@pytest.mark.integration
async def test_v2_typed_patch_updates_value_and_rejects_stale_version(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    entry = await app_ctx.state.user_memory_store.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="chart_style",
        value="line",
    )

    async with _client(app_ctx) as client:
        updated = await client.patch(
            f"/api/v1/memory/items/{entry.id}",
            json={
                "schema": "text_v1",
                "patch": {"content": "bar"},
                "version": "1",
            },
        )
        stale = await client.patch(
            f"/api/v1/memory/items/{entry.id}",
            json={
                "schema": "text_v1",
                "patch": {"content": "pie"},
                "version": "1",
            },
        )

    assert updated.status_code == 200
    assert updated.headers["cache-control"] == "no-store, private"
    assert updated.json()["display_value"] == "bar"
    assert updated.json()["version"] == "2"
    assert "key" not in updated.json()
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "memory_version_conflict"
    assert stale.json()["detail"]["current_version"] == "2"
    assert stale.json()["detail"]["current_item"]["display_value"] == "bar"


@pytest.mark.integration
async def test_v2_typed_patch_rejects_extra_and_wrong_typed_fields(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    entry = await app_ctx.state.user_memory_store.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="chart_style",
        value="line",
    )

    async with _client(app_ctx) as client:
        extra = await client.patch(
            f"/api/v1/memory/items/{entry.id}",
            json={
                "schema": "text_v1",
                "patch": {"content": "bar"},
                "version": "1",
                "scope": "global",
            },
        )
        wrong_type = await client.patch(
            f"/api/v1/memory/items/{entry.id}",
            json={
                "schema": "text_v1",
                "patch": {"content": True},
                "version": "1",
            },
        )

    assert extra.status_code == 422
    assert wrong_type.status_code == 422
    assert wrong_type.json()["detail"]["code"] == "memory_edit_payload_invalid"


@pytest.mark.integration
async def test_v2_delete_and_restore_accept_if_match(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    entry = await app_ctx.state.user_memory_store.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="chart_style",
        value="line",
    )
    await app_ctx.state.user_memory_store.patch_product_value(
        user_id=uid,
        entry_id=entry.id,
        expected_version=1,
        edit_schema="text_v1",
        value="bar",
    )

    async with _client(app_ctx) as client:
        stale_delete = await client.delete(
            f"/api/v1/memory/{entry.id}",
            headers={"If-Match": '"1"'},
        )
        deleted = await client.delete(
            f"/api/v1/memory/{entry.id}",
            headers={"If-Match": '"2"'},
        )
        stale_restore = await client.post(
            f"/api/v1/memory/{entry.id}/restore",
            headers={"If-Match": '"2"'},
        )
        restored = await client.post(
            f"/api/v1/memory/{entry.id}/restore",
            headers={"If-Match": '"3"'},
        )

    assert stale_delete.status_code == 409
    assert stale_delete.json()["detail"]["current_version"] == "2"
    assert deleted.status_code == 200
    assert stale_restore.status_code == 409
    assert stale_restore.json()["detail"]["current_version"] == "3"
    assert restored.status_code == 200


@pytest.mark.integration
async def test_v2_items_search_matches_the_client_localized_source_line(
    app_ctx,
) -> None:
    """116 dogfood: an English search for a word in the displayed source line found nothing.

    The client names the source keys whose label, in its interface language, contains the query; the Gateway
    matches those keys instead of its Chinese `source_label`. Older clients that omit the parameter keep the
    Chinese-label match.
    """
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    told = await mem.upsert_product_memory(user_id=uid, kind="preference", key="told", value="Lead with the conclusion", source="user_directive")
    await mem.upsert_product_memory(user_id=uid, kind="preference", key="system", value="Quarterly view", source="system")

    async with _client(app_ctx) as client:
        localized = await client.get("/api/v1/memory/items", params={"query": "told", "source_label_keys": "told_in_chat"})
        no_label_match = await client.get("/api/v1/memory/items", params={"query": "told", "source_label_keys": ""})
        chinese_with_keys = await client.get("/api/v1/memory/items", params={"query": "告诉", "source_label_keys": ""})
        content_with_keys = await client.get("/api/v1/memory/items", params={"query": "quarterly", "source_label_keys": ""})
        legacy = await client.get("/api/v1/memory/items", params={"query": "告诉"})

    assert [item["id"] for item in localized.json()["items"]] == [told.id]
    assert no_label_match.json()["items"] == []
    assert chinese_with_keys.json()["items"] == []
    assert [item["display_value"] for item in content_with_keys.json()["items"]] == ["Quarterly view"]
    assert [item["id"] for item in legacy.json()["items"]] == [told.id]


@pytest.mark.integration
async def test_v2_items_use_opaque_cursor(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    for index in range(3):
        await mem.upsert_product_memory(
            user_id=uid,
            kind="preference",
            key=f"k{index}",
            value=f"value {index}",
        )

    async with _client(app_ctx) as client:
        first = await client.get("/api/v1/memory/items?limit=1")
        cursor = first.json()["next_cursor"]
        second = await client.get(
            "/api/v1/memory/items",
            params={"limit": 1, "cursor": cursor},
        )

    assert cursor
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]


@pytest.mark.integration
async def test_v2_read_routes_are_exposed_when_ready(app_ctx) -> None:
    await _enable_memory_v2_stores(app_ctx)

    async with _client(app_ctx) as client:
        overview = await client.get("/api/v1/memory/overview")
        items = await client.get("/api/v1/memory/items")

    assert overview.status_code == 200
    assert items.status_code == 200


# ─── /memory/snapshot ────────────────────────────────────


@pytest.mark.integration
async def test_snapshot_empty(app_ctx) -> None:
    async with _client(app_ctx) as c:
        r = await c.get("/api/v1/memory/snapshot")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["stats"]["active_count"] == 0
        assert body["stats"]["trash_count"] == 0
        assert body["stats"]["pending_candidates"] == 0
        assert body["dreaming"]["paused"] is False


@pytest.mark.integration
async def test_snapshot_with_memory_entries(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    await _seed_memory(app_ctx, user_id=uid, key="prefer_qoq", value="yes")
    await _seed_memory(app_ctx, user_id=uid, key="metric", value="GMV")
    async with _client(app_ctx) as c:
        body = (await c.get("/api/v1/memory/snapshot")).json()
        assert body["stats"]["active_count"] == 2
        origins = {item["origin"] for item in body["items"]}
        assert origins == {"memory"}
        keys = {item["key"] for item in body["items"]}
        assert keys == {"prefer_qoq", "metric"}


@pytest.mark.integration
async def test_explicit_global_remember_rejects_cross_space_move(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    existing = await mem.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="report_order",
        value="detail first",
        workspace_id="ws_retail",
    )

    async with _client(app_ctx) as c:
        response = await c.post(
            "/api/v1/memory/user/remember",
            json={
                "kind": "preference",
                "key": "report_order",
                "value": "summary first",
            },
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "memory_scope_conflict"
    assert detail["entry_id"] == existing.id
    assert detail["existing_scope"] == "ws_retail"
    assert detail["requested_scope"] == "global"
    current = next(entry for entry in await mem.list_for_user(uid) if entry.id == existing.id)
    assert current.parsed_value() == "detail first"
    assert current.workspace_id == "ws_retail"


@pytest.mark.integration
async def test_snapshot_excludes_trashed(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    eid = await _seed_memory(app_ctx, user_id=uid, key="x", value="1")
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.soft_delete(user_id=uid, entry_id=eid)
    async with _client(app_ctx) as c:
        body = (await c.get("/api/v1/memory/snapshot")).json()
        assert body["items"] == []
        assert body["stats"]["active_count"] == 0
        assert body["stats"]["trash_count"] == 1


@pytest.mark.integration
async def test_snapshot_dreaming_paused_state(app_ctx) -> None:
    settings: UserSettingsStore = app_ctx.state.user_settings_store
    uid = app_ctx.state.settings.dev_default_user
    await settings.set(uid, "memory_dreaming_paused", "true")
    async with _client(app_ctx) as c:
        body = (await c.get("/api/v1/memory/snapshot")).json()
        assert body["dreaming"]["paused"] is True


@pytest.mark.integration
async def test_snapshot_pending_candidate_count(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="a",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="b",
        value="y",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        body = (await c.get("/api/v1/memory/snapshot")).json()
        assert body["stats"]["pending_candidates"] == 2


# ─── /memory/trash ───────────────────────────────────────


@pytest.mark.integration
async def test_trash_unified_list(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    eid = await _seed_memory(app_ctx, user_id=uid, key="m", value="1")
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.soft_delete(user_id=uid, entry_id=eid)

    async with _client(app_ctx) as c:
        body = (await c.get("/api/v1/memory/trash")).json()
        assert len(body["items"]) == 1
        origins = {it["origin"] for it in body["items"]}
        assert origins == {"memory"}


# ─── DELETE /memory/{entry_id} (unified) ─────────────────


@pytest.mark.integration
async def test_unified_delete_routes_memory(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    eid = await _seed_memory(app_ctx, user_id=uid, key="x", value="1")
    async with _client(app_ctx) as c:
        r = await c.delete(f"/api/v1/memory/{eid}")
        assert r.status_code == 200
        assert r.json()["deleted"] == eid
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    assert await mem.list_for_user(uid) == []
    assert await mem.count_trashed(uid) == 1


@pytest.mark.integration
async def test_unified_delete_locked_returns_404(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    e = await mem.upsert(
        user_id=uid,
        kind="datarc_binding",
        key="member_uid",
        value="m_locked",
        locked=True,
        lock_reason="bound",
    )
    async with _client(app_ctx) as c:
        r = await c.delete(f"/api/v1/memory/{e.id}")
        assert r.status_code == 404


# ─── /memory/{entry_id}/restore ──────────────────────────


@pytest.mark.integration
async def test_restore_memory_entry(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    eid = await _seed_memory(app_ctx, user_id=uid, key="x", value="1")
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.soft_delete(user_id=uid, entry_id=eid)
    async with _client(app_ctx) as c:
        r = await c.post(f"/api/v1/memory/{eid}/restore")
        assert r.status_code == 200
        assert r.json()["restored"] is True
    actives = await mem.list_for_user(uid)
    assert len(actives) == 1


@pytest.mark.integration
async def test_restore_404_on_active(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    eid = await _seed_memory(app_ctx, user_id=uid, key="x", value="1")
    async with _client(app_ctx) as c:
        r = await c.post(f"/api/v1/memory/{eid}/restore")
        assert r.status_code == 404


# ─── /memory/reset ───────────────────────────────────────


@pytest.mark.integration
async def test_v2_reset_is_atomic_product_only_and_restorable(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="report_style",
        value="先结论后依据",
    )
    operational = await mem.upsert_operational_memory(
        user_id=uid,
        kind="satisfaction",
        key="t1",
        value={"score": 5},
        source="turn_feedback",
    )
    await app_ctx.state.memory_notes_store.upsert_content(
        user_id=uid,
        content="📌 我负责零售业务",
    )
    await app_ctx.state.memory_note_anchors_store.insert_or_revive(
        user_id=uid,
        bullet_text="我负责零售业务",
        canvas_id="c_1",
        section_id="sec_1",
        verifier_result_hash="hash",
    )
    await app_ctx.state.memory_candidate_store.enqueue(
        user_id=uid,
        kind="preference",
        key="chart_style",
        value="折线图",
        reason=None,
        source_turn_id=None,
    )

    async with _client(app_ctx) as client:
        preview = await client.get("/api/v1/memory/reset-preview")
        reset = await client.post(
            "/api/v1/memory/reset",
            headers=_idempotency_headers(),
        )
        reset_body = reset.json()
        after_reset = await client.get("/api/v1/memory/items")
        recent = await client.get("/api/v1/memory/resets")
        detail = await client.get(
            f"/api/v1/memory/resets/{reset_body['snapshot_id']}",
        )
        restored = await client.post(
            f"/api/v1/memory/resets/{reset_body['snapshot_id']}/restore",
            headers=_idempotency_headers(),
        )
        after_restore = await client.get("/api/v1/memory/items")

    expected_counts = {
        "notes": 1,
        "entries": 1,
        "anchors": 1,
        "locked_skipped": 0,
        "pending_preserved": 1,
    }
    assert preview.status_code == 200
    assert preview.json()["counts"] == expected_counts
    assert reset.status_code == 200
    assert reset_body["counts"] == expected_counts
    assert reset_body["expires_at"] is not None
    assert reset_body["undo_available"] is True
    assert after_reset.json()["items"] == []
    assert recent.json()["items"][0]["remaining_recoverable"] == 3
    assert detail.json()["remaining_recoverable"] == 3
    assert "先结论后依据" not in detail.text
    assert "我负责零售业务" not in detail.text
    all_rows = await mem.list_for_user(uid, include_trashed=True)
    operational_after_reset = next(row for row in all_rows if row.id == operational.id)
    assert operational_after_reset.deleted_at is None
    assert restored.status_code == 200
    assert restored.json()["status"] == "restored"
    assert restored.json()["restored_counts"] == {
        "entries": 1,
        "notes": 1,
        "anchors": 1,
    }
    assert len(after_restore.json()["items"]) == 1
    assert (await app_ctx.state.memory_notes_store.get(user_id=uid)).content == "📌 我负责零售业务"
    assert (
        len(
            await app_ctx.state.memory_note_anchors_store.list_for_user(
                user_id=uid,
            ),
        )
        == 1
    )


@pytest.mark.integration
async def test_v2_reset_restore_never_overwrites_newer_entry(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="report_style",
        value="旧值",
    )

    async with _client(app_ctx) as client:
        reset = await client.post(
            "/api/v1/memory/reset",
            headers=_idempotency_headers(),
        )
        await mem.upsert_product_memory(
            user_id=uid,
            kind="preference",
            key="report_style",
            value="清空后的新值",
        )
        restored = await client.post(
            "/api/v1/memory/undo-reset",
            json={"snapshot_id": reset.json()["snapshot_id"]},
        )

    current = await mem.list_product_for_user(uid)
    assert restored.status_code == 200
    assert restored.json()["status"] == "partially_restored"
    assert restored.json()["conflicts"] == [
        {
            "resource_type": "memory_entry",
            "reason": "newer_version_exists",
        },
    ]
    assert restored.json()["remaining_recoverable"] == 1
    assert current[0].parsed_value() == "清空后的新值"


@pytest.mark.integration
async def test_v2_reset_and_restore_idempotency_contract(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="report_style",
        value="summary first",
    )
    reset_key = "781d8bd9-56b1-443d-a3bd-2bbbdd10a025"
    restore_key = "f4da969c-7bd4-42ef-8720-1366b8f305ee"

    async with _client(app_ctx) as client:
        first_reset = await client.post(
            "/api/v1/memory/reset",
            headers={"Idempotency-Key": reset_key},
        )
        replayed_reset = await client.post(
            "/api/v1/memory/reset",
            headers={"Idempotency-Key": reset_key},
        )
        snapshot_id = first_reset.json()["snapshot_id"]
        reused_key = await client.post(
            f"/api/v1/memory/resets/{snapshot_id}/restore",
            headers={"Idempotency-Key": reset_key},
        )
        first_restore = await client.post(
            f"/api/v1/memory/resets/{snapshot_id}/restore",
            headers={"Idempotency-Key": restore_key},
        )
        replayed_restore = await client.post(
            f"/api/v1/memory/resets/{snapshot_id}/restore",
            headers={"Idempotency-Key": restore_key},
        )
        invalid_key = await client.post(
            "/api/v1/memory/reset",
            headers={"Idempotency-Key": "not-a-uuid"},
        )

    assert first_reset.status_code == 200
    assert replayed_reset.status_code == 200
    assert replayed_reset.json() == first_reset.json()
    assert reused_key.status_code == 409
    assert reused_key.json()["detail"]["code"] == ("memory_idempotency_key_reused")
    assert first_restore.status_code == 200
    assert replayed_restore.status_code == 200
    assert replayed_restore.json() == first_restore.json()
    assert invalid_key.status_code == 400
    assert invalid_key.json()["detail"]["code"] == ("memory_idempotency_key_invalid")


@pytest.mark.integration
async def test_reset_soft_deletes_all_unlocked(app_ctx) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    await _seed_product_memory(app_ctx, user_id=uid, key="a", value="1")
    await _seed_product_memory(app_ctx, user_id=uid, key="b", value="2")
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/reset",
            headers=_idempotency_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["affected_memory"] == 2
        assert body["snapshot_id"].startswith("snap_")

    (await _client(app_ctx).__aenter__()).get  # fresh client  # noqa: B018
    async with _client(app_ctx) as c:
        body = (await c.get("/api/v1/memory/snapshot")).json()
        assert body["items"] == []
        assert body["stats"]["trash_count"] == 2


@pytest.mark.integration
async def test_reset_skips_locked(app_ctx) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="member_uid",
        value="m_x",
        locked=True,
    )
    await _seed_product_memory(app_ctx, user_id=uid, key="free", value="1")
    async with _client(app_ctx) as c:
        body = (
            await c.post(
                "/api/v1/memory/reset",
                headers=_idempotency_headers(),
            )
        ).json()
        assert body["affected_memory"] == 1
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        # Locked entry remains
        assert any(it["locked"] for it in snap["items"])


@pytest.mark.integration
async def test_reset_writes_snapshot_to_db(app_ctx) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    await _seed_product_memory(app_ctx, user_id=uid, key="x", value="1")
    async with _client(app_ctx) as c:
        body = (
            await c.post(
                "/api/v1/memory/reset",
                headers=_idempotency_headers(),
            )
        ).json()
        snap_id = body["snapshot_id"]

    import aiosqlite

    db_path = str(app_ctx.state.user_memory_store._db_path)
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT schema_version, snapshot_json FROM memory_reset_snapshots WHERE id=?",
            (snap_id,),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 3
    assert json.loads(row[1]) == {}
    async with (
        aiosqlite.connect(db_path) as db,
        db.execute(
            "SELECT COUNT(*) FROM memory_reset_snapshot_items WHERE snapshot_id=?",
            (snap_id,),
        ) as cur,
    ):
        item_count = await cur.fetchone()
    assert item_count == (1,)


# ─── /memory/export (streaming) ──────────────────────────


@pytest.mark.integration
async def test_export_streams_json(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="report_style",
        value="先结论后依据",
    )
    async with _client(app_ctx) as c:
        r = await c.get("/api/v1/memory/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "attachment" in r.headers["content-disposition"]
        assert r.headers["cache-control"] == "no-store, private"
        assert r.headers["pragma"] == "no-cache"
        assert r.headers["x-content-type-options"] == "nosniff"
        body = json.loads(r.text)
        assert body["_schema_version"] == 3
        assert body["user_id"] == uid
        assert body["items"][0]["display_value"] == "先结论后依据"


@pytest.mark.integration
async def test_export_includes_trashed(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    entry = await mem.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="report_style",
        value="先结论后依据",
    )
    eid = entry.id
    await mem.soft_delete(user_id=uid, entry_id=eid)
    async with _client(app_ctx) as c:
        body = json.loads((await c.get("/api/v1/memory/export")).text)
        assert len(body["items"]) == 1
        assert body["items"][0]["deleted_at"] is not None


@pytest.mark.integration
async def test_export_separates_active_and_recoverable_about_you(
    app_ctx,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    note = await app_ctx.state.memory_notes_store.upsert_content(
        user_id=uid,
        content="旧的关于你",
    )

    async with _client(app_ctx) as client:
        await client.delete(
            "/api/v1/memory/about-you",
            headers={"If-Match": f'"{note.version}"'},
        )
        await app_ctx.state.memory_notes_store.upsert_content(
            user_id=uid,
            content="新的关于你",
        )
        response = await client.get("/api/v1/memory/export")

    body = json.loads(response.text)
    assert body["about_you"]["content"] == "新的关于你"
    assert body["about_you"]["state"] == "active"
    assert body["recoverable_about_you"][0]["content"] == "旧的关于你"
    assert body["recoverable_about_you"][0]["state"] == "trashed"
    assert "editing_lock_token_hash" not in response.text
    assert "verifier_result_hash" not in response.text


@pytest.mark.integration
async def test_export_excludes_operational_and_unclassified_rows(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    await mem.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="report_style",
        value="可导出的产品记忆",
    )
    await mem.upsert_operational_memory(
        user_id=uid,
        kind="satisfaction",
        key="internal_turn_secret",
        value={"score": 5, "sentinel": "never-export-operational"},
        source="turn_feedback",
    )
    await mem.upsert(
        user_id=uid,
        kind="preference",
        key="legacy_secret_key",
        value="never-export-unclassified",
    )

    async with _client(app_ctx) as c:
        response = await c.get("/api/v1/memory/export")

    assert response.status_code == 200
    assert "可导出的产品记忆" in response.text
    assert "internal_turn_secret" not in response.text
    assert "never-export-operational" not in response.text
    assert "legacy_secret_key" not in response.text
    assert "never-export-unclassified" not in response.text


@pytest.mark.integration
async def test_v2_permanent_delete_all_reports_success_only_after_scrub(
    app_ctx,
    tmp_path: Path,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    await _enable_permanent_delete_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    await app_ctx.state.user_memory_store.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="report_style",
        value="先结论后依据",
    )
    await app_ctx.state.user_memory_store.upsert_operational_memory(
        user_id=uid,
        kind="satisfaction",
        key="private_comment",
        value={"comment": "private"},
        source="turn_feedback",
    )
    await app_ctx.state.memory_notes_store.upsert_content(
        user_id=uid,
        content="我负责零售业务",
    )
    await app_ctx.state.memory_candidate_store.enqueue(
        user_id=uid,
        kind="preference",
        key="chart_style",
        value="折线图",
        reason="用户明确偏好",
        source_turn_id="t_private",
    )
    idempotency_key = "61df932a-23c8-45c6-a2b8-1c4f952ba4ed"

    async with _client(app_ctx) as client:
        capabilities = await client.get("/api/v1/memory/capabilities")
        deleted = await client.post(
            "/api/v1/memory/permanent-delete-all",
            headers={"Idempotency-Key": idempotency_key},
        )
        operation = await client.get(
            f"/api/v1/memory/operations/{deleted.json()['operation_id']}",
        )
        overview = await client.get("/api/v1/memory/overview")
        repeated = await client.post(
            "/api/v1/memory/permanent-delete-all",
            headers={"Idempotency-Key": idempotency_key},
        )

    assert capabilities.json()["actions"]["permanent_delete"] is True
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "completed"
    assert deleted.json()["logical_scrub_completed"] is True
    assert deleted.json()["physical_purge_completed"] is True
    assert deleted.json()["deletion_receipt_committed"] is True
    assert deleted.json()["dreaming_paused"] is True
    assert operation.json()["operation_id"] == deleted.json()["operation_id"]
    assert overview.json()["about_you"]["content"] == ""
    assert overview.json()["stats"]["saved_entries"] == 0
    assert overview.json()["stats"]["pending_candidates"] == 0
    assert overview.json()["learning"]["automatic_learning"] == "paused"
    assert repeated.json()["operation_id"] == deleted.json()["operation_id"]


@pytest.mark.integration
async def test_v2_permanent_delete_item_and_reset_endpoints(
    app_ctx,
    tmp_path: Path,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    await _enable_permanent_delete_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    memory: UserMemoryStore = app_ctx.state.user_memory_store
    item = await memory.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="temporary",
        value="remove permanently",
    )
    assert await memory.soft_delete(
        user_id=uid,
        entry_id=item.id,
        expected_version=item.version,
    )

    async with _client(app_ctx) as client:
        trash_before = await client.get(
            "/api/v1/memory/items?state=trashed",
        )
        deleted_item = await client.post(
            f"/api/v1/memory/items/{item.id}/permanent-delete",
            headers={
                "Idempotency-Key": ("cf3d7032-facd-483d-b0a3-bbc85d0ed32c"),
            },
        )
        missing_item = await client.post(
            "/api/v1/memory/items/not-owned/permanent-delete",
            headers={
                "Idempotency-Key": ("d741713e-72ba-4057-b866-aa001634d956"),
            },
        )
        await memory.upsert_product_memory(
            user_id=uid,
            kind="preference",
            key="reset-target",
            value="old",
        )
        reset = await client.post(
            "/api/v1/memory/reset",
            headers=_idempotency_headers(),
        )
        deleted_reset = await client.post(
            (f"/api/v1/memory/resets/{reset.json()['snapshot_id']}/permanent-delete"),
            headers={
                "Idempotency-Key": ("6e140427-7757-4e31-a3c1-8e3b306740fd"),
            },
        )
        resets_after = await client.get("/api/v1/memory/resets")

    assert trash_before.json()["items"][0]["capabilities"]["permanent_delete"] is True
    assert deleted_item.status_code == 200
    assert deleted_item.json()["action"] == "permanent_delete_item"
    assert deleted_item.json()["status"] == "completed"
    assert missing_item.status_code == 404
    assert deleted_reset.status_code == 200
    assert deleted_reset.json()["action"] == "permanent_delete_reset"
    assert deleted_reset.json()["status"] == "completed"
    assert resets_after.json()["items"] == []


@pytest.mark.integration
async def test_v2_clear_trash_uses_frozen_manifest(
    app_ctx,
    tmp_path: Path,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    await _enable_permanent_delete_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    memory: UserMemoryStore = app_ctx.state.user_memory_store
    first = await memory.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="first",
        value="one",
    )
    second = await memory.upsert_product_memory(
        user_id=uid,
        kind="preference",
        key="second",
        value="two",
    )
    assert await memory.soft_delete(
        user_id=uid,
        entry_id=first.id,
        expected_version=first.version,
    )
    assert await memory.soft_delete(
        user_id=uid,
        entry_id=second.id,
        expected_version=second.version,
    )

    async with _client(app_ctx) as client:
        cleared = await client.post(
            "/api/v1/memory/trash/permanent-delete",
            headers={
                "Idempotency-Key": ("d3475b68-1101-4994-bf2a-da90633baa54"),
            },
        )
        trash_after = await client.get(
            "/api/v1/memory/items?state=trashed",
        )

    assert cleared.status_code == 200
    assert cleared.json()["action"] == "permanent_delete_trash"
    assert cleared.json()["counts"]["entries"] == 2
    assert trash_after.json()["items"] == []


@pytest.mark.integration
async def test_memory_write_barrier_returns_retryable_423(
    app_ctx,
    tmp_path: Path,
) -> None:
    await _enable_memory_v2_stores(app_ctx)
    store = await _enable_permanent_delete_store(app_ctx, tmp_path)
    uid = app_ctx.state.settings.dev_default_user
    await store._prepare_full_delete(
        user_id=uid,
        idempotency_key="ce4f4c5d-5485-41ef-a929-43957810bb38",
    )

    async with _client(app_ctx) as client:
        response = await client.post("/api/v1/memory/dreaming/resume")

    assert response.status_code == 423
    assert response.headers["cache-control"] == "no-store, private"
    assert response.json()["detail"] == {
        "code": "memory_write_temporarily_blocked",
        "operation_id": response.json()["detail"]["operation_id"],
        "retryable": True,
    }
    assert response.json()["detail"]["operation_id"].startswith("mop_")


# ─── /memory/dreaming pause toggle ───────────────────────


@pytest.mark.integration
async def test_dreaming_state_default_unpaused(app_ctx) -> None:
    async with _client(app_ctx) as c:
        body = (await c.get("/api/v1/memory/dreaming")).json()
        assert body["paused"] is False


@pytest.mark.integration
async def test_dreaming_pause_then_resume(app_ctx) -> None:
    async with _client(app_ctx) as c:
        body = (await c.post("/api/v1/memory/dreaming/pause")).json()
        assert body["paused"] is True
        body2 = (await c.get("/api/v1/memory/dreaming")).json()
        assert body2["paused"] is True

        body3 = (await c.post("/api/v1/memory/dreaming/resume")).json()
        assert body3["paused"] is False


@pytest.mark.integration
async def test_dreaming_pause_invalidates_cache(app_ctx) -> None:
    """After pause, cache must reflect new state immediately (TTL bypassed via invalidate)."""
    cache: PauseStateCache = app_ctx.state.pause_state_cache
    uid = app_ctx.state.settings.dev_default_user

    # warm cache with unpaused state
    assert await cache.is_paused(uid) is False

    async with _client(app_ctx) as c:
        await c.post("/api/v1/memory/dreaming/pause")

    # Cache invalidated → next read reflects DB state (true)
    assert await cache.is_paused(uid) is True


# ─── candidates ownership + status ───────────────────────


@pytest.mark.integration
async def test_candidates_list_status_filter(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    a = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="a",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    b = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="b",
        value="y",
        reason=None,
        source_turn_id=None,
    )
    await cs.set_decision(cid=b.id, status="approved", decided_by=uid)

    async with _client(app_ctx) as c:
        pending = (await c.get("/api/v1/memory/candidates?status=pending")).json()
        assert len(pending["items"]) == 1
        assert pending["items"][0]["id"] == a.id

        approved = (await c.get("/api/v1/memory/candidates?status=approved")).json()
        assert len(approved["items"]) == 1
        assert approved["items"][0]["id"] == b.id

        all_ = (await c.get("/api/v1/memory/candidates?status=all")).json()
        assert len(all_["items"]) == 2


@pytest.mark.integration
async def test_candidates_approve_cross_user_blocked(app_ctx) -> None:
    """Spike #4 security fix: user A cannot approve user B's candidate."""
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    cand = await cs.enqueue(
        user_id="u_bob",
        kind="preference",
        key="bob_pref",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        # Default user (alice) tries to approve bob's candidate
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/approve", json={})
        assert r.status_code == 404

    # Bob's candidate is still pending (not approved by alice)
    refreshed = await cs.get(cand.id)
    assert refreshed.status == "pending"
    # Bob's user_memory not written to
    mem: UserMemoryStore = app_ctx.state.user_memory_store
    bob_entries = await mem.list_for_user("u_bob")
    assert all(e.key != "bob_pref" for e in bob_entries)


@pytest.mark.integration
async def test_candidates_reject_cross_user_blocked(app_ctx) -> None:
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    cand = await cs.enqueue(
        user_id="u_bob",
        kind="preference",
        key="x",
        value="y",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/reject", json={})
        assert r.status_code == 404
    refreshed = await cs.get(cand.id)
    assert refreshed.status == "pending"


@pytest.mark.integration
async def test_candidates_approve_owner_succeeds(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    cand = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="my_pref",
        value="x",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        r = await c.post(f"/api/v1/memory/candidates/{cand.id}/approve", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    mem: UserMemoryStore = app_ctx.state.user_memory_store
    entries = await mem.list_for_user(uid)
    assert any(e.key == "my_pref" for e in entries)


# ─── audit + telemetry ───────────────────────────────────


@pytest.mark.integration
async def test_audit_logged_on_approve(app_ctx) -> None:
    uid = app_ctx.state.settings.dev_default_user
    cs: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    cand = await cs.enqueue(
        user_id=uid,
        kind="preference",
        key="k",
        value="v",
        reason=None,
        source_turn_id=None,
    )
    async with _client(app_ctx) as c:
        await c.post(f"/api/v1/memory/candidates/{cand.id}/approve", json={})

    import aiosqlite

    db_path = app_ctx.state.user_memory_store._db_path
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT event_type, payload_json FROM user_audit_log WHERE event_type=?",
            ("memory.candidate.approve",),
        ) as cur:
            rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "memory.candidate.approve"


@pytest.mark.integration
async def test_telemetry_logged_on_reset(app_ctx) -> None:
    await _enable_memory_v2_stores(app_ctx)
    uid = app_ctx.state.settings.dev_default_user
    await _seed_product_memory(app_ctx, user_id=uid, key="x", value="1")
    async with _client(app_ctx) as c:
        await c.post(
            "/api/v1/memory/reset",
            headers=_idempotency_headers(),
        )

    log: MetricsLog = app_ctx.state.metrics_log
    rows = await log.query_recent(name="memory.reset", user_id=uid)
    assert len(rows) == 1


@pytest.mark.integration
async def test_telemetry_logged_on_dreaming_pause(app_ctx) -> None:
    async with _client(app_ctx) as c:
        await c.post("/api/v1/memory/dreaming/pause")

    log: MetricsLog = app_ctx.state.metrics_log
    rows = await log.query_recent(name="memory.dreaming.paused")
    assert len(rows) == 1


# ─── end-to-end happy path ──────────────────────────────


@pytest.mark.integration
async def test_remember_rejects_oversized_value(app_ctx) -> None:
    """Round 3 P1: per-entry value size cap (16 KB) prevents DOS via huge payloads."""
    big = "x" * (32 * 1024)  # 32 KB > 16 KB cap
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/user/remember",
            json={"kind": "preference", "key": "huge", "value": big},
        )
        assert r.status_code == 413


@pytest.mark.integration
async def test_remember_under_cap_works(app_ctx) -> None:
    medium = "y" * (10 * 1024)  # 10 KB under cap
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/user/remember",
            json={"kind": "preference", "key": "med", "value": medium},
        )
        assert r.status_code == 200


@pytest.mark.integration
async def test_remember_locked_field_in_body_ignored(app_ctx) -> None:
    """User cannot lock entries via /remember; locked field is ignored (Pydantic strips)."""
    async with _client(app_ctx) as c:
        r = await c.post(
            "/api/v1/memory/user/remember",
            json={"kind": "preference", "key": "x", "value": "v", "locked": True},
        )
        assert r.status_code == 200
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        item = next(it for it in snap["items"] if it["key"] == "x")
        assert item["locked"] is False


@pytest.mark.integration
async def test_full_flow_remember_delete_restore(app_ctx) -> None:
    """Happy path: remember → snapshot → delete → trash → restore → snapshot."""
    async with _client(app_ctx) as c:
        # remember
        r = await c.post(
            "/api/v1/memory/user/remember",
            json={"kind": "preference", "key": "k1", "value": "v1"},
        )
        assert r.status_code == 200
        eid = r.json()["id"]

        # snapshot — visible
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        assert any(it["id"] == eid for it in snap["items"])

        # delete (unified)
        r = await c.delete(f"/api/v1/memory/{eid}")
        assert r.status_code == 200

        # snapshot — gone
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        assert all(it["id"] != eid for it in snap["items"])

        # trash — present
        trash = (await c.get("/api/v1/memory/trash")).json()
        assert any(it["id"] == eid for it in trash["items"])

        # restore
        r = await c.post(f"/api/v1/memory/{eid}/restore")
        assert r.status_code == 200

        # snapshot — back
        snap = (await c.get("/api/v1/memory/snapshot")).json()
        assert any(it["id"] == eid for it in snap["items"])


@pytest.mark.integration
async def test_the_memory_page_write_gate_refuses_credentials_and_one_off_requests(app_ctx, monkeypatch) -> None:
    """★★ 记忆页手动新建（`POST /memory/user/remember`）这条写路的写入闸此前**零覆盖**：
    2026-09-10 把整个闸块关掉，186 个 gateway memory 用例全绿。而它守的恰恰是凭据入库——
    调用点上方的注释写着：同一段 `api_key=sk-...`，走 agent 被拒、走记忆页照存。

    这是安全边界用例（凭据不许落进长期记忆），不是在断言旧功能不存在。
    ★ 突变验证：`if memory_write_gate_enabled():` 改成 `if False:` ⇒ 本用例红。
    """
    from bowhead_core.memory_write_gate import REASON_CONTAINS_SECRET, REASON_ONE_OFF

    # Explicitly enable the gate, independent of defaults: flipping the default silently reverses the premise that deleting the variable disables it.
    secret = "sk-ant-0123456789abcdefghijklmnop"
    async with _client(app_ctx) as c:
        ok = await c.post(
            "/api/v1/memory/user/remember",
            json={"kind": "preference", "key": "tone", "value": "回答请简洁一些"},
        )
        cred = await c.post(
            "/api/v1/memory/user/remember",
            json={"kind": "preference", "key": "apikey", "value": f"api_key={secret}"},
        )
        once = await c.post(
            "/api/v1/memory/user/remember",
            json={"kind": "preference", "key": "once", "value": "这一次先不要图"},
        )
        snap = (await c.get("/api/v1/memory/snapshot")).json()

    # Positive control: ordinary preferences must save; otherwise 422 below could merely mean this route rejects everything.
    assert ok.status_code == 200, ok.text

    assert cred.status_code == 422, cred.text
    assert cred.json()["detail"]["code"] == "memory_write_gate_refused"
    assert cred.json()["detail"]["reason"] == REASON_CONTAINS_SECRET
    assert secret not in cred.text, "拒绝回执里不许把凭据原样带回去"

    assert once.status_code == 422, once.text
    assert once.json()["detail"]["reason"] == REASON_ONE_OFF

    keys = {it["key"] for it in snap["items"]}
    assert "tone" in keys
    assert "apikey" not in keys, "凭据不许落库"
    assert "once" not in keys, "一次性要求不许落库"
