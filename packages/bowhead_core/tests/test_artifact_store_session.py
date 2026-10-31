"""ArtifactStore.list_for_session contract tests.

list_for_session includes only artifacts with an explicit session relationship:
1. turn_attachments (user uploads via turn)
2. session_attachments (pinned)
3. canvas_id → session join
4. created_by_turn → turns.session_id (producer provenance)

Tool outputs must pass session_id when persisted. Time-window inference is forbidden
because the same user can run concurrent sessions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.artifact_store import (
    ArtifactStore,
    LocalBackend,
)

if TYPE_CHECKING:
    from pathlib import Path


def _at(offset_min: int) -> str:
    return (datetime.now(tz=UTC) + timedelta(minutes=offset_min)).isoformat()


@pytest.fixture
async def store(tmp_path: Path) -> ArtifactStore:
    db_path = str(tmp_path / "art.db")
    backend = LocalBackend(root_dir=tmp_path / "blob")
    s = ArtifactStore(db_path=db_path, backend=backend)
    await s.initialize()
    from bowhead_core.turn_log_store import TurnLogStore

    await TurnLogStore(db_path).initialize()
    # Bootstrap minimal sessions / turns / canvases / turn_attachments tables
    # (artifact_store creates only artifacts; session_store and others ensure the remaining tables).
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, workspace_id TEXT, user_id TEXT,
                started_at TEXT, last_active_at TEXT
            );
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY, session_id TEXT
            );
            CREATE TABLE IF NOT EXISTS turn_attachments (
                turn_id TEXT, artifact_id TEXT, seq INTEGER,
                PRIMARY KEY (turn_id, artifact_id)
            );
            CREATE TABLE IF NOT EXISTS session_attachments (
                session_id TEXT, artifact_id TEXT,
                pinned_at TEXT, detached_at TEXT,
                PRIMARY KEY (session_id, artifact_id)
            );
            CREATE TABLE IF NOT EXISTS canvases (
                id TEXT PRIMARY KEY, session_id TEXT
            );
        """)
        await db.commit()
    return s


@pytest.mark.asyncio
async def test_list_for_session_picks_up_tool_output_artifact(
    store: ArtifactStore,
) -> None:
    """显式 session_id 会把 tool output 关联到当前 session."""
    sess_start = _at(-30)
    sess_active = _at(-1)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            ("s_t1", "1", "u1", sess_start, sess_active),
        )
        await db.commit()

    # Create artifact mid-session via bi_fetch_rows source
    art = await store.put_bytes(
        content=b"col_a,col_b\n1,2\n",
        filename="data.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
        session_id="s_t1",
    )

    items = await store.list_for_session(session_id="s_t1", user_id="u1")
    assert len(items) == 1, f"expected the bi_fetch_rows artifact to surface, got {items}"
    assert items[0].id == art.id


@pytest.mark.asyncio
async def test_list_for_session_isolates_by_user(
    store: ArtifactStore,
) -> None:
    """别用户 created_by 不应被列出（防越权）."""
    sess_start, sess_active = _at(-30), _at(-1)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            ("s_t1", "1", "u1", sess_start, sess_active),
        )
        await db.commit()
    # u2 produced an artifact in u1's session window — should NOT show up
    await store.put_bytes(
        content=b"x",
        filename="other.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u2",
    )
    items = await store.list_for_session(session_id="s_t1", user_id="u1")
    assert items == []


@pytest.mark.asyncio
async def test_list_for_session_isolates_concurrent_sessions_for_same_user(
    store: ArtifactStore,
) -> None:
    """同一用户的并发 session 不能靠重叠时间窗互相收录运行产物."""
    sess_start, sess_active = _at(-30), _at(-1)
    async with aiosqlite.connect(store.db_path) as db:
        await db.executemany(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            (
                ("s_left", "1", "u1", sess_start, sess_active),
                ("s_right", "1", "u1", sess_start, sess_active),
            ),
        )
        await db.commit()

    right_artifact = await store.put_bytes(
        content=b"right session only",
        filename="right.txt",
        mime="text/plain",
        source="python_sandbox",
        created_by="u1",
        session_id="s_right",
    )

    left_items = await store.list_for_session(session_id="s_left", user_id="u1")
    right_items = await store.list_for_session(session_id="s_right", user_id="u1")

    assert right_artifact.id not in {item.id for item in left_items}
    assert right_artifact.id in {item.id for item in right_items}


@pytest.mark.asyncio
async def test_list_for_session_does_not_infer_unlinked_artifacts_from_time(
    store: ArtifactStore,
) -> None:
    """同用户且创建时间重叠，也不能替代显式 session 关系."""
    sess_start = _at(-30)
    sess_active = _at(-1)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            ("s_t1", "1", "u1", sess_start, sess_active),
        )
        await db.commit()

    unlinked = await store.put_bytes(
        content=b"x",
        filename="unlinked.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
    )
    linked = await store.put_bytes(
        content=b"x",
        filename="linked.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
        session_id="s_t1",
    )

    items = await store.list_for_session(session_id="s_t1", user_id="u1")
    ids = {a.id for a in items}
    assert linked.id in ids
    assert unlinked.id not in ids


@pytest.mark.asyncio
async def test_list_for_session_includes_turn_attachment(
    store: ArtifactStore,
) -> None:
    """Source=upload 走 turn_attachments 精确关系链路."""
    sess_start, sess_active = _at(-30), _at(-1)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            ("s_t1", "1", "u1", sess_start, sess_active),
        )
        await db.execute("INSERT INTO turns(id, session_id) VALUES ('t_t1', 's_t1')")
        await db.commit()

    art = await store.put_bytes(
        content=b"x",
        filename="upload.csv",
        mime="text/csv",
        source="upload",
        created_by="u1",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO turn_attachments(turn_id, artifact_id, seq) VALUES (?, ?, 0)",
            ("t_t1", art.id),
        )
        await db.commit()

    items = await store.list_for_session(session_id="s_t1", user_id="u1")
    assert any(a.id == art.id for a in items)


@pytest.mark.asyncio
async def test_list_for_session_includes_artifact_created_by_session_turn(
    store: ArtifactStore,
) -> None:
    """Producer turn 是精确关系，可兼容没有 session pin 的历史运行产物."""
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            ("s_t1", "1", "u1", _at(-30), _at(-1)),
        )
        await db.execute("INSERT INTO turns(id, session_id) VALUES ('t_t1', 's_t1')")
        await db.commit()

    artifact = await store.put_bytes(
        content=b"generated",
        filename="generated.txt",
        mime="text/plain",
        source="python_sandbox",
        created_by="u1",
        created_by_turn="t_t1",
    )

    items = await store.list_for_session(session_id="s_t1", user_id="u1")
    assert artifact.id in {item.id for item in items}


@pytest.mark.asyncio
async def test_list_for_session_with_no_session_returns_empty(
    store: ArtifactStore,
) -> None:
    """Session 不存在 (no row) → 空 list (而不是 crash)."""
    await store.put_bytes(
        content=b"x",
        filename="orphan.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
    )
    items = await store.list_for_session(
        session_id="s_nonexistent",
        user_id="u1",
    )
    assert items == []


@pytest.mark.asyncio
async def test_list_for_session_explicit_relation_ignores_artifact_timestamp(
    store: ArtifactStore,
) -> None:
    """显式 session 关系是归属真相，不受 artifact 创建时间影响."""
    sess_start = "2026-04-26T03:00:00+00:00"
    sess_active = "2026-04-26T03:30:00+00:00"
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            ("s_iso", "1", "u1", sess_start, sess_active),
        )
        await db.commit()

    art = await store.put_bytes(
        content=b"x",
        filename="iso.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
        session_id="s_iso",
    )
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE artifacts SET created_at = ? WHERE id = ?",
            ("2026-04-26T03:15:00+00:00", art.id),
        )
        await db.commit()

    items = await store.list_for_session(session_id="s_iso", user_id="u1")
    assert any(a.id == art.id for a in items)


@pytest.mark.asyncio
async def test_results_snapshot_filters_internal_artifacts_before_pagination(
    store: ArtifactStore,
) -> None:
    sess_start, sess_active = _at(-30), _at(-1)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            ("s_results", "1", "u1", sess_start, sess_active),
        )
        await db.commit()

    manifest = await store.put_bytes(
        content=b"{}",
        filename="opaque.json",
        mime="application/json",
        source="canvas_export",
        kind="deliverable",
        producer_kind="canvas_export_manifest_b1a_v1",
        session_id="s_results",
        created_by="u1",
    )
    output = await store.put_bytes(
        content=b"PDF",
        filename="report.pdf",
        mime="application/pdf",
        source="canvas_export",
        kind="deliverable",
        producer_kind="canvas_export_b1a_v1_pdf_v2",
        manifest_artifact_id=manifest.id,
        session_id="s_results",
        created_by="u1",
    )
    render_input = await store.put_bytes(
        content=b"{}",
        filename="opaque-input.json",
        mime="application/json",
        source="canvas_export",
        kind="tool_data",
        producer_kind="canvas_export",
        session_id="s_results",
        created_by="u1",
    )
    bundle_root = await store.put_bytes(
        content=b"ZIP",
        filename="bundle.zip",
        mime="application/zip",
        source="kb_bundle_upload",
        kind="user_upload",
        producer_kind="kb_bundle_upload",
        session_id="s_results",
        created_by="u1",
    )

    unfiltered = await store.list_for_session(
        session_id="s_results",
        user_id="u1",
    )
    assert {item.id for item in unfiltered} == {
        manifest.id,
        output.id,
        render_input.id,
        bundle_root.id,
    }

    visible = await store.list_for_session(
        session_id="s_results",
        user_id="u1",
        limit=1,
        results_visible_only=True,
    )
    assert [item.id for item in visible] == [output.id]


@pytest.mark.asyncio
async def test_results_snapshot_only_hides_manifests_linked_by_owned_trusted_outputs(
    store: ArtifactStore,
) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.executemany(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            (
                ("s_results_victim", "1", "u1", _at(-30), _at(-1)),
                ("s_results_attacker", "1", "u2", _at(-30), _at(-1)),
            ),
        )
        await db.commit()

    target = await store.put_bytes(
        content=b"victim",
        filename="victim-result.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        kind="tool_data",
        session_id="s_results_victim",
        created_by="u1",
    )
    await store.put_bytes(
        content=b"attacker",
        filename="attacker.pdf",
        mime="application/pdf",
        source="canvas_export",
        kind="deliverable",
        producer_kind="canvas_export_b1a_v1_pdf_v2",
        manifest_artifact_id=target.id,
        session_id="s_results_attacker",
        created_by="u2",
    )
    await store.put_bytes(
        content=b"untrusted",
        filename="untrusted.pdf",
        mime="application/pdf",
        source="upload",
        kind="user_upload",
        producer_kind="upload",
        manifest_artifact_id=target.id,
        session_id="s_results_victim",
        created_by="u1",
    )

    visible = await store.list_for_session(
        session_id="s_results_victim",
        user_id="u1",
        results_visible_only=True,
    )
    assert target.id in {item.id for item in visible}

    await store.put_bytes(
        content=b"trusted",
        filename="trusted.pdf",
        mime="application/pdf",
        source="canvas_export",
        kind="deliverable",
        producer_kind="canvas_export_b1a_v1_pdf_v2",
        manifest_artifact_id=target.id,
        session_id="s_results_victim",
        created_by="u1",
    )
    visible = await store.list_for_session(
        session_id="s_results_victim",
        user_id="u1",
        results_visible_only=True,
    )
    assert target.id not in {item.id for item in visible}


@pytest.mark.asyncio
async def test_results_snapshot_returns_100_visible_objects_with_stable_tie_order(
    store: ArtifactStore,
) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO sessions(id, workspace_id, user_id, started_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
            ("s_scale", "1", "u1", _at(-30), _at(-1)),
        )
        await db.commit()

    visible_ids: list[str] = []
    for index in range(100):
        artifact = await store.put_bytes(
            content=b"x",
            filename=f"result-{index:03}.csv",
            mime="text/csv",
            source="bi_fetch_rows",
            kind="tool_data",
            session_id="s_scale",
            created_by="u1",
        )
        visible_ids.append(artifact.id)
    internal = await store.put_bytes(
        content=b"ZIP",
        filename="bundle.zip",
        mime="application/zip",
        source="kb_bundle_upload",
        kind="user_upload",
        producer_kind="kb_bundle_upload",
        session_id="s_scale",
        created_by="u1",
    )
    all_ids = [*visible_ids, internal.id]
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE artifacts SET created_at=? WHERE id IN (" + ",".join("?" for _ in all_ids) + ")",
            ("2026-08-07T00:00:00+00:00", *all_ids),
        )
        await db.commit()

    snapshot = await store.list_for_session(
        session_id="s_scale",
        user_id="u1",
        limit=100,
        results_visible_only=True,
    )
    assert len(snapshot) == 100
    assert [item.id for item in snapshot] == sorted(visible_ids, reverse=True)


# ─────────────────────────────────────────────────────────────
# #54 long-term fix (2026-04-29): owner check + nightly GC
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_with_owner_check_silent_denial(tmp_path: Path) -> None:
    """check_created_by 不一致返 None (假装不存在), 防侧信道泄露."""
    store = ArtifactStore(
        db_path=tmp_path / "a.db",
        backend=LocalBackend(tmp_path),
    )
    await store.initialize()
    art = await store.put_bytes(
        content=b"x",
        filename="x.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="alice",
    )
    # alice can fetch her own artifact.
    got_a = await store.get(art.id, check_created_by="alice")
    assert got_a is not None and got_a.id == art.id
    # bob fetch → None (silent deny).
    got_b = await store.get(art.id, check_created_by="bob")
    assert got_b is None
    # Fetches without check_created_by still work (backward compatibility for internal helpers).
    got_internal = await store.get(art.id)
    assert got_internal is not None


@pytest.mark.asyncio
async def test_hard_delete_old_soft_deleted(tmp_path: Path) -> None:
    """30 天前的软删 row 应被硬删 + 文件清."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from bowhead_core import migrations

    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'a.db'}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")
    store = ArtifactStore(
        db_path=tmp_path / "a.db",
        backend=LocalBackend(tmp_path),
    )
    await store.initialize()
    art_old = await store.put_bytes(
        content=b"old",
        filename="old.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
    )
    art_recent = await store.put_bytes(
        content=b"recent",
        filename="recent.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
    )
    art_active = await store.put_bytes(
        content=b"active",
        filename="active.csv",
        mime="text/csv",
        source="bi_fetch_rows",
        created_by="u1",
    )
    # Soft-delete old and set deleted_at to 31 days ago.
    old_ts = (datetime.now(tz=UTC) - timedelta(days=31)).isoformat()
    recent_ts = (datetime.now(tz=UTC) - timedelta(days=5)).isoformat()
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE artifacts SET deleted_at = ? WHERE id = ?",
            (old_ts, art_old.id),
        )
        await db.execute(
            "UPDATE artifacts SET deleted_at = ? WHERE id = ?",
            (recent_ts, art_recent.id),
        )
        await db.commit()

    stats = await store.hard_delete_old_soft_deleted(days=30)
    assert stats["rows_removed"] == 1, "仅 31 天前的应被删, 5 天前 + active 保留"
    assert stats["files_removed"] >= 0  # The local backend file may already be cleaned up.

    # active + recent remain; old is actually gone.
    async with aiosqlite.connect(store.db_path) as db:
        cur = await db.execute("SELECT id FROM artifacts")
        rows = await cur.fetchall()
        ids = {r[0] for r in rows}
    assert art_active.id in ids
    assert art_recent.id in ids
    assert art_old.id not in ids


@pytest.mark.asyncio
async def test_find_turn_artifact_ids_by_filename_scopes_to_the_turn_and_takes_latest(tmp_path) -> None:
    """沙箱入口按文件名找回本 turn 上次运行写出的文件：只认本 turn、同名取最近一份。"""
    import asyncio

    from bowhead_core.artifact_store import ArtifactStore, LocalBackend

    store = ArtifactStore(db_path=str(tmp_path / "a.db"), backend=LocalBackend(root_dir=str(tmp_path / "blobs")))
    await store.initialize()
    await store.put_bytes(created_by=None, content=b"x", filename="m.csv", mime="text/csv", source="python_sandbox", created_by_turn="t_other")
    first = await store.put_bytes(created_by=None, content=b"1", filename="m.csv", mime="text/csv", source="python_sandbox", created_by_turn="t1")
    assert await store.find_turn_artifact_ids_by_filename("t1", ["m.csv", "none.csv"]) == {"m.csv": first.id}
    await asyncio.sleep(0.01)
    latest = await store.put_bytes(created_by=None, content=b"2", filename="m.csv", mime="text/csv", source="python_sandbox", created_by_turn="t1")
    assert await store.find_turn_artifact_ids_by_filename("t1", ["m.csv"]) == {"m.csv": latest.id}
    assert await store.find_turn_artifact_ids_by_filename("t2", ["m.csv"]) == {}
