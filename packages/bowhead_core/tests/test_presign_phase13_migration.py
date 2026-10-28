"""Phase 1.3 schema migration UT.

Covers:
  - artifacts table: status + expires_at columns added via PRAGMA+ALTER
  - artifacts: idx_artifacts_pending partial index created
  - kb_files: parse_status CHECK constraint expanded via copy-migrate
  - idempotent re-runs (no-op on already-migrated DBs)
  - data preservation (existing rows survive copy-migrate)
  - schemas.py KbFileDTO.parse_status Literal includes new values
  - put_bytes() regression: existing INSERT path still works without explicit status
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest
from alembic import command
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.kb.files_store import (
    _PARSE_STATUS_VALUES,
    _TABLE_SCHEMA,
    KbFilesStore,
)
from bowhead_core.kb.schemas import KbFileDTO
from test_artifact_workspace_cutover_migration import _config

if TYPE_CHECKING:
    from pathlib import Path

# ── ArtifactStore schema migration ────────────────────────────────


@pytest.fixture
async def fresh_store(tmp_path: Path) -> ArtifactStore:
    """Fresh DB → initialize → returns a store ready to insert."""
    s = ArtifactStore(
        db_path=str(tmp_path / "art.db"),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_artifacts_status_column_added(fresh_store: ArtifactStore) -> None:
    async with aiosqlite.connect(fresh_store.db_path) as db:
        cur = await db.execute("PRAGMA table_info(artifacts)")
        cols = {row[1]: row[4] for row in await cur.fetchall()}  # name → dflt_value
        assert "status" in cols, "status column should be added by Phase 1.3 migration"
        # Default 'ready' is stored as quoted string in PRAGMA output
        assert cols["status"] == "'ready'"
        assert "expires_at" in cols
        assert cols["expires_at"] is None  # NULL default


@pytest.mark.asyncio
async def test_artifacts_pending_index_created(fresh_store: ArtifactStore) -> None:
    async with aiosqlite.connect(fresh_store.db_path) as db:
        cur = await db.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type='index' AND tbl_name='artifacts'",
        )
        rows = await cur.fetchall()
        names = {r[0] for r in rows}
        assert "idx_artifacts_pending" in names
        # Verify it's a partial index (WHERE clause)
        pending_sql = next(r[1] for r in rows if r[0] == "idx_artifacts_pending")
        assert "WHERE status='pending_upload'" in pending_sql


@pytest.mark.asyncio
async def test_artifacts_initialize_idempotent(tmp_path: Path) -> None:
    """Calling initialize() twice must NOT fail (e.g. duplicate ALTER COLUMN)."""
    s = ArtifactStore(
        db_path=str(tmp_path / "idem.db"),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await s.initialize()
    await s.initialize()  # should be no-op
    async with aiosqlite.connect(s.db_path) as db:
        cur = await db.execute("PRAGMA table_info(artifacts)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "status" in cols and "expires_at" in cols


@pytest.mark.asyncio
async def test_put_bytes_inserts_with_default_status_ready(fresh_store: ArtifactStore) -> None:
    """Existing put_bytes() callsite doesn't supply status → DEFAULT 'ready'.

    Per Phase 1.2 review P1-M-5: ensure unmodified INSERT path keeps working
    post-migration without callsite changes.
    """
    art = await fresh_store.put_bytes(
        content=b"hello",
        filename="x.txt",
        mime="text/plain",
        source="upload",
        created_by="u_test",
    )
    async with aiosqlite.connect(fresh_store.db_path) as db:
        cur = await db.execute(
            "SELECT status, expires_at FROM artifacts WHERE id=?",
            (art.id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "ready", "default DEFAULT 'ready' must apply"
    assert row[1] is None, "expires_at NULL for non-pending row"


@pytest.mark.asyncio
async def test_artifacts_legacy_db_upgrades_via_alter(tmp_path: Path) -> None:
    """Simulate legacy DB without status/expires_at; initialize() adds them."""
    db_path = tmp_path / "legacy.db"
    # Bootstrap a pre-Phase-1.3 schema by hand
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, filename TEXT, mime TEXT, size INTEGER,
                source TEXT, path TEXT, canvas_id TEXT, created_by TEXT,
                created_at TEXT
            )
        """)
        await db.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("art_legacy00001", "old.txt", "text/plain", 5, "upload", "/tmp/old", None, "u_legacy", "2025-01-01T00:00:00+00:00"),
        )
        await db.commit()

    # Now run Phase 1.3 initialize() — must ALTER ADD COLUMN without breaking
    s = ArtifactStore(
        db_path=str(db_path),
        backend=LocalBackend(root_dir=tmp_path / "blobs"),
    )
    await s.initialize()

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("PRAGMA table_info(artifacts)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "status" in cols and "expires_at" in cols
        # Legacy row still readable
        cur = await db.execute("SELECT status, expires_at FROM artifacts WHERE id=?", ("art_legacy00001",))
        row = await cur.fetchone()
    # Legacy row gets 'ready' from the DEFAULT applied by ALTER ADD COLUMN
    assert row is not None
    assert row[0] == "ready"
    assert row[1] is None


# ── KbFilesStore parse_status CHECK migration ─────────────────────


@pytest.fixture
async def fresh_kb_store(tmp_path: Path) -> KbFilesStore:
    s = KbFilesStore(tmp_path / "kb.db")
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_kb_files_fresh_db_has_expanded_check(fresh_kb_store: KbFilesStore) -> None:
    """Fresh DB created via _TABLE_SCHEMA already has the expanded CHECK."""
    async with aiosqlite.connect(fresh_kb_store._db_path) as db:
        cur = await db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='kb_files'",
        )
        row = await cur.fetchone()
    assert row is not None
    sql = row[0] or ""
    assert "'awaiting_upload'" in sql
    assert "'queued'" in sql


@pytest.mark.asyncio
async def test_kb_files_new_status_values_accepted(fresh_kb_store: KbFilesStore) -> None:
    """The two new states must INSERT without CHECK rejection."""
    async with aiosqlite.connect(fresh_kb_store._db_path) as db:
        # Sanity insert with awaiting_upload
        await db.execute(
            """INSERT INTO kb_files(file_id, artifact_id, filename, mime_type,
                size_bytes, scope, scope_id, workspace_id, team_id,
                uploaded_by, uploaded_at, parse_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "kbf_test_aw_1",
                "art_aw_1",
                "x.bin",
                "application/octet-stream",
                100,
                "personal",
                "u_t",
                "",
                "",
                "u_t",
                "2026-05-27T00:00:00+00:00",
                "awaiting_upload",
            ),
        )
        await db.execute(
            """INSERT INTO kb_files(file_id, artifact_id, filename, mime_type,
                size_bytes, scope, scope_id, workspace_id, team_id,
                uploaded_by, uploaded_at, parse_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "kbf_test_q_1",
                "art_q_1",
                "x.bin",
                "application/octet-stream",
                100,
                "personal",
                "u_t",
                "",
                "",
                "u_t",
                "2026-05-27T00:00:00+00:00",
                "queued",
            ),
        )
        await db.commit()
        # Old states still work
        await db.execute(
            """INSERT INTO kb_files(file_id, artifact_id, filename, mime_type,
                size_bytes, scope, scope_id, workspace_id, team_id,
                uploaded_by, uploaded_at, parse_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "kbf_test_done_1",
                "art_done_1",
                "x.bin",
                "application/octet-stream",
                100,
                "personal",
                "u_t",
                "",
                "",
                "u_t",
                "2026-05-27T00:00:00+00:00",
                "done",
            ),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_kb_files_check_rejects_unknown_state(fresh_kb_store: KbFilesStore) -> None:
    """A non-enum state must STILL fail CHECK — migration didn't widen too far."""
    async with aiosqlite.connect(fresh_kb_store._db_path) as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                """INSERT INTO kb_files(file_id, artifact_id, filename, mime_type,
                    size_bytes, scope, scope_id, workspace_id, team_id,
                    uploaded_by, uploaded_at, parse_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "kbf_bad",
                    "art_bad",
                    "x.bin",
                    "application/octet-stream",
                    100,
                    "personal",
                    "u_t",
                    "",
                    "",
                    "u_t",
                    "2026-05-27T00:00:00+00:00",
                    "bogus_state",
                ),
            )
            await db.commit()


@pytest.mark.asyncio
async def test_kb_files_copy_migrate_preserves_legacy_rows(tmp_path: Path) -> None:
    """Pre-Phase-1.3 DB (CHECK without 'awaiting_upload') → copy-migrate must
    preserve every row + indexes. Data preservation invariant.
    """
    db_path = tmp_path / "kb-legacy.db"
    # Bootstrap legacy schema by hand (CHECK lists only the original 6 states)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE kb_files (
                file_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                scope TEXT NOT NULL CHECK (scope IN ('personal', 'workspace', 'team')),
                scope_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT '',
                team_id TEXT NOT NULL DEFAULT '',
                uploaded_by TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                parse_status TEXT NOT NULL DEFAULT 'pending'
                              CHECK (parse_status IN ('pending', 'parsing', 'chunking', 'embedding', 'done', 'failed')),
                parse_progress_pct INTEGER,
                parse_layer TEXT,
                parse_error TEXT,
                parse_duration_ms INTEGER,
                chunks_count INTEGER NOT NULL DEFAULT 0,
                cite_count INTEGER NOT NULL DEFAULT 0,
                last_cited_at TEXT,
                use_llm_marker INTEGER NOT NULL DEFAULT 0,
                doc_type_id TEXT,
                summary_json TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                soft_deleted_at TEXT
            );
            CREATE INDEX idx_kb_files_scope ON kb_files(scope, scope_id);
        """)
        # Insert 3 legacy rows in various states
        rows = [
            ("kbf_l1", "art_l1", "a.pdf", "application/pdf", 1000, "personal", "u_a", "", "", "u_a", "2025-12-01T00:00:00+00:00", "done"),
            (
                "kbf_l2",
                "art_l2",
                "b.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                2000,
                "workspace",
                "ws_b",
                "ws_b",
                "",
                "u_b",
                "2025-12-02T00:00:00+00:00",
                "failed",
            ),
            ("kbf_l3", "art_l3", "c.csv", "text/csv", 500, "team", "team_c", "", "team_c", "u_c", "2025-12-03T00:00:00+00:00", "pending"),
        ]
        for r in rows:
            await db.execute(
                """INSERT INTO kb_files(file_id, artifact_id, filename, mime_type,
                    size_bytes, scope, scope_id, workspace_id, team_id,
                    uploaded_by, uploaded_at, parse_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                r,
            )
        await db.commit()

    # Run Phase 1.3 migration
    s = KbFilesStore(db_path)
    await s.initialize()

    async with aiosqlite.connect(db_path) as db:
        # Check expanded CHECK
        cur = await db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='kb_files'",
        )
        sql = (await cur.fetchone())[0] or ""
        assert "'awaiting_upload'" in sql
        assert "'queued'" in sql

        # Legacy rows preserved
        cur = await db.execute(
            "SELECT file_id, parse_status FROM kb_files ORDER BY file_id",
        )
        preserved = await cur.fetchall()
        assert len(preserved) == 3
        assert preserved[0] == ("kbf_l1", "done")
        assert preserved[1] == ("kbf_l2", "failed")
        assert preserved[2] == ("kbf_l3", "pending")

        # Indexes recreated
        cur = await db.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' AND tbl_name='kb_files'",
        )
        idx_names = {r[0] for r in await cur.fetchall()}
        assert "idx_kb_files_scope" in idx_names

        # New states now accept inserts
        await db.execute(
            """INSERT INTO kb_files(file_id, artifact_id, filename, mime_type,
                size_bytes, scope, scope_id, workspace_id, team_id,
                uploaded_by, uploaded_at, parse_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "kbf_post_mig",
                "art_pm",
                "p.bin",
                "application/octet-stream",
                100,
                "personal",
                "u_t",
                "",
                "",
                "u_t",
                "2026-05-27T00:00:00+00:00",
                "awaiting_upload",
            ),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_kb_files_migration_idempotent(tmp_path: Path) -> None:
    """Initialize twice on already-migrated DB → second pass is no-op."""
    s = KbFilesStore(tmp_path / "idem-kb.db")
    await s.initialize()
    # Insert a row
    async with aiosqlite.connect(s._db_path) as db:
        await db.execute(
            """INSERT INTO kb_files(file_id, artifact_id, filename, mime_type,
                size_bytes, scope, scope_id, workspace_id, team_id,
                uploaded_by, uploaded_at, parse_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "kbf_idem",
                "art_idem",
                "i.bin",
                "application/octet-stream",
                100,
                "personal",
                "u_t",
                "",
                "",
                "u_t",
                "2026-05-27T00:00:00+00:00",
                "queued",
            ),
        )
        await db.commit()
    # Second initialize — must be no-op, row survives
    await s.initialize()
    async with aiosqlite.connect(s._db_path) as db:
        cur = await db.execute("SELECT parse_status FROM kb_files WHERE file_id=?", ("kbf_idem",))
        row = await cur.fetchone()
    assert row is not None and row[0] == "queued"


@pytest.mark.asyncio
async def test_kb_files_migrates_post_parse_status_org_scope_schema_without_data_loss(
    tmp_path: Path,
) -> None:
    """The production 0.87.3 shape already has new parse states and ``space``.

    Its only stale constraint is ``scope='org'``.  That shape must not hit the
    old substring guard, and the copy must preserve columns added after the
    original migration was written.
    """
    db_path = tmp_path / "kb-org-scope.db"
    legacy_schema = _TABLE_SCHEMA.replace(
        "scope IN ('personal', 'space', 'workspace', 'team')",
        "scope IN ('personal', 'space', 'workspace', 'org')",
    )
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(legacy_schema)
        await db.execute(
            """INSERT INTO kb_files(
                file_id, artifact_id, filename, mime_type, size_bytes,
                scope, scope_id, workspace_id, team_id, uploaded_by, uploaded_at,
                parse_status, auto_classified_doc_type_id, extracted_rows_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "kbf_org_legacy",
                "art_org_legacy",
                "legacy.pdf",
                "application/pdf",
                42,
                "org",
                "team_legacy",
                "",
                "team_legacy",
                "u_legacy",
                "2026-07-20T00:00:00+00:00",
                "queued",
                "dt_auto_keep",
                "succeeded",
            ),
        )
        await db.commit()

    store = KbFilesStore(db_path)
    await store.initialize()
    await store.initialize()
    # Columns after 1.0.0 come only from Alembic (数据库迁移.md); Store first aligns old DBs to 1.0.0, then upgrades to head.
    command.upgrade(_config(db_path, "files"), "head")

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='kb_files'",
        )
        sql = (await cur.fetchone())[0] or ""
        assert "'team'" in sql
        assert "'org'" not in sql

        cur = await db.execute(
            """SELECT scope, scope_id, team_id, parse_status,
                      auto_classified_doc_type_id, extracted_rows_status
               FROM kb_files WHERE file_id = ?""",
            ("kbf_org_legacy",),
        )
        row = await cur.fetchone()
        assert row == (
            "team",
            "team_legacy",
            "team_legacy",
            "queued",
            "dt_auto_keep",
            "succeeded",
        )

        cur = await db.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name='kb_files_new'",
        )
        assert await cur.fetchone() is None

    await store.create(
        artifact_id="art_team_new",
        filename="new.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        scope="team",
        scope_id="team_legacy",
        workspace_id="",
        team_id="team_legacy",
        uploaded_by="u_legacy",
    )


# ── DTO Literal alignment ─────────────────────────────────────────


def test_kb_file_dto_literal_includes_new_states() -> None:
    """KbFileDTO.parse_status Literal must include the 2 new states.

    P1-M-7: 3-file atomic coordination — CHECK + Literal + FE TS mirror.
    Backend halves verified here; FE side will add a round-trip fixture in
    Phase 2 (P1-C-9 from review-r1).
    """
    # Build a DTO with each new state — must not raise validation error
    for state in ("awaiting_upload", "queued"):
        dto = KbFileDTO(
            file_id="kbf_x",
            artifact_id="art_x",
            filename="x.bin",
            mime_type="application/octet-stream",
            size_bytes=100,
            scope="personal",
            scope_id="u_t",
            workspace_id="",
            team_id="",
            uploaded_by="u_t",
            uploaded_at="2026-05-27T00:00:00+00:00",
            parse_status=state,
        )
        assert dto.parse_status == state


def test_kb_file_dto_literal_rejects_unknown_state() -> None:
    """Pydantic should still reject states outside the union."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KbFileDTO(
            file_id="kbf_x",
            artifact_id="art_x",
            filename="x.bin",
            mime_type="application/octet-stream",
            size_bytes=100,
            scope="personal",
            scope_id="u_t",
            workspace_id="",
            team_id="",
            uploaded_by="u_t",
            uploaded_at="2026-05-27T00:00:00+00:00",
            parse_status="bogus_state",
        )


def test_parse_status_values_constant_matches_check() -> None:
    """The exported constant must list exactly the states the CHECK allows."""
    expected = {"pending", "parsing", "chunking", "embedding", "done", "failed", "awaiting_upload", "queued"}
    assert set(_PARSE_STATUS_VALUES) == expected


# ── review P1-COV-1: atomicity / rollback on mid-migrate failure ──


@pytest.mark.asyncio
async def test_kb_files_migration_rolls_back_on_insert_failure(tmp_path: Path, monkeypatch) -> None:
    """If INSERT SELECT fails mid-migrate, original kb_files survives intact +
    no orphan kb_files_new is left around.

    Verifies the P0-MIG-1 BEGIN IMMEDIATE / ROLLBACK fix.
    """
    db_path = tmp_path / "atomic-kb.db"
    # Legacy schema with 2 rows
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE kb_files (
                file_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                scope TEXT NOT NULL CHECK (scope IN ('personal', 'workspace', 'team')),
                scope_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL DEFAULT '',
                team_id TEXT NOT NULL DEFAULT '',
                uploaded_by TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                parse_status TEXT NOT NULL DEFAULT 'pending'
                              CHECK (parse_status IN ('pending', 'parsing', 'chunking', 'embedding', 'done', 'failed')),
                parse_progress_pct INTEGER,
                parse_layer TEXT,
                parse_error TEXT,
                parse_duration_ms INTEGER,
                chunks_count INTEGER NOT NULL DEFAULT 0,
                cite_count INTEGER NOT NULL DEFAULT 0,
                last_cited_at TEXT,
                use_llm_marker INTEGER NOT NULL DEFAULT 0,
                doc_type_id TEXT,
                summary_json TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                soft_deleted_at TEXT
            );
        """)
        await db.execute(
            """INSERT INTO kb_files(file_id, artifact_id, filename, mime_type,
                size_bytes, scope, scope_id, workspace_id, team_id,
                uploaded_by, uploaded_at, parse_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("kbf_pre1", "art_pre1", "p.pdf", "application/pdf", 100, "personal", "u_t", "", "", "u_t", "2026-05-27T00:00:00+00:00", "done"),
        )
        await db.execute(
            """INSERT INTO kb_files(file_id, artifact_id, filename, mime_type,
                size_bytes, scope, scope_id, workspace_id, team_id,
                uploaded_by, uploaded_at, parse_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("kbf_pre2", "art_pre2", "q.pdf", "application/pdf", 200, "personal", "u_t", "", "", "u_t", "2026-05-27T00:00:00+00:00", "failed"),
        )
        await db.commit()

    # Inject a failure midway through the migrate: monkeypatch aiosqlite's
    # execute to raise specifically on the INSERT INTO kb_files_new SQL.
    import aiosqlite as _aiosqlite_real

    original_execute = _aiosqlite_real.Connection.execute

    async def explode_on_insert_select(self, sql, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(sql, str) and sql.startswith("INSERT INTO kb_files_new"):
            raise RuntimeError("simulated mid-migrate failure")
        return await original_execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(_aiosqlite_real.Connection, "execute", explode_on_insert_select)

    s = KbFilesStore(db_path)
    with pytest.raises(RuntimeError, match="simulated mid-migrate failure"):
        await s.initialize()

    # Restore real execute before verifying
    monkeypatch.setattr(_aiosqlite_real.Connection, "execute", original_execute)

    # Verify atomicity: original kb_files still has 2 rows with OLD CHECK,
    # AND no orphan kb_files_new sits in the schema.
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'kb_files%'",
        )
        tables = {r[0] for r in await cur.fetchall()}
        assert "kb_files" in tables
        assert "kb_files_new" not in tables, "orphan table must be rolled back"
        cur = await db.execute("SELECT COUNT(*) FROM kb_files")
        count = (await cur.fetchone())[0]
        assert count == 2, "all original rows must survive ROLLBACK"
        # Old CHECK still in place
        cur = await db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='kb_files'",
        )
        sql = (await cur.fetchone())[0] or ""
        assert "'awaiting_upload'" not in sql, "CHECK constraint must still be the old one"


# ── review P1-COV-2: EXPLAIN QUERY PLAN for sweeper index ─────────


@pytest.mark.asyncio
async def test_pending_index_used_by_sweeper_query(fresh_store: ArtifactStore) -> None:
    """The partial index `idx_artifacts_pending` MUST be used by the sweeper
    query pattern in Phase 1.5; without index, sweeper does full table scan.
    """
    async with aiosqlite.connect(fresh_store.db_path) as db:
        cur = await db.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT id, path, expires_at FROM artifacts "
            "WHERE status='pending_upload' AND expires_at < '2026-01-01T00:00:00+00:00'",
        )
        plan_rows = await cur.fetchall()
        plan = " ".join(str(r) for r in plan_rows)
    assert "idx_artifacts_pending" in plan, f"sweeper query must hit idx_artifacts_pending; plan={plan}"
