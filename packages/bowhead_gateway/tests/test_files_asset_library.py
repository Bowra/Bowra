"""file-management-v2 P1: 资产库视图后端（kind 过滤 + in_kb 徽章 + sections.json backfill）。

对应 PRD §4.2.3 / §4.2.4 与验收标准 P1 段。核心断言：

1. `kind` 参数把三 tab 切开，且**不被默认的 source=["upload"] 二次收窄**
   （否则资产 tab 只剩上传件，成果/导出全丢——这是最容易写错的一处）
2. `in_kb` 走跨 store 批查；"活跃"= soft_deleted_at IS NULL
3. KB store 缺失/抛错时列表照常返回（degrade，不 500）
4. sections.json 的 kind backfill 幂等且不误伤真正的导出成品
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.session_store import SessionStore
from bowhead_gateway.routes import files as files_routes
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "usr_owner"
_H = {"X-Bowhead-User-Id": _OWNER}

# The two kind sets in PRD §2.1.
_ASSET_KINDS = ["user_upload", "deliverable", "canvas_export"]
_RUN_KINDS = ["tool_data", "sandbox_output", "web_clip"]


class _FakeKbStore:
    """artifact_ids_in_kb 的最小替身（真 store 在独立 db，这里只验协议）。"""

    def __init__(self, in_kb: set[str] | None = None, boom: bool = False) -> None:
        self._in_kb = in_kb or set()
        self._boom = boom
        self.calls: list[list[str]] = []

    async def artifact_ids_in_kb(self, artifact_ids: list[str]) -> set[str]:
        self.calls.append(list(artifact_ids))
        if self._boom:
            raise RuntimeError("kb db unavailable")
        return {i for i in artifact_ids if i in self._in_kb}

    async def active_for_artifacts(self, artifact_ids: list[str]) -> list[SimpleNamespace]:
        """列表路径的批查：返回活跃 KB 行（这里只需 artifact_id；可见性另测）。"""
        hits = await self.artifact_ids_in_kb(artifact_ids)
        return [
            SimpleNamespace(artifact_id=i, file_id=f"kbf_{i}", scope="team", scope_id="team_elsewhere", uploaded_by="usr_x")
            for i in artifact_ids
            if i in hits
        ]


@pytest.fixture
async def app_client(tmp_path: Path):
    db_path = tmp_path / "a.db"
    session_store = SessionStore(db_path)
    await session_store.initialize()
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "files"), db_path=db_path)
    await store.initialize()
    app = FastAPI()
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.settings = SimpleNamespace(dev_default_user=_OWNER, auth_mode="dev")
    client = TestClient(app)
    return client, store, app


async def _put(store: ArtifactStore, *, filename: str, source: str, kind: str | None = None):
    return await store.put_bytes(
        content=b"x",
        filename=filename,
        mime="text/csv",
        source=source,
        created_by=_OWNER,
        kind=kind,
    )


async def _seed(store: ArtifactStore) -> dict[str, str]:
    """一份覆盖两个 kind 集合的语料。"""
    ids = {}
    ids["upload"] = (await _put(store, filename="Q1-复盘.pptx", source="upload")).id  # user_upload
    ids["deck"] = (await _put(store, filename="竞品分析.pptx", source="consultant_deck", kind="deliverable")).id
    ids["export"] = (await _put(store, filename="销售明细.xlsx", source="canvas_export")).id  # canvas_export
    ids["rows"] = (await _put(store, filename="rows_1042.csv", source="bi_fetch_rows")).id  # tool_data
    ids["sandbox"] = (await _put(store, filename="out_3.csv", source="python_sandbox")).id  # sandbox_output
    ids["clip"] = (await _put(store, filename="白皮书.md", source="web_fetch", kind="web_clip")).id
    return ids


# ── kind filtering (foundation for three tabs) ──────────────


async def test_asset_kinds_return_upload_deck_and_export(app_client) -> None:
    client, store, _ = app_client
    ids = await _seed(store)
    r = client.get("/api/v1/files/?" + "&".join(f"kind={k}" for k in _ASSET_KINDS), headers=_H)
    assert r.status_code == 200
    got = {i["file_id"] for i in r.json()["items"]}
    # Key regression: when only kind is supplied, default source=["upload"] must not narrow results to uploads alone.
    assert got == {ids["upload"], ids["deck"], ids["export"]}, "资产 tab 必须同时含上传/成果/导出"


async def test_run_kinds_return_only_runtime_exhaust(app_client) -> None:
    client, store, _ = app_client
    ids = await _seed(store)
    r = client.get("/api/v1/files/?" + "&".join(f"kind={k}" for k in _RUN_KINDS), headers=_H)
    got = {i["file_id"] for i in r.json()["items"]}
    assert got == {ids["rows"], ids["sandbox"], ids["clip"]}


async def test_asset_and_run_partition_is_disjoint_and_total(app_client) -> None:
    # The two tabs together cover the whole set without overlap (PRD §2.1 must define a partition).
    client, store, _ = app_client
    await _seed(store)
    a = {i["file_id"] for i in client.get("/api/v1/files/?" + "&".join(f"kind={k}" for k in _ASSET_KINDS), headers=_H).json()["items"]}
    b = {i["file_id"] for i in client.get("/api/v1/files/?" + "&".join(f"kind={k}" for k in _RUN_KINDS), headers=_H).json()["items"]}
    allr = {i["file_id"] for i in client.get("/api/v1/files/?source=all", headers=_H).json()["items"]}
    assert a & b == set()
    assert a | b == allr


async def test_kind_and_source_are_and_composed(app_client) -> None:
    client, store, _ = app_client
    ids = await _seed(store)
    r = client.get("/api/v1/files/?kind=canvas_export&source=canvas_export", headers=_H)
    assert {i["file_id"] for i in r.json()["items"]} == {ids["export"]}
    # Contradictory combination → empty set (proves AND, not OR).
    r2 = client.get("/api/v1/files/?kind=user_upload&source=canvas_export", headers=_H)
    assert r2.json()["items"] == []


async def test_source_default_unchanged_without_kind(app_client) -> None:
    # Existing callers (Data tab / old deep links) retain identical behavior.
    client, store, _ = app_client
    ids = await _seed(store)
    r = client.get("/api/v1/files/", headers=_H)
    assert {i["file_id"] for i in r.json()["items"]} == {ids["upload"]}


# ── in_kb badge ────────────────────────────────────────────


async def test_in_kb_flag_from_cross_store_batch(app_client) -> None:
    client, store, app = app_client
    ids = await _seed(store)
    fake = _FakeKbStore(in_kb={ids["upload"]})
    app.state.kb_files_store = fake
    r = client.get("/api/v1/files/?source=all", headers=_H)
    by_id = {i["file_id"]: i for i in r.json()["items"]}
    assert by_id[ids["upload"]]["in_kb"] is True
    assert by_id[ids["export"]]["in_kb"] is False
    # One batch query per page, not N+1.
    assert len(fake.calls) == 1
    assert set(fake.calls[0]) == set(by_id)


async def test_in_kb_degrades_when_kb_store_missing_or_raises(app_client) -> None:
    client, store, app = app_client
    await _seed(store)
    # Not wired → all False, no crash.
    r = client.get("/api/v1/files/?source=all", headers=_H)
    assert r.status_code == 200
    assert all(i["in_kb"] is False for i in r.json()["items"])
    # Exceptions also degrade rather than 500 (losing the whole list is much worse than a missing badge).
    app.state.kb_files_store = _FakeKbStore(boom=True)
    r2 = client.get("/api/v1/files/?source=all", headers=_H)
    assert r2.status_code == 200
    assert all(i["in_kb"] is False for i in r2.json()["items"])


# ── sections.json backfill（PRD §4.2.4）──────────────────────


async def test_sections_json_backfill_fixes_kind_and_spares_real_exports(tmp_path: Path) -> None:
    db_path = tmp_path / "b.db"
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=db_path)
    await store.initialize()
    # Simulate a historical row: sections.json persisted as canvas_export kind.
    import aiosqlite

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO artifacts (id,filename,mime,size,source,path,created_at,kind,producer_kind) "
            "VALUES ('art_legacy_sec','sections.json','application/json',9,'canvas_export','/tmp/s.json',"
            "'2026-01-01T00:00:00Z','canvas_export','canvas_export')",
        )
        await db.execute(
            "INSERT INTO artifacts (id,filename,mime,size,source,path,created_at,kind,producer_kind) "
            "VALUES ('art_real_export','经营分析.pptx','application/vnd.ms-powerpoint',9,'canvas_export','/tmp/d.pptx',"
            "'2026-01-01T00:00:00Z','canvas_export','canvas_export')",
        )
        # Roll back user_version to rerun backfill.
        await db.execute("PRAGMA user_version = 1")
        await db.commit()

    store2 = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=db_path)
    await store2.initialize()

    sec = await store2.get("art_legacy_sec")
    exp = await store2.get("art_real_export")
    assert sec is not None and sec.kind == "tool_data", "sections.json 应被修为中间数据"
    assert exp is not None and exp.kind == "canvas_export", "真正的导出成品不得被误伤"

    # Idempotent: initialize again without errors or changed values.
    store3 = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=db_path)
    await store3.initialize()
    sec2 = await store3.get("art_legacy_sec")
    assert sec2 is not None and sec2.kind == "tool_data"


# ── Source session (run-data tab column) ────────────────────


async def test_origin_session_batch_and_earliest_wins(app_client) -> None:
    client, store, _ = app_client
    from bowhead_core.session_store import SessionStore

    ss = SessionStore(store.db_path)
    await ss.initialize()
    s1 = await ss.ensure_session(session_id=None, workspace_id="ws_t", user_id=_OWNER)
    s2 = await ss.ensure_session(session_id=None, workspace_id="ws_t", user_id=_OWNER)
    await ss.set_title(session_id=s1.id, title="Q3 华东区销售分析")
    art = await store.put_bytes(
        content=b"x",
        filename="rows.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by=_OWNER,
        session_id=s1.id,
    )
    # Same artifact pinned to a second session later.
    import aiosqlite

    async with aiosqlite.connect(str(store.db_path)) as db:
        await db.execute(
            "INSERT INTO session_attachments(session_id, artifact_id, pinned_at) VALUES (?,?,?)",
            (s2.id, art.id, "2099-01-01T00:00:00Z"),
        )
        await db.commit()

    r = client.get("/api/v1/files/?kind=tool_data", headers=_H)
    row = next(i for i in r.json()["items"] if i["file_id"] == art.id)
    # Source = earliest pin (users ask where it came from, not who used it afterward).
    assert row["session_id"] == s1.id
    assert row["session_title"] == "Q3 华东区销售分析"


async def test_origin_session_absent_is_null_not_error(app_client) -> None:
    client, store, _ = app_client
    art = await _put(store, filename="orphan.csv", source="bi_fetch_rows")
    r = client.get("/api/v1/files/?kind=tool_data", headers=_H)
    row = next(i for i in r.json()["items"] if i["file_id"] == art.id)
    assert row["session_id"] is None
    assert row["session_title"] is None


# ── D2 backend defense: artifacts referenced by KB cannot be deleted (P2) ──


async def test_delete_kb_referenced_artifact_returns_409(app_client) -> None:
    client, store, app = app_client
    art = await _put(store, filename="制度手册.pdf", source="upload")
    app.state.kb_files_store = _FakeKbStore(in_kb={art.id})

    r = client.delete(f"/api/v1/files/{art.id}", headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "file_in_knowledge_base"

    # Regression for the silent half-dead bug: after rejection, the file must **remain readable**.
    still = await store.get(art.id)
    assert still is not None and still.deleted_at is None


async def test_delete_allowed_after_removed_from_kb(app_client) -> None:
    # Use a fresh decision: deletable immediately after KB removal (cannot use the list's batch-query cache).
    client, store, app = app_client
    art = await _put(store, filename="曾在知识库.pdf", source="upload")
    app.state.kb_files_store = _FakeKbStore(in_kb=set())
    assert client.delete(f"/api/v1/files/{art.id}", headers=_H).status_code == 204


async def test_delete_fails_open_when_kb_store_unavailable(app_client) -> None:
    # KB store instability must not block deletion (restorable within 30 days, an acceptable cost).
    client, store, app = app_client
    art = await _put(store, filename="x.csv", source="upload")
    app.state.kb_files_store = _FakeKbStore(boom=True)
    assert client.delete(f"/api/v1/files/{art.id}", headers=_H).status_code == 204


# ── KB reference reconciliation (P2 repair of existing data) ──


async def test_force_restore_rescues_kb_referenced_trashed_artifact(tmp_path: Path) -> None:
    """D2 防线上线前被删的知识库文件，必须在 GC 硬删之前被捞回来。"""
    db_path = tmp_path / "r.db"
    ss = SessionStore(db_path)
    await ss.initialize()
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=db_path)
    await store.initialize()

    kept = await _put(store, filename="制度.pdf", source="upload")
    gone = await _put(store, filename="临时.csv", source="upload")
    assert await store.soft_delete(kept.id, user_id=_OWNER)
    assert await store.soft_delete(gone.id, user_id=_OWNER)

    trashed = await store.list_soft_deleted_ids()
    assert set(trashed) == {kept.id, gone.id}

    # Simulate GC loop reconciliation: rescue only the artifact still referenced by KB.
    kb = _FakeKbStore(in_kb={kept.id})
    still_ref = await kb.artifact_ids_in_kb(trashed)
    n = await store.force_restore(sorted(still_ref))
    assert n == 1

    assert (await store.get(kept.id)) is not None, "被知识库引用的必须已恢复"
    assert (await store.get(gone.id)) is None, "没被引用的应留在回收站"


async def test_force_restore_is_idempotent_and_ignores_unknown(tmp_path: Path) -> None:
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f2"), db_path=tmp_path / "r2.db")
    await store.initialize()
    a = await _put(store, filename="a.csv", source="upload")
    await store.soft_delete(a.id, user_id=_OWNER)
    assert await store.force_restore([a.id]) == 1
    assert await store.force_restore([a.id]) == 0  # Already restored; rerun matches 0 rows.
    assert await store.force_restore(["art_不存在"]) == 0
    assert await store.force_restore([]) == 0


async def test_list_soft_deleted_ids_paginates_stably(tmp_path: Path) -> None:
    """对账必须能取尽——只取第一批会让超出批次的 KB 引用文件被静默硬删。"""
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "pg"), db_path=tmp_path / "pg.db")
    await store.initialize()
    ids = []
    for i in range(7):
        a = await _put(store, filename=f"f{i}.csv", source="upload")
        await store.soft_delete(a.id, user_id=_OWNER)
        ids.append(a.id)

    page1 = await store.list_soft_deleted_ids(limit=3, offset=0)
    page2 = await store.list_soft_deleted_ids(limit=3, offset=3)
    page3 = await store.list_soft_deleted_ids(limit=3, offset=6)
    assert len(page1) == 3 and len(page2) == 3 and len(page3) == 1
    # No overlap or omissions across pages (ORDER BY id ensures stability).
    assert set(page1 + page2 + page3) == set(ids)
    assert await store.list_soft_deleted_ids(limit=3, offset=7) == []


async def test_class_and_category_filters_apply_before_pagination(app_client) -> None:
    """「知识库」类别与业务分类在分页前生效：总数与页数按筛后结果算，不是只筛当前页（此前第 1 页只剩几行而页数不变）。"""
    import aiosqlite

    client, store, app = app_client
    ids = await _seed(store)
    app.state.kb_files_store = _FakeKbStore(in_kb={ids["upload"], ids["deck"]})
    assets = "&".join(f"kind={k}" for k in _ASSET_KINDS)

    def page(extra: str) -> dict:
        r = client.get(f"/api/v1/files/?{assets}&limit=1&{extra}", headers=_H)
        assert r.status_code == 200, r.text
        return r.json()

    kb_only = page("in_kb=1")
    assert kb_only["total"] == 2
    assert kb_only["items"][0]["file_id"] in {ids["upload"], ids["deck"]}
    not_kb = page("in_kb=0")
    assert not_kb["total"] == 1
    assert not_kb["items"][0]["file_id"] == ids["export"]
    # Control: without filtering, all three count.
    assert page("")["total"] == 3

    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET category = 'report' WHERE id IN (?, ?)", (ids["deck"], ids["export"]))
        await db.commit()
    assert page("category=report")["total"] == 2
    assert page("category=report&in_kb=1")["total"] == 1
    listed = page("category=report")["items"][0]
    assert listed["category"] == "report"
    # Transitional compatibility: normalize old Chinese category labels to code values before filtering.
    assert page("category=汇报材料")["total"] == 2
    assert page("category=汇报材料&in_kb=1")["total"] == 1
    # Unknown values use exact matching without errors; empty values do not filter.
    assert page("category=Report")["total"] == 0
    assert page("category=")["total"] == 3
