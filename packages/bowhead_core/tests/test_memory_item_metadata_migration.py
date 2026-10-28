"""0024_memory_item_metadata 的最小升级测试（CLAUDE.md：实现/迁移测试/文档同一提交）。

上游：`docs/sprints/memory-spike-2026-08-29/tech-design.md` §3.3 / §4.1（记忆重做 批0）。

要证四件事：

1. **0023 的库 → head**：6 个 per-条元数据列长出来、存量行留 NULL（不做内容回填），
   身份唯一约束从四列变成「四列 + `COALESCE(workspace_id,'*')`」，5 个既有索引全部重建，
   行数与 id 集合逐条一致（copy-migrate 的校验步骤）。
2. **幂等**：迁移重复跑、store 的 `initialize()` 重复跑、两条路任意顺序，都收敛到同一形状。
3. **作用域真进了身份键**：同 (user, kind, key) 在两个空间可以并存；同 (user, kind, key)
   的两条全局记忆仍然撞唯一（`COALESCE` 让 NULL 成为一个**具体**的作用域值，
   而不是「NULL != NULL 所以谁都不撞」——后者会让全局记忆无限重复）。
4. **`ON CONFLICT` 没被这次改动打断**：表级 UNIQUE 换成表达式唯一索引之后，
   四列冲突目标不再匹配任何唯一约束，SQLite 直接报错。生产上走这条路的是
   `ProjectMemoryStore.upsert`（116 有 81 行 `datarc_pref`），必须真调一次才算验过——
   「换约束 = 换接口」，光看列在不在证明不了它还能写。
"""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import upgrade
from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

from bowhead_core import migrations

pytestmark = pytest.mark.unit

_NEW_COLUMNS = (
    "memory_type",
    "confidence",
    "last_confirmed_at",
    "superseded_by",
    "previous_value",
    "previous_value_at",
)
_REBUILT_INDEXES = (
    "idx_memory_subject_kind",
    "idx_memory_deleted_at",
    "idx_memory_ws",
    "idx_memory_user_updated",
    "idx_memory_user_role",
)
_SCOPED_IDENTITY_INDEX = "idx_memory_entries_identity_scoped"


def _chain_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from bowhead_core import migrations

    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"revision 链分叉了，heads={heads}"
    return heads[0]


def _columns(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [row[1] for row in conn.execute("PRAGMA table_info(memory_entries)")]


def _index_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute("PRAGMA index_list(memory_entries)")}


def _has_legacy_identity_unique(db_path: Path) -> bool:
    """旧形状 = 由表级 UNIQUE(...) 生成的、恰好覆盖那四列的自动索引。"""
    with sqlite3.connect(db_path) as conn:
        for row in conn.execute("PRAGMA index_list(memory_entries)"):
            name, unique, origin = row[1], row[2], row[3]
            if not unique or origin != "u":
                continue
            cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")')]
            if cols == ["subject_type", "subject_id", "kind", "key"]:
                return True
    return False


def _upgrade_to_0023(db_path: Path) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    config.attributes["bowhead_target"] = "core"
    # 0024's parent revision (0023); no revision literal (test_migration_head_pins_are_consistent).
    command.upgrade(config, import_module("bowhead_core.migrations.versions.0024_memory_item_metadata").down_revision)


def _seed_pre_0024(db_path: Path) -> None:
    """真实 Alembic 链升到 0023 的库（memory_entries 仍带旧的四列 UNIQUE）+ 存量行。

    曾用「1.0.0 baseline + 把 alembic_version 直接写成 0023」造库，跳过了 0002–0023 的
    真实 DDL；0035 起要 ALTER 由 0009 建的 ``team_builtin_subagent_override``，那样造出的
    库在 0035 就失败。这里按链升到 0023，只在此之后才由被测的 ``upgrade`` 接手。
    """
    _upgrade_to_0023(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = [
            # Genuine global memory
            ("mem_global", "user", "u1", "preference", "kpi_money_unit", '"百万"', None),
            # Genuine space-scoped memory
            ("mem_ws", "user", "u1", "constraint", "profit_metric", '"净利率"', "ws_a"),
            # Operational row (batch 0② policy keeps it out of prompts; this migration leaves it untouched)
            ("mem_ops", "user", "u1", "tool_context", "bi:project", '{"id":"1"}', None),
            # Project scope (subject_type='project'), separate from personal memory
            ("prj_pref", "project", "ws_a", "datarc_pref", "last_used", '"proj_1"', None),
        ]
        for entry_id, subject_type, subject_id, kind, key, value, ws in rows:
            conn.execute(
                "INSERT INTO memory_entries(id, subject_type, subject_id, kind, key, value, locked, "
                "source, created_at, updated_at, workspace_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, 'user_explicit', '2026-01-01', '2026-01-01', ?)",
                (entry_id, subject_type, subject_id, kind, key, value, ws),
            )
        conn.commit()


@pytest.mark.asyncio
async def test_upgrade_adds_columns_and_scopes_the_identity_key(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _seed_pre_0024(db_path)

    assert _has_legacy_identity_unique(db_path), "fixture 没造出旧形状，后面的断言全是空转"
    assert not set(_NEW_COLUMNS) & set(_columns(db_path))
    with sqlite3.connect(db_path) as conn:
        before_ids = [r[0] for r in conn.execute("SELECT id FROM memory_entries ORDER BY id")]

    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    columns = _columns(db_path)
    assert set(_NEW_COLUMNS).issubset(columns), f"新列没长出来: {columns}"
    # Identity key changes shape.
    assert not _has_legacy_identity_unique(db_path), "旧的四列 UNIQUE 还在 —— copy-migrate 没生效"
    assert _SCOPED_IDENTITY_INDEX in _index_names(db_path)
    # Rebuild all 5 existing indexes (DROP removes them with the old table; each omission causes a full table scan).
    assert set(_REBUILT_INDEXES).issubset(_index_names(db_path))

    with sqlite3.connect(db_path) as conn:
        after_ids = [r[0] for r in conn.execute("SELECT id FROM memory_entries ORDER BY id")]
        assert after_ids == before_ids, "copy-migrate 丢/串行了"
        # No content backfill: all 6 new columns are NULL for existing rows (unannotated ≠ annotated as empty).
        row = conn.execute(
            f"SELECT {', '.join(_NEW_COLUMNS)} FROM memory_entries WHERE id='mem_global'",
        ).fetchone()
        assert row == (None,) * len(_NEW_COLUMNS)
        # Content remains byte-for-byte unchanged.
        assert conn.execute("SELECT value FROM memory_entries WHERE id='mem_ws'").fetchone() == ('"净利率"',)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (_chain_head(),)


@pytest.mark.asyncio
async def test_scoped_identity_lets_two_spaces_hold_the_same_key(tmp_path) -> None:
    """FR2 的地基：空间 A 记 top_n=10、空间 B 记 top_n=5 不该互相顶掉。

    反向那半同样重要——两条**全局**的同 key 记忆仍必须撞唯一。
    `COALESCE(workspace_id,'*')` 就是为这半写的：直接把 `workspace_id` 放进索引的话
    NULL != NULL，全局记忆会无限重复。
    """
    db_path = tmp_path / "bowhead.db"
    _seed_pre_0024(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO memory_entries(id, subject_type, subject_id, kind, key, value, locked, source, "
            "created_at, updated_at, workspace_id) VALUES "
            "('mem_ws_b','user','u1','preference','kpi_money_unit','\"元\"',0,'u','2026','2026','ws_b')",
        )
        assert conn.execute("SELECT COUNT(*) FROM memory_entries WHERE key='kpi_money_unit'").fetchone()[0] == 2

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_entries(id, subject_type, subject_id, kind, key, value, locked, source, "
                "created_at, updated_at, workspace_id) VALUES "
                "('mem_dup','user','u1','preference','kpi_money_unit','\"元\"',0,'u','2026','2026',NULL)",
            )


@pytest.mark.asyncio
async def test_project_memory_upsert_still_writes_after_the_identity_change(tmp_path) -> None:
    """★ 换约束 = 换接口。

    表级 UNIQUE 换成表达式唯一索引之后，`ON CONFLICT(subject_type, subject_id, kind, key)`
    不再匹配任何唯一约束，SQLite 直接抛 "ON CONFLICT clause does not match any PRIMARY KEY
    or UNIQUE constraint"。仓里唯一那处是 `ProjectMemoryStore.upsert`，生产 81 行
    `datarc_pref` 走它 —— 只验「列在不在」证明不了它还能写，必须真 upsert 两次。
    """
    db_path = tmp_path / "bowhead.db"
    _seed_pre_0024(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    store = ProjectMemoryStore(db_path)
    await store.initialize()
    first = await store.upsert(workspace_id="ws_a", kind="datarc_pref", key="last_used", value="proj_2")
    second = await store.upsert(workspace_id="ws_a", kind="datarc_pref", key="last_used", value="proj_3")
    assert first.id == second.id, "ON CONFLICT 没命中 → 变成插新行"
    assert second.parsed_value() == "proj_3"


@pytest.mark.asyncio
async def test_migration_and_store_initialize_are_both_idempotent(tmp_path) -> None:
    """双路维护：revision 与 `user_memory.SCHEMA` / `_migrate_*` 任意顺序、各跑任意次。"""
    db_path = tmp_path / "bowhead.db"
    _seed_pre_0024(db_path)

    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    store = UserMemoryStore(db_path)
    await store.initialize()
    await store.initialize()
    project_store = ProjectMemoryStore(db_path)
    await project_store.initialize()

    assert set(_NEW_COLUMNS).issubset(_columns(db_path))
    assert not _has_legacy_identity_unique(db_path)
    assert _SCOPED_IDENTITY_INDEX in _index_names(db_path)
    assert set(_REBUILT_INDEXES).issubset(_index_names(db_path))
    entries = await store.list_for_user("u1")
    assert {e.id for e in entries} == {"mem_global", "mem_ws", "mem_ops"}


@pytest.mark.asyncio
async def test_copy_migrate_recovers_from_a_crashed_midstate(tmp_path) -> None:
    """上次跑到一半就断电（中转表建了、没复制没改名）⇒ 重跑必须收敛，不是撞「表已存在」。

    为什么这个中间态真的可能留下：store 那条路上 `CREATE TABLE` 是 DDL、Python 3.12 的
    sqlite3 不会为它隐式开事务 ⇒ 它先自己提交了；之后的 `INSERT ... SELECT` 才开事务，
    它失败回滚只会撤掉复制，中转表留在库里。恢复靠 copy-migrate 开头那句
    `DROP TABLE IF EXISTS memory_entries_0024_new`（0022 也是这个形态）。
    """
    db_path = tmp_path / "crashed.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            "CREATE TABLE memory_entries ("
            "id TEXT PRIMARY KEY, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
            "locked INTEGER NOT NULL DEFAULT 0, lock_reason TEXT, "
            "source TEXT NOT NULL DEFAULT 'user_explicit', created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, UNIQUE(subject_type, subject_id, kind, key));",
        )
        conn.execute(
            "INSERT INTO memory_entries(id, subject_type, subject_id, kind, key, value, created_at, updated_at) "
            "VALUES ('mem_a','user','u1','preference','k','\"v\"','2020-01-01','2020-01-01')",
        )
        conn.execute("CREATE TABLE memory_entries_0024_new (id TEXT PRIMARY KEY, junk TEXT)")
        conn.commit()

    store = UserMemoryStore(db_path)
    await store.initialize()

    entries = await store.list_for_user("u1")
    assert [e.id for e in entries] == ["mem_a"]
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "memory_entries_0024_new" not in tables, "中转表没清掉，下次升级会撞上它"
    assert _SCOPED_IDENTITY_INDEX in _index_names(db_path)


@pytest.mark.asyncio
async def test_store_first_then_migration_converges_to_the_same_shape(tmp_path) -> None:
    """反向顺序：store 先 `initialize()` 建表，alembic 后跑。

    正向那条（迁移先跑）在上面。两条都要，因为两边探测的是不同分支：
    store 先建 ⇒ 表已经是新形状 ⇒ revision 走「不做 copy-migrate、只补索引」那条路。

    ★ 等价突变留档：**只**摘掉其中一条路的建索引语句，本条不会红——另一条路会把索引补上，
    这正是「双路维护」的定义。两条一起摘才红（已实测）。所以本条钉的是
    「两条路的并集收敛到同一形状」，不是「revision 单独也够」。
    """
    db_path = tmp_path / "bowhead.db"
    _upgrade_to_0023(db_path)
    assert _has_legacy_identity_unique(db_path), "fixture 没造出旧形状，store 那条路就是空转"

    store = UserMemoryStore(db_path)
    await store.initialize()
    assert not _has_legacy_identity_unique(db_path)

    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    assert not _has_legacy_identity_unique(db_path)
    assert _SCOPED_IDENTITY_INDEX in _index_names(db_path)
    assert set(_NEW_COLUMNS).issubset(_columns(db_path))
    assert set(_REBUILT_INDEXES).issubset(_index_names(db_path))


@pytest.mark.asyncio
async def test_store_alone_migrates_a_legacy_db_to_the_same_shape(tmp_path) -> None:
    """没跑过 alembic 的老库（dev / 单测路径）由 store 侧那条路收敛到同一形状。"""
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            "CREATE TABLE memory_entries ("
            "id TEXT PRIMARY KEY, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
            "locked INTEGER NOT NULL DEFAULT 0, lock_reason TEXT, "
            "source TEXT NOT NULL DEFAULT 'user_explicit', created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, UNIQUE(subject_type, subject_id, kind, key));",
        )
        conn.execute(
            "INSERT INTO memory_entries(id, subject_type, subject_id, kind, key, value, created_at, updated_at) "
            "VALUES ('mem_old','user','u1','preference','k','\"v\"','2020-01-01','2020-01-01')",
        )
        conn.commit()
    assert _has_legacy_identity_unique(db_path)

    store = UserMemoryStore(db_path)
    await store.initialize()

    assert not _has_legacy_identity_unique(db_path)
    assert _SCOPED_IDENTITY_INDEX in _index_names(db_path)
    assert set(_NEW_COLUMNS).issubset(_columns(db_path))
    entries = await store.list_for_user("u1")
    assert len(entries) == 1
    assert entries[0].id == "mem_old"
    assert entries[0].memory_type is None
