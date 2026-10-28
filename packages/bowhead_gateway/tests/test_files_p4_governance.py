"""file-management-v2 P4 —— 存储治理（运行数据 TTL / 永久删除 / 用量拆分）。

PRD §4.6。核心是一条产品规则：**回收的是 agent 的副产物，不是用户的东西**。
资产（上传/成品/导出）永不自动过期；运行数据到期也只是软删进回收站，
仍在 30 天窗口内可恢复。
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
_OTHER = "usr_other"
_H = {"X-Bowhead-User-Id": _OWNER}

_OLD = "2020-01-01T00:00:00+00:00"


@pytest.fixture
async def app_client(tmp_path: Path):
    db_path = tmp_path / "a.db"
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from bowhead_core import migrations

    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    ss = SessionStore(db_path)
    await ss.initialize()
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=db_path)
    await store.initialize()
    app = FastAPI()
    app.include_router(files_routes.router, prefix="/api/v1/files")
    app.state.artifact_store = store
    app.state.settings = SimpleNamespace(dev_default_user=_OWNER, auth_mode="dev")
    return TestClient(app), store


async def _make(store: ArtifactStore, *, kind: str, filename="f.csv", owner=_OWNER, aged=False, size=100, source="upload"):
    art = await store.put_bytes(
        content=b"x" * size,
        filename=filename,
        mime="text/csv",
        source=source,
        created_by=owner,
        kind=kind,
    )
    if aged:
        import aiosqlite

        async with aiosqlite.connect(str(store.db_path)) as db:
            await db.execute("UPDATE artifacts SET created_at = ? WHERE id = ?", (_OLD, art.id))
            await db.commit()
    return art


async def _run_ttl(store: ArtifactStore, *, days: int = 30, protected: set[str] | None = None) -> int:
    """TTL 是两步的（列候选 → 调用方剔除仍被引用的 → 落刀），因为 kb_files 在
    另一个库里 join 不到。测试按 GC 循环的真实用法串起来。"""
    cands = await store.stale_run_data_ids(days=days)
    return await store.expire_run_data_ids([i for i in cands if i not in (protected or set())])


# ── Run-data TTL ───────────────────────────────────────────


async def test_ttl_expires_old_run_data_only(tmp_path: Path) -> None:
    """回收 agent 副产物，**不碰用户的东西**。"""
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f"), db_path=tmp_path / "t.db")
    await SessionStore(tmp_path / "t.db").initialize()
    await store.initialize()

    old_run = await _make(store, kind="tool_data", filename="rows.csv", aged=True)
    old_asset = await _make(store, kind="user_upload", filename="Q1.pptx", aged=True)
    old_deck = await _make(store, kind="deliverable", filename="deck.pptx", aged=True)
    fresh_run = await _make(store, kind="sandbox_output", filename="out.csv", aged=False)

    n = await _run_ttl(store, days=30)
    assert n == 1, "只有过期的运行数据该被回收"

    assert (await store.get(old_run.id)) is None, "过期运行数据应进回收站"
    assert (await store.get(old_asset.id)) is not None, "用户上传永不自动过期"
    assert (await store.get(old_deck.id)) is not None, "AI 成品永不自动过期"
    assert (await store.get(fresh_run.id)) is not None, "未到期的运行数据不动"


async def test_ttl_is_soft_delete_not_permanent(tmp_path: Path) -> None:
    """TTL 只是进回收站——仍在 30 天窗口内可恢复。"""
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f2"), db_path=tmp_path / "t2.db")
    await SessionStore(tmp_path / "t2.db").initialize()
    await store.initialize()
    art = await _make(store, kind="tool_data", aged=True)

    await _run_ttl(store, days=30)
    assert (await store.get(art.id)) is None
    # Still in trash, restorable.
    assert art.id in await store.list_soft_deleted_ids()
    assert await store.restore(art.id, user_id=_OWNER) is True
    assert (await store.get(art.id)) is not None


async def test_ttl_skips_user_pinned_and_canvas_bound_but_not_auto_pinned(tmp_path: Path) -> None:
    """还被引用的运行数据不回收；**但「出生时自动打的 pin」不算引用**。

    契约在 2026-09 改过：`put_bytes(session_id=...)` 给每个产物自动打的那个 pin
    此前被当成「还在用」，于是 TTL 恒回收 0 条（116 实测 3,781/3,781 全被它挡下）。
    现在保活的是**真实引用边**：报告引用 / 画布正文 / 用户手动 pin（inject='full'）/
    文档任务输入。判据在 `bowhead_core.artifact_references`。
    """
    db = tmp_path / "t3.db"
    ss = SessionStore(db)
    await ss.initialize()
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f3"), db_path=db)
    await store.initialize()

    sess = await ss.ensure_session(session_id=None, workspace_id="ws", user_id=_OWNER)
    auto_pinned = await store.put_bytes(
        content=b"x", filename="auto.csv", mime="text/csv", source="bi_fetch_rows", created_by=_OWNER, session_id=sess.id
    )
    user_pinned = await store.put_bytes(
        content=b"x", filename="mine.csv", mime="text/csv", source="bi_fetch_rows", created_by=_OWNER, session_id=sess.id
    )
    on_canvas = await store.put_bytes(content=b"x", filename="c.csv", mime="text/csv", source="bi_fetch_rows", created_by=_OWNER, canvas_id="cv_1")
    orphan = await _make(store, kind="tool_data", filename="orphan.csv", aged=True)

    import aiosqlite

    async with aiosqlite.connect(str(db)) as d:
        await d.execute("UPDATE artifacts SET created_at = ? WHERE id IN (?,?,?)", (_OLD, auto_pinned.id, user_pinned.id, on_canvas.id))
        # Explicit user pin marker (same criterion as composer pin rows: inject='full').
        await d.execute("UPDATE artifacts SET inject='full' WHERE id = ?", (user_pinned.id,))
        await d.commit()

    n = await _run_ttl(store, days=30)
    assert n == 2, "自动 pin 的那份现在应该被回收（连同 orphan）"
    assert (await store.get(auto_pinned.id)) is None, "出生时自动打的 pin 不是引用，不该保活"
    assert (await store.get(user_pinned.id)) is not None, "用户自己 pin 的必须留着"
    assert (await store.get(on_canvas.id)) is not None, "挂在 canvas 上的不该被回收"
    assert (await store.get(orphan.id)) is None


async def test_purge_only_touches_own_trashed_rows(app_client) -> None:
    client, store = app_client
    mine = await _make(store, kind="user_upload", filename="mine.csv")
    theirs = await _make(store, kind="user_upload", filename="theirs.csv", owner=_OTHER)
    active = await _make(store, kind="user_upload", filename="active.csv")
    await store.soft_delete(mine.id, user_id=_OWNER)
    await store.soft_delete(theirs.id, user_id=_OTHER)

    r = client.post(
        "/api/v1/files/purge",
        json={"artifact_ids": [mine.id, theirs.id, active.id]},
        headers=_H,
    )
    assert r.status_code == 200
    # Purge only the row both owned by the caller and already in trash.
    assert r.json()["purged"] == 1
    assert mine.id not in await store.list_soft_deleted_ids()
    assert theirs.id in await store.list_soft_deleted_ids(), "别人的回收站不该被清"
    assert (await store.get(active.id)) is not None, "未删除的不该被 purge 带走"


async def test_purge_empty_is_noop(app_client) -> None:
    client, _ = app_client
    assert client.post("/api/v1/files/purge", json={"artifact_ids": []}, headers=_H).json()["purged"] == 0


async def test_failed_purge_cannot_be_restored_as_a_live_file(app_client, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    client, store = app_client
    art = await _make(store, kind="user_upload")
    await store.soft_delete(art.id, user_id=_OWNER)
    delete = store.backend.delete
    monkeypatch.setattr(store.backend, "delete", AsyncMock(side_effect=OSError("uncertain storage result")))
    response = client.post("/api/v1/files/purge", json={"artifact_ids": [art.id]}, headers=_H)
    assert response.status_code == 200 and response.json()["purged"] == 0
    response = client.post(f"/api/v1/files/{art.id}/restore", headers=_H)
    assert response.status_code == 404
    assert await store.get(art.id) is None
    assert await store.get(art.id, include_deleted=True) is not None
    monkeypatch.setattr(store.backend, "delete", delete)
    assert (await store.hard_delete_old_soft_deleted(days=30))["rows_removed"] == 1


# ── Usage breakdown ────────────────────────────────────────


async def test_usage_splits_assets_from_run_data(app_client) -> None:
    """一个总数说明不了该清哪一堆——必须分开给。"""
    client, store = app_client
    await _make(store, kind="user_upload", filename="a.pptx", size=1000)
    await _make(store, kind="canvas_export", filename="b.xlsx", size=500)
    await _make(store, kind="tool_data", filename="rows.csv", size=3000)
    await _make(store, kind="web_clip", filename="w.md", size=200)
    # Others' data must not count toward my usage.
    await _make(store, kind="user_upload", filename="x.pptx", owner=_OTHER, size=9999)

    usage = client.get("/api/v1/files/usage", headers=_H).json()
    assert usage["assets"] == 1500
    assert usage["run_data"] == 3200


async def test_usage_excludes_trashed(app_client) -> None:
    client, store = app_client
    keep = await _make(store, kind="user_upload", size=100)
    gone = await _make(store, kind="user_upload", size=700)
    await store.soft_delete(gone.id, user_id=_OWNER)
    assert client.get("/api/v1/files/usage", headers=_H).json()["assets"] == 100
    assert keep.id


async def test_purge_really_removes_not_just_marks(app_client) -> None:
    """永久删除必须真删——不能只把 deleted_at 改早让 GC 代劳。

    改时间戳的做法有两个坑：①文件在回收站继续显示最多 24h（GC 间隔）；
    ②若它被知识库引用，GC 前的对账会 force_restore 把它**复活**。
    """
    client, store = app_client
    art = await _make(store, kind="user_upload", filename="gone.csv")
    await store.soft_delete(art.id, user_id=_OWNER)

    client.post("/api/v1/files/purge", json={"artifact_ids": [art.id]}, headers=_H)

    # Row gone (not merely in trash with an old timestamp).
    assert (await store.get(art.id, include_deleted=True)) is None
    assert art.id not in await store.list_soft_deleted_ids()
    # Reconciliation cannot find or revive it.
    assert await store.force_restore([art.id]) == 0


class _FakeKb:
    def __init__(self, in_kb: set[str]) -> None:
        self._in_kb = in_kb

    async def artifact_ids_in_kb(self, ids):
        return {i for i in ids if i in self._in_kb}


async def test_purge_blocked_for_kb_referenced(app_client) -> None:
    """D2 同一把锁：软删有 409 防线，永久删除也必须有——而且这条路径连
    30 天恢复窗口都没有，漏了就是不可逆的知识库损坏。"""
    client, store = app_client
    app = client.app
    kb_art = await _make(store, kind="user_upload", filename="manual.pdf")
    plain = await _make(store, kind="user_upload", filename="plain.csv")
    await store.soft_delete(kb_art.id, user_id=_OWNER)
    await store.soft_delete(plain.id, user_id=_OWNER)
    app.state.kb_files_store = _FakeKb({kb_art.id})

    r = client.post(
        "/api/v1/files/purge",
        json={"artifact_ids": [kb_art.id, plain.id]},
        headers=_H,
    )
    body = r.json()
    assert body["purged"] == 1 and body["kb_blocked"] == 1
    assert (await store.get(kb_art.id, include_deleted=True)) is not None, "知识库引用的必须留下"
    assert (await store.get(plain.id, include_deleted=True)) is None


# ── TTL's biggest trap: KB source text resembles run data ────


async def test_ttl_never_touches_kb_upload_source(tmp_path: Path) -> None:
    """**P0 回归**：知识库上传的原文走 `routes/kb.py` 的 put_bytes，**不传 kind**，
    于是落成默认的 `tool_data`——按 kind 判，它就是"运行数据"。

    少了这道豁免，TTL 会把每一份知识库文档的原始字节收进回收站、30 天后硬删：
    检索侧还命中，点开原文 404，且不可逆。这条豁免按 `source` 判，是"就算 GC
    调用方忘了过滤 kb_files 也不该出事"的兜底。
    """
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f5"), db_path=tmp_path / "t5.db")
    await SessionStore(tmp_path / "t5.db").initialize()
    await store.initialize()
    kb_doc = await _make(store, kind="tool_data", filename="manual.pdf", aged=True, source="kb_upload")
    exhaust = await _make(store, kind="tool_data", filename="rows.csv", aged=True)

    assert kb_doc.id not in await store.stale_run_data_ids(days=30), "kb_upload 不该进候选"
    assert await _run_ttl(store, days=30) == 1
    assert (await store.get(kb_doc.id)) is not None, "知识库原文必须留下"
    assert (await store.get(exhaust.id)) is None


async def test_ttl_skips_ids_the_caller_marks_protected(tmp_path: Path) -> None:
    """GC 循环拿候选去反查 kb_files，把还被引用的剔掉再落刀——本测试锁这个契约。"""
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f6"), db_path=tmp_path / "t6.db")
    await SessionStore(tmp_path / "t6.db").initialize()
    await store.initialize()
    referenced = await _make(store, kind="tool_data", filename="cited.csv", aged=True)
    orphan = await _make(store, kind="tool_data", filename="orphan.csv", aged=True)

    assert await _run_ttl(store, days=30, protected={referenced.id}) == 1
    assert (await store.get(referenced.id)) is not None
    assert (await store.get(orphan.id)) is None


async def test_expire_refuses_ids_that_are_not_run_data(tmp_path: Path) -> None:
    """不信任传进来的 id 集：调用方算错也不能把资产删掉（WHERE 里再确认一次 kind）。"""
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "f7"), db_path=tmp_path / "t7.db")
    await SessionStore(tmp_path / "t7.db").initialize()
    await store.initialize()
    asset = await _make(store, kind="user_upload", filename="Q1.pptx", aged=True)

    assert await store.expire_run_data_ids([asset.id]) == 0
    assert (await store.get(asset.id)) is not None


async def test_usage_connection_count_does_not_grow_with_the_number_of_artifacts(app_client, monkeypatch) -> None:
    """用量按产物归属过滤：必须整批判，不能每个产物各开几次库。

    116 实测（2026-09-24）：逐个判时 1.2 万个产物 ≈4 万次开库、一次请求跑几十分钟，客户端 499 后后端照烧，
    4 核打满拖慢全站。这里每个产物挂在**各自的** turn 上——正是那次的形状（按 turn 推会话那一级曾逐个查）。
    """
    import aiosqlite

    client, store = app_client
    sessions = SessionStore(store.db_path)
    client.app.state.session_store = sessions

    async def make_on_own_turn(i: int) -> None:
        turn_id = f"turn_usage_{i}"
        session = await sessions.ensure_session(session_id=None, workspace_id="ws_usage", user_id=_OWNER)  # Only one active turn per session.
        await sessions.record_turn(turn_id=turn_id, session_id=session.id, user_text="t")
        await store.put_bytes(
            content=b"x" * 10,
            filename=f"r{i}.csv",
            mime="text/csv",
            created_by=_OWNER,
            kind="tool_data",
            created_by_turn=turn_id,
        )

    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    await make_on_own_turn(0)
    await make_on_own_turn(1)
    monkeypatch.setattr(aiosqlite, "connect", counting)
    assert client.get("/api/v1/files/usage", headers=_H).json()["run_data"] == 20
    few = opened["n"]

    monkeypatch.setattr(aiosqlite, "connect", real_connect)
    for i in range(2, 30):
        await make_on_own_turn(i)
    opened["n"] = 0
    monkeypatch.setattr(aiosqlite, "connect", counting)
    assert client.get("/api/v1/files/usage", headers=_H).json()["run_data"] == 300
    assert opened["n"] == few, f"2 个产物开库 {few} 次，30 个产物开库 {opened['n']} 次"


async def test_file_list_page_connection_count_does_not_grow_with_the_page(app_client, monkeypatch) -> None:
    """「文件」页列表：归属、可见性、引用次数整页批量取——开库次数与页内条数无关。"""
    import aiosqlite

    client, store = app_client
    sessions = SessionStore(store.db_path)
    client.app.state.session_store = sessions

    async def make_on_own_turn(i: int) -> None:
        session = await sessions.ensure_session(session_id=None, workspace_id="ws_list", user_id=_OWNER)
        await sessions.record_turn(turn_id=f"turn_list_{i}", session_id=session.id, user_text="t")
        art = await store.put_bytes(
            content=b"x", filename=f"l{i}.csv", mime="text/csv", created_by=_OWNER, kind="tool_data", created_by_turn=f"turn_list_{i}"
        )
        await sessions.record_turn_attachments(turn_id=f"turn_list_{i}", artifact_ids=[art.id])

    real_connect = aiosqlite.connect
    opened = {"n": 0}

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real_connect(*args, **kwargs)

    async def page_opens(expected: int) -> int:
        opened["n"] = 0
        monkeypatch.setattr(aiosqlite, "connect", counting)
        try:
            body = client.get("/api/v1/files/?kind=tool_data&limit=200", headers=_H).json()
        finally:
            monkeypatch.setattr(aiosqlite, "connect", real_connect)
        assert len(body["items"]) == expected
        assert {item["usage_count"] for item in body["items"]} == {1}
        return opened["n"]

    for i in range(2):
        await make_on_own_turn(i)
    await page_opens(2)  # Warm up: pools/caches built on the first process request do not count toward steady state.
    few = await page_opens(2)
    for i in range(2, 20):
        await make_on_own_turn(i)
    many = await page_opens(20)
    assert many == few, f"2 条开库 {few} 次，20 条开库 {many} 次"


async def test_batch_usage_counts_equal_usage_count(app_client) -> None:
    _client, store = app_client
    sessions = SessionStore(store.db_path)
    used = await _make(store, kind="user_upload")
    unused = await _make(store, kind="user_upload")
    for i in range(3):
        session = await sessions.ensure_session(session_id=None, workspace_id="ws_u", user_id=_OWNER)
        await sessions.record_turn(turn_id=f"turn_uc_{i}", session_id=session.id, user_text="t")
        await sessions.record_turn_attachments(turn_id=f"turn_uc_{i}", artifact_ids=[used.id])
    counts = await store.usage_counts([used.id, unused.id, "art_missing", ""])
    assert counts == {used.id: 3, unused.id: 0, "art_missing": 0}
    for artifact_id in (used.id, unused.id, "art_missing"):
        assert counts[artifact_id] == await store.usage_count(artifact_id)
