"""原文件丢失对账（artifact-volume 批 0）：判定、两道闸、状态切换与清单排除。"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
from bowhead_core.artifact_reconcile import probe_blob, reconcile_missing_artifacts
from bowhead_core.artifact_store import ArtifactStore, LocalBackend

pytestmark = pytest.mark.unit


@pytest.fixture
async def store(tmp_path: Path) -> ArtifactStore:
    db_path = str(tmp_path / "art.db")
    s = ArtifactStore(db_path=db_path, backend=LocalBackend(root_dir=tmp_path / "blob"))
    await s.initialize()
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, workspace_id TEXT, user_id TEXT, started_at TEXT, last_active_at TEXT);
            CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, session_id TEXT);
            CREATE TABLE IF NOT EXISTS turn_attachments (turn_id TEXT, artifact_id TEXT, seq INTEGER, PRIMARY KEY (turn_id, artifact_id));
            CREATE TABLE IF NOT EXISTS session_attachments (session_id TEXT, artifact_id TEXT, pinned_at TEXT, detached_at TEXT,
                PRIMARY KEY (session_id, artifact_id));
            CREATE TABLE IF NOT EXISTS canvases (id TEXT PRIMARY KEY, session_id TEXT);
        """)
        await db.commit()
    return s


async def _put(store: ArtifactStore, name: str, **kw: object) -> str:
    art = await store.put_bytes(content=b"a,b\n1,2\n", filename=name, mime="text/csv", source="upload", created_by="u1", **kw)  # type: ignore[arg-type]
    return art.id


async def _lose(store: ArtifactStore, aid: str) -> None:
    art = await store.get(aid)
    assert art is not None
    Path(art.path).unlink()


async def _status(store: ArtifactStore, aid: str) -> str:
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT status FROM artifacts WHERE id=?", (aid,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]


# ─── Classification ─────────────────────────────────────────────────────────


class _RaisingBackend:
    async def head(self, path: str) -> None:
        raise PermissionError("denied")


async def test_probe_blob_only_definite_absence_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalBackend(root_dir=tmp_path / "blob")
    present = tmp_path / "blob" / "x.csv"
    present.write_bytes(b"1")
    # Match KB worker fallback: a relative path readable under .data/ is not lost.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".data" / "artifacts").mkdir(parents=True)
    (tmp_path / ".data" / "artifacts" / "y.csv").write_bytes(b"1")

    assert await probe_blob(backend, str(present)) == "present"
    assert await probe_blob(backend, str(tmp_path / "blob" / "gone.csv")) == "absent"
    assert await probe_blob(backend, "artifacts/y.csv") == "present"
    assert await probe_blob(backend, "artifacts-116/gone.csv") == "absent"
    assert await probe_blob(_RaisingBackend(), "any") == "unknown"  # type: ignore[arg-type]


# ─── Reconciliation ─────────────────────────────────────────────────────────


async def test_dry_run_counts_without_changing_rows(store: ArtifactStore) -> None:
    lost = await _put(store, "lost.csv")
    await _lose(store, lost)
    for i in range(5):
        await _put(store, f"ok{i}.csv")

    result = await reconcile_missing_artifacts(store, apply=False)

    assert (result.checked, result.absent, result.skipped_reason) == (6, 1, "dry_run")
    assert await _status(store, lost) == "ready"


async def test_apply_marks_missing_and_restores_when_file_returns(store: ArtifactStore) -> None:
    lost = await _put(store, "lost.csv")
    art = await store.get(lost)
    assert art is not None
    content = Path(art.path).read_bytes()
    await _lose(store, lost)
    for i in range(5):
        await _put(store, f"ok{i}.csv")

    first = await reconcile_missing_artifacts(store, apply=True)
    assert (first.marked, first.applied) == (1, True)
    assert await _status(store, lost) == "missing"

    Path(art.path).write_bytes(content)  # For example, the volume is remounted
    second = await reconcile_missing_artifacts(store, apply=True)
    assert second.restored == 1
    assert await _status(store, lost) == "ready"


async def test_unhealthy_storage_marks_nothing(store: ArtifactStore) -> None:
    """最近的产物也读不到（卷没挂上）⇒ 一律不标；对照：最近的读得到时同一批丢失会被标记。"""
    ids = [await _put(store, f"f{i}.csv") for i in range(10)]
    for aid in ids:
        await _lose(store, aid)

    blocked = await reconcile_missing_artifacts(store, apply=True, force_ratio=True)
    assert (blocked.skipped_reason, blocked.marked) == ("storage_unhealthy", 0)
    assert {await _status(store, aid) for aid in ids} == {"ready"}

    for i in range(20):
        await _put(store, f"new{i}.csv")
    control = await reconcile_missing_artifacts(store, apply=True, force_ratio=True)
    assert control.marked == 10


async def test_ratio_gate_needs_force(store: ArtifactStore) -> None:
    lost = [await _put(store, f"old{i}.csv") for i in range(60)]
    for aid in lost:
        await _lose(store, aid)
    for i in range(20):
        await _put(store, f"new{i}.csv")

    gated = await reconcile_missing_artifacts(store, apply=True)
    assert (gated.absent, gated.marked, gated.skipped_reason) == (60, 0, "ratio_gate")
    forced = await reconcile_missing_artifacts(store, apply=True, force_ratio=True)
    assert forced.marked == 60


async def test_soft_deleted_and_document_copies_are_left_alone(store: ArtifactStore) -> None:
    trashed = await _put(store, "trashed.csv")
    doc_copy = await _put(store, "doc.csv")
    for aid in (trashed, doc_copy):
        await _lose(store, aid)
    for i in range(5):
        await _put(store, f"ok{i}.csv")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE artifacts SET deleted_at='2026-01-01T00:00:00+00:00' WHERE id=?", (trashed,))
        await db.execute("CREATE TABLE document_input_copies (artifact_id TEXT)")
        await db.execute("INSERT INTO document_input_copies VALUES (?)", (doc_copy,))
        await db.commit()

    result = await reconcile_missing_artifacts(store, apply=True)

    assert result.marked == 0
    assert await _status(store, trashed) == "ready"
    assert await _status(store, doc_copy) == "ready"


# ─── Inventory exclusion ────────────────────────────────────────────────────


async def test_missing_rows_leave_files_list_usage_and_model_list(store: ArtifactStore) -> None:
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("INSERT INTO sessions VALUES ('s1','ws','u1','2026-01-01','2026-01-01')")
        await db.commit()
    kept = await _put(store, "kept.csv", session_id="s1")
    gone = await _put(store, "gone.csv", session_id="s1")

    def ids(items: list) -> set[str]:
        return {a.id for a in items}

    # Control: both entries exist before marking.
    items, total, _ = await store.list_for_user(user_id="u1")
    assert ids(items) == {kept, gone} and total == 2
    assert {kept, gone} <= ids(await store.list_usage_artifacts_for_user("u1"))
    assert ids(await store.list_for_session(session_id="s1", user_id="u1")) == {kept, gone}

    assert await store.set_blob_missing([gone], missing=True) == 1

    items, total, _ = await store.list_for_user(user_id="u1")
    assert ids(items) == {kept} and total == 1
    assert gone not in ids(await store.list_usage_artifacts_for_user("u1"))
    assert ids(await store.list_for_session(session_id="s1", user_id="u1")) == {kept}


async def test_pinned_snapshot_skips_missing_but_keeps_objects_without_status() -> None:
    from types import SimpleNamespace

    from bowhead_core.turn_runner import build_pinned_snapshot

    rows = {
        "art_ok": SimpleNamespace(filename="ok.csv", status="ready"),
        "art_gone": SimpleNamespace(filename="gone.csv", status="missing"),
        "art_legacy": SimpleNamespace(filename="legacy.csv"),  # Do not mistakenly discard legacy objects/stand-ins lacking status
    }

    class _Store:
        async def get(self, aid: str) -> object | None:
            return rows.get(aid)

    snapshot = await build_pinned_snapshot(_Store(), ["art_ok", "art_gone", "art_legacy", "art_absent"])

    assert [s["artifact_id"] for s in snapshot] == ["art_ok", "art_legacy"]
