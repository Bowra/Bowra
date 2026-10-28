"""让「一条记忆」成为可寻址实体：`memory_entries` 加 6 个 per-条元数据列 + 身份键带上作用域。

上游设计：`docs/sprints/memory-spike-2026-08-29/tech-design.md` §3.3 / §4.1（批0 地基）。

**为什么要动 schema**：今天真记忆是 `memory_notes` 里的一段 markdown（整块一行、无 per-条
元数据），带元数据的 `memory_entries` 只装机器状态。于是取代做不了（blob 里没有可寻址的
条目 ⇒ 只能追加）、遗忘做不了（没有 per-条时间戳）、追溯做不了（`source_turn_id` 是**行级**
的，一行 6 条 bullet 只有一个 turn id）。FR3 取代 + FR7 遗忘 + FR9 置信度三条一起要求
per-条的 `memory_type / confidence / last_confirmed_at / superseded_by`，blob 装不下。

本 revision 做两件事，**都不碰内容**：

1. **加 6 列**（`ALTER TABLE ADD COLUMN`，先 `PRAGMA table_info` 探测 ⇒ 幂等）：
   `memory_type` / `confidence` / `last_confirmed_at` / `superseded_by` /
   `previous_value` + `previous_value_at`（后两个同进同出、算一件事，只留**上一版**）。
   ★ `expires_at` / `created_source_session_id` **不加**——r3 砍掉 `episodic` 之后没有
   需要过期的类；真出现需求再加列，加列是整套 schema 里最软的一步。

2. **身份键带上作用域**（copy-migrate）：
   `UNIQUE(subject_type, subject_id, kind, key)`
   → `UNIQUE(subject_type, subject_id, kind, key, COALESCE(workspace_id,'*'))`。
   今天空间 A 记了 `top_n=10`、空间 B 想记 `top_n=5`，第二次写直接
   `MemoryScopeConflictError`（`user_memory.upsert` 与
   `personal_memory_governance._upsert_structured_candidate` 各显式抛一处）。
   SQLite 不能原地改约束，按 `docs/design/数据库迁移.md` 走
   **新表 → 校验复制 → 建索引 → 原子改名**。116 该表 93 行，一次事务内完成。

   ⚠ **表级 UNIQUE 装不下表达式**，所以新表不再带内联 UNIQUE，作用域身份由
   `idx_memory_entries_identity_scoped` 这个**表达式唯一索引**承载。连带后果：
   `ON CONFLICT(subject_type, subject_id, kind, key)` 这种四列冲突目标**不再匹配任何
   唯一约束、会直接报错**（实测 SQLite 3.51：`ON CONFLICT clause does not match any
   PRIMARY KEY or UNIQUE constraint`）。仓里唯一那处是 `ProjectMemoryStore.upsert`
   （生产 81 行 `datarc_pref` 走它），已同批改成带 `COALESCE(...)` 的冲突目标。
   新键严格细于旧键 ⇒ 存量数据不可能在新索引上撞唯一。

**不做内容回填**：6 条 bullet 拆条、58 条候选处置、`memory_policy_cutover_complete` 补写
都归 §5 那个带 `--dry-run` 的一次性可重入作业（迁移文档明写「大数据回填不隐藏在 revision 中」）。
存量行的 6 个新列一律 NULL，语义是「未标注」。

与 `user_memory.SCHEMA` / `_migrate_*` 双路维护，两边幂等（同 0021 / 0023 先例）：
store 那条路是 `_migrate_memory_item_columns` + `_migrate_scoped_identity`，
结构与本文件一一对应；两条路任意顺序、各跑任意次都必须收敛到同一形状。

⚠ **回滚**：copy-migrate 之后回退镜像会遇到不认识的列与新唯一索引，退路只有库快照
（同 0023 教训：带迁移回滚会 crash-loop）。发版前必须先存快照。

Revision ID: 0024_memory_item_metadata
Revises: 0023_canvas_content_status
Create Date: 2026-08-29
"""

from __future__ import annotations

from alembic import context

revision = "0024_memory_item_metadata"
down_revision = "0023_canvas_content_status"
branch_labels = None
depends_on = None


#: 批0 新增的 per-条元数据列（tech-design §3.3）。
_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("memory_type", "TEXT"),  # procedural | semantic；NULL=未分类（存量）
    ("confidence", "TEXT"),  # high(显式) | low(推断)；NULL=未知
    ("last_confirmed_at", "TEXT"),
    ("superseded_by", "TEXT"),  # 跨 key 取代时指向新条 id
    ("previous_value", "TEXT"),  # 同 key 覆盖时留上一版（只留一版）
    ("previous_value_at", "TEXT"),
)

#: 0024 之后 `memory_entries` 的**完整**列序。revision 是不可变的，所以这份清单在本文件
#: 里冻结一份副本，不从 `user_memory` import（import 会让历史 revision 随源码漂移）。
_CANONICAL_COLUMNS: tuple[str, ...] = (
    "id",
    "subject_type",
    "subject_id",
    "kind",
    "key",
    "value",
    "locked",
    "lock_reason",
    "source",
    "created_at",
    "updated_at",
    "deleted_at",
    "workspace_id",
    "version",
    "memory_role",
    "value_schema",
    "memory_policy_version",
    "created_source_type",
    "created_source_turn_id",
    "created_source_candidate_id",
    "created_source_workspace_id",
    "last_modified_actor_type",
    "last_modified_actor_id",
    "last_modified_at",
    *(name for name, _ in _NEW_COLUMNS),
)

_NEW_TABLE_DDL = """
CREATE TABLE memory_entries_0024_new (
    id              TEXT PRIMARY KEY,
    subject_type    TEXT NOT NULL,
    subject_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    locked          INTEGER NOT NULL DEFAULT 0,
    lock_reason     TEXT,
    source          TEXT NOT NULL DEFAULT 'user_explicit',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    deleted_at      TEXT,
    workspace_id    TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    memory_role     TEXT NOT NULL DEFAULT 'unclassified',
    value_schema    TEXT,
    memory_policy_version INTEGER NOT NULL DEFAULT 0,
    created_source_type TEXT,
    created_source_turn_id TEXT,
    created_source_candidate_id TEXT,
    created_source_workspace_id TEXT,
    last_modified_actor_type TEXT,
    last_modified_actor_id TEXT,
    last_modified_at TEXT,
    memory_type     TEXT,
    confidence      TEXT,
    last_confirmed_at TEXT,
    superseded_by   TEXT,
    previous_value  TEXT,
    previous_value_at TEXT
)
"""

#: 旧表被 DROP 时它的索引一起没了，改名后必须逐条重建。
#: 前 5 条与 `user_memory._INDEX_SCHEMA` 逐字对应；最后一条是本 revision 新增的作用域身份键。
_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_memory_subject_kind ON memory_entries(subject_type, subject_id, kind)",
    "CREATE INDEX IF NOT EXISTS idx_memory_deleted_at ON memory_entries(subject_type, subject_id, deleted_at)",
    "CREATE INDEX IF NOT EXISTS idx_memory_ws ON memory_entries(subject_type, subject_id, workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_user_updated ON memory_entries(subject_type, subject_id, deleted_at, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memory_user_role ON memory_entries(subject_type, subject_id, memory_role, kind, deleted_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_entries_identity_scoped "
    "ON memory_entries(subject_type, subject_id, kind, key, COALESCE(workspace_id, '*'))",
)

_LEGACY_IDENTITY = ["subject_type", "subject_id", "kind", "key"]


def _table_columns(connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()]


def _has_legacy_identity_unique(connection) -> bool:
    """旧形状 = 存在一个由表级 `UNIQUE(...)` 生成的、恰好覆盖那四列的自动索引。

    判据用 `PRAGMA index_list` + `index_info` 而不是正则去 grep 建表 SQL：
    后者对空白/换行敏感，而这段 DDL 在 baseline、store、本文件三处各有一份写法。
    表达式索引在 `index_info` 里那一列的名字是 NULL ⇒ 新形状不会被误判成旧形状。
    """
    for row in connection.exec_driver_sql("PRAGMA index_list(memory_entries)").fetchall():
        name, unique, origin = str(row[1]), int(row[2]), str(row[3])
        if not unique or origin != "u":
            continue
        columns = [row_info[2] for row_info in connection.exec_driver_sql(f'PRAGMA index_info("{name}")').fetchall()]
        if columns == _LEGACY_IDENTITY:
            return True
    return False


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()

    columns = _table_columns(connection, "memory_entries")
    # 空 list = 表还不存在（store 尚未 initialize 的全新库）→ no-op，别在这里 CREATE：
    # 建表的权威是 `user_memory.SCHEMA` 与 baseline，在这里复制第三份就是第三个真源。
    if not columns:
        return

    # ① 加列（幂等）
    for name, ddl_type in _NEW_COLUMNS:
        if name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE memory_entries ADD COLUMN {name} {ddl_type}")

    # ② 身份键带作用域（copy-migrate；旧形状不在就只补索引）
    if _has_legacy_identity_unique(connection):
        present = set(_table_columns(connection, "memory_entries"))
        missing = [name for name in _CANONICAL_COLUMNS if name not in present]
        if missing:
            # 低于 1.0.0 基线的库不该走到这里（升级链固定 旧版本 → 1.0.0 → 后续）。
            # 与其静默丢列，不如让容器停在迁移这一步。
            msg = f"memory_entries missing pre-0024 baseline columns: {missing}"
            raise RuntimeError(msg)
        extra = [name for name in present if name not in _CANONICAL_COLUMNS]
        if extra:
            # copy-migrate 按固定列清单复制；未知列会被无声丢掉 ⇒ 宁可炸在这里。
            msg = f"memory_entries has unexpected columns, refusing copy-migrate: {sorted(extra)}"
            raise RuntimeError(msg)

        before = connection.exec_driver_sql("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
        column_list = ", ".join(_CANONICAL_COLUMNS)
        connection.exec_driver_sql("DROP TABLE IF EXISTS memory_entries_0024_new")
        connection.exec_driver_sql(_NEW_TABLE_DDL)
        connection.exec_driver_sql(
            f"INSERT INTO memory_entries_0024_new ({column_list}) SELECT {column_list} FROM memory_entries",
        )
        after = connection.exec_driver_sql("SELECT COUNT(*) FROM memory_entries_0024_new").fetchone()[0]
        if before != after:
            msg = f"memory_entries copy-migrate row count mismatch: {before} → {after}"
            raise RuntimeError(msg)
        connection.exec_driver_sql("DROP TABLE memory_entries")
        connection.exec_driver_sql("ALTER TABLE memory_entries_0024_new RENAME TO memory_entries")

    # ③ 索引重建（旧表被 DROP 时索引一起没了；没走 copy-migrate 时这里只是补作用域身份键）
    for ddl in _INDEX_DDL:
        connection.exec_driver_sql(ddl)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
