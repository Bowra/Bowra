"""KbFilesStore — kb_files SQL CRUD.

Tech design v3.6 §3.8 + §2.2. 跟 V4 MemoryNotesStore 同模式 (aiosqlite + initialize).

P1-O6: workspace_id NOT NULL DEFAULT '' (team scope 文件填空 string)
P1-O10: kb_files_store.py 单独模块
P1-S3: idx_kb_files_workspace 加 workspace_id != '' partial index
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core.kb.parse_recovery import (
    UNFINISHED_PARSE_STATUSES,
    heartbeat_iso,
    is_parse_stuck,
    parse_error_code,
    stuck_cutoff,
)
from bowhead_core.kb.schemas import KbFileDTO, KbTruncatedRows


@dataclass(frozen=True, slots=True)
class KbReadinessStats:
    active_file_count: int
    image_file_count: int
    scanned_pdf_evidence_count: int


# Sprint file-upload-presign Phase 1.3: 'awaiting_upload' / 'queued' added.
# Fresh DBs use this directly. Legacy DBs go through _migrate_parse_status_check
# below (SQLite cannot ALTER CHECK in place).
_PARSE_STATUS_VALUES = (
    "pending",
    "parsing",
    "chunking",
    "embedding",
    "done",
    "failed",
    "awaiting_upload",
    "queued",
)
_PARSE_STATUS_CHECK_SQL = "CHECK (parse_status IN (" + ", ".join(f"'{v}'" for v in _PARSE_STATUS_VALUES) + "))"

_TABLE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS kb_files (
    file_id           TEXT PRIMARY KEY,
    artifact_id       TEXT NOT NULL,
    filename          TEXT NOT NULL,
    mime_type         TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL,
    scope             TEXT NOT NULL CHECK (scope IN ('personal', 'space', 'workspace', 'team')),  -- 批3/c1: canonical='space'，'workspace'=迁移期/回滚兼容
    scope_id          TEXT NOT NULL,
    workspace_id      TEXT NOT NULL DEFAULT '',
    team_id            TEXT NOT NULL DEFAULT '',
    uploaded_by       TEXT NOT NULL,
    uploaded_at       TEXT NOT NULL,
    parse_status      TEXT NOT NULL DEFAULT 'pending'
                       {_PARSE_STATUS_CHECK_SQL},
    parse_progress_pct INTEGER,
    parse_layer       TEXT,
    parse_error       TEXT,
    parse_duration_ms INTEGER,
    chunks_count      INTEGER NOT NULL DEFAULT 0,
    cite_count        INTEGER NOT NULL DEFAULT 0,
    last_cited_at     TEXT,
    use_llm_marker    INTEGER NOT NULL DEFAULT 0,
    doc_type_id       TEXT,
    -- Sprint 2 kb-structured-tables: classifier writes this (NOT doc_type_id).
    -- Rollback-safe: 0.40.5 does not read this column.
    auto_classified_doc_type_id TEXT,
    -- Sprint 25 kb-per-row-extract: cardinality=many per-row extraction state.
    -- enum: na/queued/running/succeeded/failed/cancelled (Python-validated,
    -- ALTER ADD COLUMN can't enforce CHECK per doc_type_migration:47-50 note).
    -- Lead chat prompt dispatch reads this field (Sprint 25 §9.2).
    extracted_rows_status TEXT NOT NULL DEFAULT 'na',
    summary_json      TEXT,
    version           INTEGER NOT NULL DEFAULT 1,
    soft_deleted_at   TEXT
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_kb_files_scope
    ON kb_files(scope, scope_id) WHERE soft_deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_kb_files_workspace
    ON kb_files(workspace_id) WHERE soft_deleted_at IS NULL AND workspace_id != '';
CREATE INDEX IF NOT EXISTS idx_kb_files_team
    ON kb_files(team_id) WHERE soft_deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_kb_files_uploaded_by
    ON kb_files(uploaded_by);
CREATE INDEX IF NOT EXISTS idx_kb_files_parse_status
    ON kb_files(parse_status) WHERE soft_deleted_at IS NULL;
"""

# Sprint file-upload-presign Phase 1.3: index list usable inside a transaction.
# executescript implicitly COMMITs before running, ending any open BEGIN — so
# the copy-migrate must issue indexes one-by-one via db.execute() instead.
_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_kb_files_scope ON kb_files(scope, scope_id) WHERE soft_deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_kb_files_workspace ON kb_files(workspace_id) WHERE soft_deleted_at IS NULL AND workspace_id != ''",
    "CREATE INDEX IF NOT EXISTS idx_kb_files_team ON kb_files(team_id) WHERE soft_deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_kb_files_uploaded_by ON kb_files(uploaded_by)",
    "CREATE INDEX IF NOT EXISTS idx_kb_files_parse_status ON kb_files(parse_status) WHERE soft_deleted_at IS NULL",
)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _in_clause(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _summary_dict(summary_json: str | None) -> dict[str, Any]:
    if not summary_json:
        return {}
    try:
        data = json.loads(summary_json)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def truncated_rows_from_summary(summary_json: str | None) -> KbTruncatedRows | None:
    raw = _summary_dict(summary_json).get("truncated_rows")
    if not isinstance(raw, dict):
        return None
    try:
        return KbTruncatedRows.model_validate(raw)
    except ValueError:
        return None


def summary_json_with_truncation(summary_json: str | None, truncated_rows: dict[str, int] | None) -> str | None:
    """在既有 summary_json 上写入/清除 ``truncated_rows``（重新解析后不再截断要清掉旧值）。"""
    data = _summary_dict(summary_json)
    if truncated_rows:
        data["truncated_rows"] = {"indexed": int(truncated_rows["indexed"]), "total": int(truncated_rows["total"])}
    else:
        data.pop("truncated_rows", None)
    return json.dumps(data, ensure_ascii=False) if data else None


def _row_to_dto(row: aiosqlite.Row) -> KbFileDTO:
    # Sprint 2 (I-A5): defensive col-missing read so NEW image reading pre-ALTER
    # rows doesn't crash. aiosqlite.Row supports `.keys()`.
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    auto_classified = row["auto_classified_doc_type_id"] if "auto_classified_doc_type_id" in keys else None
    # Sprint 25: defensive read for pre-ALTER rows
    extracted_rows_status = (row["extracted_rows_status"] if "extracted_rows_status" in keys else "na") or "na"
    heartbeat_at = row["parse_heartbeat_at"] if "parse_heartbeat_at" in keys else None
    return KbFileDTO(
        file_id=row["file_id"],
        artifact_id=row["artifact_id"],
        filename=row["filename"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        scope=row["scope"],
        scope_id=row["scope_id"],
        workspace_id=row["workspace_id"] or "",
        team_id=row["team_id"] or "",
        uploaded_by=row["uploaded_by"],
        uploaded_at=row["uploaded_at"],
        parse_status=row["parse_status"],
        parse_progress_pct=row["parse_progress_pct"],
        parse_layer=row["parse_layer"],
        parse_error=row["parse_error"],
        parse_duration_ms=row["parse_duration_ms"],
        parse_error_code=parse_error_code(row["parse_status"], row["parse_error"]),
        parse_stuck=is_parse_stuck(row["parse_status"], heartbeat_at),
        chunks_count=row["chunks_count"] or 0,
        cite_count=row["cite_count"] or 0,
        last_cited_at=row["last_cited_at"],
        use_llm_marker=bool(row["use_llm_marker"]),
        doc_type_id=row["doc_type_id"],
        auto_classified_doc_type_id=auto_classified,
        extracted_rows_status=extracted_rows_status,
        summary_json=row["summary_json"],
        truncated_rows=truncated_rows_from_summary(row["summary_json"]),
        version=row["version"] or 1,
        active_generation_id=row["active_generation_id"] if "active_generation_id" in keys else None,
        source_observation_json=row["source_observation_json"] if "source_observation_json" in keys else None,
        source_binding_json=row["source_binding_json"] if "source_binding_json" in keys else None,
        source_absent=bool(row["source_absent"]) if "source_absent" in keys else False,
        parse_attempts=row["parse_attempts"] if "parse_attempts" in keys else 0,
        soft_deleted_at=row["soft_deleted_at"],
        # Routes fill can_delete/can_change_scope from caller permissions; DTO defaults False.
        can_delete=False,
        can_change_scope=False,
    )


class KbFilesStore:
    """Async aiosqlite-backed kb_files store. 跟 MemoryNotesStore 同模式."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def initialize(self) -> None:
        """两步: TABLE → INDEX. ALTER 用于 legacy DB upgrade (V1 不需, V2 加列时用).

        Sprint file-upload-presign Phase 1.3: also runs _migrate_parse_status_check
        for legacy DBs whose CHECK constraints predate current parse states or
        the canonical ``space``/``team`` scopes.

        timeout=30.0 (review P0-CON-1): the copy-migrate's BEGIN IMMEDIATE may
        contend with concurrent `scripts/kb-reembed.py` writers; default 5s
        could time out under operational overlap. 30s is conservative.
        """
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path, timeout=30.0) as db:
            await db.executescript(_TABLE_SCHEMA)
            await db.executescript(_INDEX_SCHEMA)
            await self._migrate_parse_status_check(db)
            await self._migrate_extracted_rows_status(db)
            await db.commit()

    async def _migrate_extracted_rows_status(self, db: aiosqlite.Connection) -> None:
        """Sprint 25 kb-per-row-extract: ADD COLUMN extracted_rows_status to legacy DBs.

        Simple ADD COLUMN (no CHECK constraint) — sqlite limits ALTER ADD COLUMN
        to "no CHECK" (per doc_type_migration:47-50 note). Python layer
        validates enum values via KbFileDTO Literal type. Idempotent via
        PRAGMA table_info sniff.
        """
        async with db.execute("SELECT name FROM pragma_table_info('kb_files')") as cur:
            cols = {row[0] for row in await cur.fetchall()}
        if "extracted_rows_status" not in cols:
            await db.execute("ALTER TABLE kb_files ADD COLUMN extracted_rows_status TEXT NOT NULL DEFAULT 'na'")

    # Sprint file-upload-presign Phase 1.3 (review P0-MIG-2): explicit column
    # list keeps the copy-migrate immune to future _TABLE_SCHEMA column-order
    # drift. Whenever _TABLE_SCHEMA changes, update this list too.
    # Columns added after the original migration are intentionally not in this
    # base list: _migrate_parse_status_check PRAGMA-sniffs them and appends only
    # those present in the source table. This keeps old schemas readable while
    # preserving non-default values in partially upgraded schemas.
    _COPY_MIGRATE_COLS = (
        "file_id",
        "artifact_id",
        "filename",
        "mime_type",
        "size_bytes",
        "scope",
        "scope_id",
        "workspace_id",
        "team_id",
        "uploaded_by",
        "uploaded_at",
        "parse_status",
        "parse_progress_pct",
        "parse_layer",
        "parse_error",
        "parse_duration_ms",
        "chunks_count",
        "cite_count",
        "last_cited_at",
        "use_llm_marker",
        "doc_type_id",
        "summary_json",
        "version",
        "soft_deleted_at",
    )

    async def _migrate_parse_status_check(self, db: aiosqlite.Connection) -> None:
        """Expand parse-status and scope CHECK constraints via copy-migrate.

        SQLite cannot ALTER a CHECK constraint in place. We:
        1. Detect via sqlite_schema whether the existing CHECK already lists
           all current parse states and the canonical ``space``/``team``
           scopes — if so, no-op (idempotent).
        2. Otherwise: BEGIN IMMEDIATE transaction (review P0-MIG-1) wrapping
           DROP-orphan + CREATE-new + INSERT-SELECT + DROP-old + RENAME +
           index-recreate; COMMIT on success or ROLLBACK on any error → original
           kb_files survives intact + no orphan kb_files_new left around.
        3. Canonicalize legacy ``scope='org'`` rows to ``team`` while retaining
           optional columns that already exist in the source table.
        4. PRAGMA foreign_keys=OFF/ON brackets the txn per SQLite docs §7,
           kept for forward-compat even though kb_files has no FK clauses today
           (review P1-CON-2 documented).

        Fresh DBs created via _TABLE_SCHEMA already have the expanded CHECK
        and skip the copy-migrate (idempotent guard).
        """
        cur = await db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='kb_files'",
        )
        row = await cur.fetchone()
        if row is None:
            return  # table missing — _TABLE_SCHEMA above will create
        existing_sql = row[0] or ""
        # Substring guard (review P1-MIG-3): both literals appear in the CHECK
        # clause only (CREATE TABLE SQL has no comments here), so substring
        # match is reliable. If future migrations reuse these tokens elsewhere
        # in the schema, switch to AST-based introspection.
        if (
            "'awaiting_upload'" in existing_sql
            and "'queued'" in existing_sql
            # Batch3/c1: scope CHECK must also accept both (missing space means old CHECK rejects
            # canonical writes); reuse copy-migrate whose CREATE has new CHECK.
            and "'space'" in existing_sql
            # 0.87.3 production incident: the mechanical org→team rename changed
            # writers but not an already-expanded CHECK, so all team writes 500.
            and "'team'" in existing_sql
            and "'org'" not in existing_sql
        ):
            return  # already migrated

        cur = await db.execute("SELECT name FROM pragma_table_info('kb_files')")
        source_cols = {row[0] for row in await cur.fetchall()}
        optional_copy_cols = [name for name in ("auto_classified_doc_type_id", "extracted_rows_status") if name in source_cols]
        copy_cols = [*self._COPY_MIGRATE_COLS, *optional_copy_cols]

        # SQLite §7 idiomatic copy-migrate. The FK toggle here is forward-compat
        # cargo (kb_files has no FK columns today) but matches the documented
        # idiom so a future artifact_id FK doesn't require revisiting.
        await db.execute("PRAGMA foreign_keys=OFF")
        try:
            await db.execute("BEGIN IMMEDIATE")
            # Defensive: clear any orphan kb_files_new from a previously-crashed
            # half-migrate run (review P0-MIG-1).
            await db.execute("DROP TABLE IF EXISTS kb_files_new")
            await db.execute(f"""
                CREATE TABLE kb_files_new (
                    file_id           TEXT PRIMARY KEY,
                    artifact_id       TEXT NOT NULL,
                    filename          TEXT NOT NULL,
                    mime_type         TEXT NOT NULL,
                    size_bytes        INTEGER NOT NULL,
                    scope             TEXT NOT NULL CHECK (scope IN ('personal', 'space', 'workspace', 'team')),  -- 批3/c1: canonical='space'，'workspace'=迁移期/回滚兼容
                    scope_id          TEXT NOT NULL,
                    workspace_id      TEXT NOT NULL DEFAULT '',
                    team_id            TEXT NOT NULL DEFAULT '',
                    uploaded_by       TEXT NOT NULL,
                    uploaded_at       TEXT NOT NULL,
                    parse_status      TEXT NOT NULL DEFAULT 'pending'
                                       {_PARSE_STATUS_CHECK_SQL},
                    parse_progress_pct INTEGER,
                    parse_layer       TEXT,
                    parse_error       TEXT,
                    parse_duration_ms INTEGER,
                    chunks_count      INTEGER NOT NULL DEFAULT 0,
                    cite_count        INTEGER NOT NULL DEFAULT 0,
                    last_cited_at     TEXT,
                    use_llm_marker    INTEGER NOT NULL DEFAULT 0,
                    doc_type_id       TEXT,
                    -- Sprint 2 NEW col (default NULL — legacy rows have no
                    -- classifier output; Sprint 2 worker writes here, not into
                    -- doc_type_id, so rollback to 0.40.5 is clean).
                    auto_classified_doc_type_id TEXT,
                    extracted_rows_status TEXT NOT NULL DEFAULT 'na',
                    summary_json      TEXT,
                    version           INTEGER NOT NULL DEFAULT 1,
                    soft_deleted_at   TEXT
                )
            """)
            # Explicit column list — column-order-independent (review P0-MIG-2)
            col_list = ", ".join(copy_cols)
            select_list = ", ".join("CASE WHEN scope = 'org' THEN 'team' ELSE scope END" if col == "scope" else col for col in copy_cols)
            await db.execute(
                f"INSERT INTO kb_files_new ({col_list}) SELECT {select_list} FROM kb_files",
            )
            await db.execute("DROP TABLE kb_files")
            await db.execute("ALTER TABLE kb_files_new RENAME TO kb_files")
            # Recreate indexes (DROP TABLE removed them). Use individual
            # execute() calls — executescript implicitly COMMITs first, which
            # would prematurely end our explicit transaction.
            for stmt in _INDEX_STATEMENTS:
                await db.execute(stmt)
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            raise
        finally:
            await db.execute("PRAGMA foreign_keys=ON")

    # ─── CRUD ─────────────────────────────────────────────

    async def create(
        self,
        *,
        artifact_id: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        scope: str,
        scope_id: str,
        workspace_id: str,
        team_id: str,
        uploaded_by: str,
        use_llm_marker: bool = False,
        doc_type_id: str | None = None,
        initial_parse_status: str = "pending",
    ) -> str:
        """Create new kb_files row, return file_id.

        kb-knowledge-tables v1.0: doc_type_id optional. If set, worker runs
        StructuredExtractor after parse and saves a KbExtractedRecord.

        Sprint file-upload-presign Phase 1.4b (review P0 #3): callers may
        pass ``initial_parse_status='awaiting_upload'`` so the row is created
        atomically in that state — closes the window where a separate UPDATE
        could be lost mid-process.
        """
        if initial_parse_status not in _PARSE_STATUS_VALUES:
            raise ValueError(
                f"initial_parse_status={initial_parse_status!r} not in {_PARSE_STATUS_VALUES}",
            )
        file_id = f"kbf_{uuid4().hex[:12]}"
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO kb_files
                (file_id, artifact_id, filename, mime_type, size_bytes,
                 scope, scope_id, workspace_id, team_id,
                 uploaded_by, uploaded_at, use_llm_marker, doc_type_id,
                 parse_status, parse_heartbeat_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    file_id,
                    artifact_id,
                    filename,
                    mime_type,
                    size_bytes,
                    scope,
                    scope_id,
                    workspace_id,
                    team_id,
                    uploaded_by,
                    now,
                    1 if use_llm_marker else 0,
                    doc_type_id,
                    initial_parse_status,
                    now,
                ),
            )
            await db.commit()
        return file_id

    async def get(self, file_id: str) -> KbFileDTO | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_files WHERE file_id = ?",
                (file_id,),
            ) as cur:
                row = await cur.fetchone()
        return _row_to_dto(row) if row else None

    async def find_active_by_artifact(self, artifact_id: str) -> str | None:
        """给 artifact_id 找活跃的 kb file_id（晋升幂等用）。"""
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT file_id FROM kb_files WHERE artifact_id = ? AND soft_deleted_at IS NULL LIMIT 1",
                (artifact_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row[0] if row else None

    async def artifact_ids_in_kb(self, artifact_ids: list[str]) -> set[str]:
        """file-management-v2 P1 (PRD §4.2.3): 批量判定哪些 artifact 有活跃 KB overlay。

        `/files` 列表用它给行打「知识库」徽章、并把删除入口收敛成跳转（D2）。
        **不能与 artifacts 表 JOIN** —— kb_files 在独立的 `kb_files.db`
        （main.py 装配），所以走单页一次批查（列表页大小 ≤200），不做 N+1。

        「活跃」= `soft_deleted_at IS NULL`：已从知识库移除的文件不该再被标记，
        也不该被删除防护拦下。
        """
        ids = [i for i in artifact_ids if i]
        if not ids:
            return set()
        ph = ",".join("?" for _ in ids)
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                f"SELECT DISTINCT artifact_id FROM kb_files "  # noqa: S608 - placeholders only
                f"WHERE artifact_id IN ({ph}) AND soft_deleted_at IS NULL",
                ids,
            ) as cur,
        ):
            rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def active_for_artifacts(self, artifact_ids: list[str]) -> list[KbFileDTO]:
        """知识库体验整治 FR7：一页 artifact 的全部活跃 KB 行，一次查询。

        `/files` 列表据此同时判定 `in_kb` 与每行的知识库状态。只取行、不判可见性——
        调用方必须按 KB 的读权限过滤后才能把状态给到用户。
        """
        ids = [i for i in artifact_ids if i]
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM kb_files WHERE artifact_id IN ({ph}) "  # noqa: S608 - placeholders only
                "AND soft_deleted_at IS NULL ORDER BY uploaded_at DESC",
                ids,
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def list_for_ws(
        self,
        ws_id: str,
        *,
        limit: int = 50,
        sort: str = "uploaded_at desc",
    ) -> list[KbFileDTO]:
        """List ws scope + personal scope (uploader 自己) files in ws."""
        # P0-O3: exclude soft-deleted via soft_deleted_at IS NULL.
        # Whitelisted sort prevents SQL injection.
        ALLOWED_SORTS = {
            "uploaded_at desc": "uploaded_at DESC",
            "uploaded_at asc": "uploaded_at ASC",
            "cite_count desc": "cite_count DESC",
            "filename asc": "filename ASC",
        }
        order_by = ALLOWED_SORTS.get(sort, "uploaded_at DESC")
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM kb_files WHERE workspace_id = ? AND soft_deleted_at IS NULL ORDER BY {order_by} LIMIT ?",
                (ws_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def list_for_ws_shared(
        self,
        ws_id: str,
        *,
        limit: int = 50,
        sort: str = "uploaded_at desc",
    ) -> list[KbFileDTO]:
        """[V7 kb-agentic-fix §2.8] List ws-shared files only (workspace + team scope).

        Personal files are excluded from this shared-Space listing.
        """
        ALLOWED_SORTS = {
            "uploaded_at desc": "uploaded_at DESC",
            "uploaded_at asc": "uploaded_at ASC",
            "cite_count desc": "cite_count DESC",
            "filename asc": "filename ASC",
        }
        order_by = ALLOWED_SORTS.get(sort, "uploaded_at DESC")
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM kb_files WHERE workspace_id = ? "
                f"AND scope IN ('space', 'workspace', 'team') "
                f"AND soft_deleted_at IS NULL ORDER BY {order_by} LIMIT ?",
                (ws_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def count_for_ws_shared(self, ws_id: str) -> int:
        """[V7 kb-agentic-fix §2.8] count workspace + team scope only."""
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM kb_files WHERE workspace_id = ? AND scope IN ('space', 'workspace', 'team') AND soft_deleted_at IS NULL",
                (ws_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row[0] if row else 0

    async def role_distribution_shared(self, ws_id: str) -> dict[str, int]:
        """[V7 kb-agentic-fix §2.8] V1 占位 (workspace + team scope only).

        V8 kb-doctype-config 实施后走 doc_type_id JOIN.
        """
        return {}

    async def list_for_user(self, user_id: str) -> list[KbFileDTO]:
        """List user 自己 personal scope files."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_files WHERE scope = 'personal' AND scope_id = ? AND soft_deleted_at IS NULL ORDER BY uploaded_at DESC",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def list_distinct_workspaces_for_user(self, user_id: str) -> list[str]:
        """0.40.4 Bug 8 step a: list distinct workspace_ids where the user has
        personal-scope chunks. Caller (kb_search tool) passes this to the
        retriever so personal chunks remain searchable after the user's
        session workspace switches (datarc ws ≠ Bowhead KB scope).

        Includes empty-string ws (legacy upload path) — those chunks live in
        `kb_ws_{sanitize_id('')}` (== `kb_ws_000` per 0.40.3 Bug 7 fix) and
        must be reachable too. Skips soft-deleted rows.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT COALESCE(workspace_id, '') AS workspace_id "
                "FROM kb_files "
                "WHERE scope = 'personal' AND scope_id = ? "
                "AND soft_deleted_at IS NULL",
                (user_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [r["workspace_id"] for r in rows]

    async def list_distinct_workspaces(self) -> list[str]:
        """Sprint 2 kb-structured-tables: ALL distinct workspace_id values
        (across all scopes, incl. legacy '').

        Used by lifespan catalog-seed backfill (per PRD r2 Phase A — fire-and-forget
        background task iterates all ws to seed catalog idempotently).
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT COALESCE(workspace_id, '') AS workspace_id FROM kb_files WHERE soft_deleted_at IS NULL",
            ) as cur:
                rows = await cur.fetchall()
        return [r["workspace_id"] for r in rows]

    async def list_for_team(self, team_id: str) -> list[KbFileDTO]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_files WHERE scope = 'team' AND scope_id = ? AND soft_deleted_at IS NULL ORDER BY uploaded_at DESC",
                (team_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    # Whitelist for list_for_scope_filtered ORDER BY (column-name injection guard).
    _FILTER_ORDER_COLS = {"uploaded_at", "filename", "size_bytes"}

    async def list_for_scope_filtered(
        self,
        scope: str,
        scope_id: str,
        *,
        statuses: list[str] | None = None,
        mime_prefixes: list[str] | None = None,
        doc_type_id: str | None = None,
        uploaded_after: str | None = None,
        uploaded_before: str | None = None,
        q: str | None = None,
        order_by: str = "uploaded_at",
        order_dir: str = "desc",
    ) -> list[KbFileDTO]:
        """[kb-management REQ-1.1/1.2] Scope-listing with SQL-side metadata
        filters + deterministic ordering, for the paginated /kb/files endpoint.

        Returns ALL matching active rows (no LIMIT/OFFSET): the route applies the
        Python-side resource-grant filter (`_filter_kb_files_by_file_grants`,
        grants live in a different DB → no cross-DB JOIN) and only then slices
        offset/limit, so SQL-level pagination would yield a wrong page/count.

        Each management-page tab maps to exactly one scope: personal keys on
        (scope='personal', scope_id=user), team on (scope='team', scope_id=team),
        and workspace on (scope='workspace', workspace_id=ws). Personal uploads
        carry the current workspace_id for chromadb retrieval, so the workspace
        branch MUST also assert scope='workspace'; keying on workspace_id alone
        leaked personal-in-ws files into the workspace tab (dingsiyan 2026-07-07).
        Unlike list_for_ws this has no implicit 50-row cap.

        ORDER always appends ``file_id`` as a tiebreaker: ``uploaded_at`` is a
        TEXT ISO string and a bundle ingest writes many rows in the same second,
        so without it offset paging would drift/skip/duplicate rows.
        """
        if scope == "personal":
            where = ["scope = 'personal'", "scope_id = ?"]
            params: list[Any] = [scope_id]
        elif scope in ("space", "workspace"):  # Batch3/c1 dual read.
            # Isolation (dingsiyan feedback 2026-07-07): the space tab must show
            # space-scope files ONLY. Personal uploads carry the current
            # workspace_id (needed for chromadb retrieval, see routes/kb.py
            # upload_file), so keying on workspace_id alone leaked every user's
            # personal-in-ws docs into the space tab. Mirror the personal/team
            # branches — each tab maps to exactly one scope.
            # Batch3/c1 dual scopes: canonical space + legacy workspace during migration.
            where = ["scope IN ('space', 'workspace')", "workspace_id = ?"]
            params = [scope_id]
        else:  # team
            where = ["scope = 'team'", "scope_id = ?"]
            params = [scope_id]
        return await self._list_filtered(
            where,
            params,
            statuses=statuses,
            mime_prefixes=mime_prefixes,
            doc_type_id=doc_type_id,
            uploaded_after=uploaded_after,
            uploaded_before=uploaded_before,
            q=q,
            order_by=order_by,
            order_dir=order_dir,
        )

    async def list_for_scopes_filtered(
        self,
        *,
        team_id: str,
        personal_owner_id: str | None,
        space_id: str | None,
        team_scope_id: str | None,
        statuses: list[str] | None = None,
        mime_prefixes: list[str] | None = None,
        doc_type_id: str | None = None,
        uploaded_after: str | None = None,
        uploaded_before: str | None = None,
        q: str | None = None,
        order_by: str = "uploaded_at",
        order_dir: str = "desc",
    ) -> list[KbFileDTO]:
        """Cross-scope listing: the union of the single-scope branches of
        :meth:`list_for_scope_filtered` in one query.

        Each non-None argument enables exactly that scope's predicate (the same
        predicate the single-scope listing uses), so the result is the union of
        the single-scope results with one global order (sort column, then
        ``file_id``). Filters apply to every scope identically. All scope
        arguments None (or an empty ``team_id``) returns nothing.

        ``team_id`` additionally pins every row to the caller's team in SQL; the
        route keeps its own team check as well (defense in depth).
        """
        if not team_id:
            return []
        branches: list[str] = []
        params: list[Any] = []
        if personal_owner_id:
            branches.append("(scope = 'personal' AND scope_id = ?)")
            params.append(personal_owner_id)
        if space_id:
            branches.append("(scope IN ('space', 'workspace') AND workspace_id = ?)")
            params.append(space_id)
        if team_scope_id:
            branches.append("(scope = 'team' AND scope_id = ?)")
            params.append(team_scope_id)
        if not branches:
            return []
        return await self._list_filtered(
            ["team_id = ?", f"({' OR '.join(branches)})"],
            [team_id, *params],
            statuses=statuses,
            mime_prefixes=mime_prefixes,
            doc_type_id=doc_type_id,
            uploaded_after=uploaded_after,
            uploaded_before=uploaded_before,
            q=q,
            order_by=order_by,
            order_dir=order_dir,
        )

    async def _list_filtered(
        self,
        where: list[str],
        params: list[Any],
        *,
        statuses: list[str] | None,
        mime_prefixes: list[str] | None,
        doc_type_id: str | None,
        uploaded_after: str | None,
        uploaded_before: str | None,
        q: str | None,
        order_by: str,
        order_dir: str,
    ) -> list[KbFileDTO]:
        where = [*where, "soft_deleted_at IS NULL"]
        params = list(params)

        if statuses:
            where.append("parse_status IN ({})".format(",".join("?" for _ in statuses)))
            params.extend(statuses)
        if mime_prefixes:
            where.append("({})".format(" OR ".join("mime_type LIKE ?" for _ in mime_prefixes)))
            params.extend(f"{p}%" for p in mime_prefixes)
        if doc_type_id:
            where.append("doc_type_id = ?")
            params.append(doc_type_id)
        if uploaded_after:
            where.append("uploaded_at >= ?")
            params.append(uploaded_after)
        if uploaded_before:
            where.append("uploaded_at <= ?")
            params.append(uploaded_before)
        if q:
            # LIKE substring on filename; escape % _ \ so they're literal.
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("filename LIKE ? ESCAPE '\\'")
            params.append(f"%{esc}%")

        col = order_by if order_by in self._FILTER_ORDER_COLS else "uploaded_at"
        direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
        order_sql = f"{col} {direction}, file_id {direction}"

        sql = f"SELECT * FROM kb_files WHERE {' AND '.join(where)} ORDER BY {order_sql}"
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def list_active_for_team_all_scopes(self, team_id: str) -> list[KbFileDTO]:
        """List all active files belonging to an team across every KB scope.

        Used by team embedding reindex. This intentionally keys by durable
        ``team_id`` rather than scope/scope_id so personal, workspace, and team
        files all rebuild into the target embedding namespace.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_files WHERE team_id = ? AND soft_deleted_at IS NULL ORDER BY uploaded_at DESC",
                (team_id,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def readiness_stats_for_team(self, team_id: str) -> KbReadinessStats:
        """Return bounded content signals without loading filenames or errors."""
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN mime_type LIKE 'image/%' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN mime_type = 'application/pdf' "
                "AND use_llm_marker = 1 THEN 1 ELSE 0 END), 0) "
                "FROM kb_files WHERE team_id = ? AND soft_deleted_at IS NULL",
                (team_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return KbReadinessStats(
            active_file_count=int(row[0]) if row else 0,
            image_file_count=int(row[1]) if row else 0,
            scanned_pdf_evidence_count=int(row[2]) if row else 0,
        )

    async def list_all_active(self) -> list[KbFileDTO]:
        """All non-soft-deleted files (audit script 用)."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM kb_files WHERE soft_deleted_at IS NULL",
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def list_soft_deleted_before(self, cutoff: datetime) -> list[KbFileDTO]:
        """P1-N3 90 day grace cron: list soft-deleted files older than cutoff."""
        cutoff_iso = cutoff.isoformat(timespec="seconds")
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='account_retention_holds'") as cur:
                has_account_holds = await cur.fetchone() is not None
            hold_filter = (
                " AND NOT EXISTS(SELECT 1 FROM account_retention_holds h WHERE h.user_id=kb_files.uploaded_by "
                "AND kb_files.scope='personal' AND julianday(h.purge_after)>julianday('now'))"
                if has_account_holds
                else ""
            )
            async with db.execute(
                "SELECT * FROM kb_files WHERE soft_deleted_at IS NOT NULL AND soft_deleted_at < ?" + hold_filter,
                (cutoff_iso,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def update(self, file_id: str, *, parse_owner: tuple[int, int] | None = None, **fields: Any) -> None:
        """Generic update. Whitelist allowed fields to prevent SQL injection."""
        ALLOWED_FIELDS = {
            "parse_status",
            "parse_progress_pct",
            "parse_layer",
            "parse_error",
            "parse_duration_ms",
            "chunks_count",
            "last_cited_at",
            "version",
            "summary_json",
            "doc_type_id",
        }
        unknown = set(fields) - ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"unknown update fields: {unknown}")
        if not fields:
            return
        if "parse_status" in fields:
            # FR1: every status write refreshes heartbeat, authority for stuck checks / startup recovery.
            fields = {**fields, "parse_heartbeat_at": heartbeat_iso()}
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = [*list(fields.values()), file_id]
        guard = ""
        if parse_owner is not None:
            guard = " AND version=? AND parse_attempts=? AND soft_deleted_at IS NULL"
            values.extend(parse_owner)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                f"UPDATE kb_files SET {set_clause} WHERE file_id = ?{guard}",
                values,
            )
            await db.commit()

    async def bump_version(self, file_id: str) -> None:
        """kb-matrix M2 §8⑥: parse 成功后原子递增 version（矩阵文件级 stale 派生输入）。

        幂等递增, 回滚无需清理。唯一 write-through 登记: chunk metadata 在
        _build_kb_chunks 记 file.version, bump 晚于建 chunk → 重解析后 metadata
        version 落后 kb_files 1（今天无消费者, 首个消费者是矩阵 stale 派生且只读
        kb_files.version）。
        """
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE kb_files SET version = version + 1 WHERE file_id = ?",
                (file_id,),
            )
            await db.commit()

    async def update_scope(
        self,
        file_id: str,
        scope: str,
        scope_id: str,
        workspace_id: str,
        team_id: str,
    ) -> None:
        """[P0-P2] scope change 更新 4 个 routing 字段."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE kb_files SET scope = ?, scope_id = ?, workspace_id = ?, team_id = ? WHERE file_id = ?",
                (scope, scope_id, workspace_id, team_id, file_id),
            )
            await db.commit()

    async def update_auto_classified_doc_type(
        self,
        file_id: str,
        doc_type_id: str | None,
        *,
        expected_workspace_id: str,
    ) -> bool:
        """Sprint 2 kb-structured-tables: classifier resolves doc_type → write
        ``auto_classified_doc_type_id`` (NOT ``doc_type_id`` — rollback safe).

        Cross-ws guard (per I-B6): the SQL UPDATE includes
        ``WHERE workspace_id = ?`` so a mismatched ws fails silently (returns
        False), preventing cross-ws FK pollution.

        Returns True if a row was actually updated; False on ws-mismatch or
        missing file.

        ``doc_type_id=None`` is a valid value (used by extract-failure cleanup
        to undo a poisoned auto-classification).
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_files SET auto_classified_doc_type_id = ? WHERE file_id = ? AND workspace_id = ? AND soft_deleted_at IS NULL",
                (doc_type_id, file_id, expected_workspace_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def requeue_for_reparse(self, file_id: str, *, include_done: bool = False, expected_file: KbFileDTO | None = None) -> bool:
        """[kb-management REQ-2.1 + 知识库体验整治 FR1] 用户「重试」的原子领取。

        可重试 = ``failed``，或进行中但心跳早于卡住阈值（``parse_stuck``）。同一条
        UPDATE 同时刷新心跳，所以连点两次时第二次的 WHERE 不再成立 ⇒ 只有一个调用方
        返回 True 并入队。活着的解析最迟在整体时限到点时写终态，心跳不会旧到阈值，
        因此不会被这里抢走。计数清零：用户主动重试不受自动恢复上限约束。
        """
        guard = ""
        params: list[Any] = [heartbeat_iso(), file_id, include_done, stuck_cutoff()]
        if expected_file is not None:
            guard = " AND version=? AND scope=? AND scope_id=? AND workspace_id=? AND team_id=?"
            params.extend([expected_file.version, expected_file.scope, expected_file.scope_id, expected_file.workspace_id, expected_file.team_id])
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                f"""UPDATE kb_files
                SET parse_status='queued', parse_error=NULL, parse_progress_pct=0,
                    parse_attempts=0, parse_heartbeat_at=?, version=version+1
                WHERE file_id=? AND soft_deleted_at IS NULL
                  AND (parse_status='failed' OR (? AND parse_status='done')
                       OR (parse_status IN ({_in_clause(UNFINISHED_PARSE_STATUSES)})
                           AND (parse_heartbeat_at IS NULL OR parse_heartbeat_at < ?))){guard}""",
                params,
            )
            await db.commit()
            return cur.rowcount > 0

    async def mark_parse_failed(self, file_id: str, parse_error: str | None = None) -> None:
        """[kb-management REQ-2.1] Roll a file back to 'failed' (used when the
        worker turns out to be unavailable right after requeue_for_reparse)."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE kb_files SET parse_status='failed', parse_error=?, parse_progress_pct=NULL, parse_heartbeat_at=? WHERE file_id=?",
                (parse_error, heartbeat_iso(), file_id),
            )
            await db.commit()

    async def claim_parse_start(self, file_id: str, *, allow_terminal: bool = False) -> bool:
        """FR1：worker 真正开跑前的原子领取（排队态 → parsing，计一次运行）。

        返回 False 表示已有执行者在跑、已被删除或已是终态，调用方直接放弃 ⇒ 重复入队
        不会产生并行解析。``allow_terminal`` 仅给「对已完成文件强制重解析」的显式路径。
        """
        claimable = ("pending", "queued", "done", "failed") if allow_terminal else ("pending", "queued")
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                f"""UPDATE kb_files
                SET parse_status='parsing', parse_progress_pct=20, parse_error=NULL,
                    parse_attempts=parse_attempts + 1, parse_heartbeat_at=?
                WHERE file_id=? AND soft_deleted_at IS NULL AND parse_status IN ({_in_clause(claimable)})""",
                (heartbeat_iso(), file_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_unfinished_parses(self, *, heartbeat_before: str) -> list[KbFileDTO]:
        """FR1 启动恢复候选：未完成且心跳早于 ``heartbeat_before``（不含 awaiting_upload，
        那归 presign sweeper）。"""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT * FROM kb_files
                WHERE soft_deleted_at IS NULL
                  AND parse_status IN ({_in_clause(UNFINISHED_PARSE_STATUSES)})
                  AND (parse_heartbeat_at IS NULL OR parse_heartbeat_at < ?)
                ORDER BY uploaded_at, file_id""",
                (heartbeat_before,),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dto(r) for r in rows]

    async def requeue_interrupted_parse(
        self,
        file_id: str,
        *,
        max_runs: int,
        heartbeat_before: str,
    ) -> str | None:
        """FR1 启动恢复单个文件：已跑满 ``max_runs`` 次 → failed(interrupted)，否则 → queued。

        两条 UPDATE 都带「未完成 + 心跳早于启动时刻」守卫，本进程已接手的行不会被动到。
        返回 ``"interrupted"`` / ``"requeued"``，行已不符合条件时返回 None。
        """
        guard = f"""file_id=? AND soft_deleted_at IS NULL
                  AND parse_status IN ({_in_clause(UNFINISHED_PARSE_STATUSES)})
                  AND (parse_heartbeat_at IS NULL OR parse_heartbeat_at < ?)"""
        now = heartbeat_iso()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                f"""UPDATE kb_files
                SET parse_status='failed', parse_progress_pct=NULL, parse_heartbeat_at=?,
                    parse_error='interrupted: automatic recovery limit reached'
                WHERE {guard} AND parse_attempts >= ?""",
                (now, file_id, heartbeat_before, max_runs),
            )
            if cur.rowcount > 0:
                await db.commit()
                return "interrupted"
            cur = await db.execute(
                f"""UPDATE kb_files
                SET parse_status='queued', parse_progress_pct=0, parse_error=NULL, parse_heartbeat_at=?
                WHERE {guard} AND parse_attempts < ?""",
                (now, file_id, heartbeat_before, max_runs),
            )
            await db.commit()
            return "requeued" if cur.rowcount > 0 else None

    async def fail_unfinished_parse(self, file_id: str, *, parse_error: str, heartbeat_before: str) -> bool:
        """FR1 启动恢复：团队解析运行时不可用时直接收敛为 failed，留给用户稍后重试。"""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                f"""UPDATE kb_files
                SET parse_status='failed', parse_progress_pct=NULL, parse_error=?, parse_heartbeat_at=?
                WHERE file_id=? AND soft_deleted_at IS NULL
                  AND parse_status IN ({_in_clause(UNFINISHED_PARSE_STATUSES)})
                  AND (parse_heartbeat_at IS NULL OR parse_heartbeat_at < ?)""",
                (parse_error, heartbeat_iso(), file_id, heartbeat_before),
            )
            await db.commit()
            return cur.rowcount > 0

    async def update_doc_type(
        self,
        file_id: str,
        doc_type_id: str | None,
        *,
        expected_workspace_id: str,
    ) -> bool:
        """[kb-management REQ-1.4b] User-facing doc_type binding (the authoritative
        ``doc_type_id`` column, NOT the classifier's ``auto_classified_doc_type_id``).

        ``doc_type_id=None`` unbinds. Cross-ws guard via ``WHERE workspace_id = ?``
        (mirrors update_auto_classified_doc_type). Returns True if a row changed.

        NOTE: this only sets the binding; it does NOT trigger row extraction —
        extraction is the rate-limited per-row subsystem (kb_admin trigger_extract_rows).
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE kb_files SET doc_type_id = ? WHERE file_id = ? AND workspace_id = ? AND soft_deleted_at IS NULL",
                (doc_type_id, file_id, expected_workspace_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def update_extracted_rows_status(
        self,
        file_id: str,
        status: str,
        *,
        expected_workspace_id: str | None = None,
    ) -> bool:
        """Sprint 25 kb-per-row-extract: per-row extraction status transition.

        enum: na/queued/running/succeeded/failed/cancelled (Python-validated).

        Cross-ws guard (P1-TD-ENG-4): if `expected_workspace_id` provided, SQL
        UPDATE includes `WHERE workspace_id = ?` so a mismatched ws fails
        silently (returns False), preventing cross-ws status flip.
        """
        VALID = ("na", "queued", "running", "succeeded", "failed", "cancelled")
        if status not in VALID:
            raise ValueError(f"extracted_rows_status must be one of {VALID}, got {status!r}")
        async with aiosqlite.connect(self._db_path) as db:
            if expected_workspace_id is not None:
                cur = await db.execute(
                    "UPDATE kb_files SET extracted_rows_status = ? WHERE file_id = ? AND workspace_id = ? AND soft_deleted_at IS NULL",
                    (status, file_id, expected_workspace_id),
                )
            else:
                cur = await db.execute(
                    "UPDATE kb_files SET extracted_rows_status = ? WHERE file_id = ? AND soft_deleted_at IS NULL",
                    (status, file_id),
                )
            await db.commit()
            return cur.rowcount > 0

    async def update_chunks_count(self, file_id: str, count: int) -> None:
        """[P1-Q2] scope change cross-coll 迁移后刷 chunks_count."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE kb_files SET chunks_count = ? WHERE file_id = ?",
                (count, file_id),
            )
            await db.commit()

    async def soft_delete(self, file_id: str) -> None:
        """Soft delete: set soft_deleted_at = now. 90 day grace 后 cron hard delete."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE kb_files SET soft_deleted_at = ? WHERE file_id = ?",
                (_now(), file_id),
            )
            await db.commit()

    async def hard_delete(self, file_id: str) -> None:
        """[P1-N3] Real DELETE row. cron 90 day grace 走."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("DELETE FROM kb_files WHERE file_id = ?", (file_id,))
            await db.commit()

    async def count_for_ws(self, ws_id: str) -> int:
        async with (
            aiosqlite.connect(self._db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM kb_files WHERE workspace_id = ? AND soft_deleted_at IS NULL",
                (ws_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        return row[0] if row else 0

    async def role_distribution(self, ws_id: str) -> dict[str, int]:
        """V1 占位 (V8 加 doc_type 后 role 走 doc_type 表). V1 返 empty dict."""
        # TODO: V8 kb-doctype-config uses doc_type_id JOIN once implemented.
        return {}

    async def increment_cite_count(self, file_id: str) -> None:
        """Cite popover 触发时刷新 cite_count + last_cited_at."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE kb_files SET cite_count = cite_count + 1, last_cited_at = ? WHERE file_id = ?",
                (_now(), file_id),
            )
            await db.commit()
