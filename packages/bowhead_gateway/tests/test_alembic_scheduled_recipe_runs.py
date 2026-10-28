"""0011_scheduled_recipe_runs — recipe_ref/run_params 列 + 在飞唯一索引谓词加宽.

核心陷阱（sprint review 抓到）：`CREATE UNIQUE INDEX IF NOT EXISTS` 对已上线库是静默 no-op，
同名旧谓词会原样留下 —— 所以 revision 必须 DROP 再建。本文件的判据不是「索引存在」，而是
「旧谓词允许的并发行，迁移后被 DB 拒掉」。
"""

from __future__ import annotations

import sqlite3

import anyio
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.scheduled_store import _RUNNING_UNIQUE_INDEX, ScheduledJobStore

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()

_OLD_RUNNING_UNIQUE_INDEX = "CREATE UNIQUE INDEX idx_sjr_one_running ON scheduled_job_runs(job_id) WHERE status = 'running'"


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_sql(connection: sqlite3.Connection, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return None if row is None else str(row[0])


def _insert_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    job_id: str = "sj_legacy",
    status: str = "running",
    started_at: str = "2026-01-01T00:00:00+00:00",
    turn_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO scheduled_job_runs(id,job_id,turn_id,started_at,finished_at,status,error) VALUES (?,?,?,?,NULL,?,NULL)",
        (run_id, job_id, turn_id, started_at, status),
    )


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.execute(
            "INSERT INTO scheduled_jobs(id,user_id,workspace_id,name,cron,query,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "sj_legacy",
                "u_legacy",
                "ws_legacy",
                "Legacy Job",
                "0 9 * * *",
                "summarize",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()


def test_1_0_core_schema_gains_recipe_columns_and_the_widened_in_flight_index(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    assert upgrade(db_url, target="core") == ("core",)
    assert upgrade(db_url, target="core") == ("core",)  # re-entrant

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        assert {"recipe_ref", "run_params"} <= _column_names(connection, "scheduled_jobs")
        running_index = _index_sql(connection, "idx_sjr_one_running")
        assert running_index is not None
        assert "awaiting_input" in running_index
        assert _index_sql(connection, "idx_sjr_turn") is not None

    # Old rows without recipe_ref still read as ScheduledJob (nullable added column, not required).
    job = anyio.run(ScheduledJobStore(str(db_path)).get, "sj_legacy")
    assert job is not None
    assert job.name == "Legacy Job"


def test_an_already_deployed_old_predicate_index_is_replaced_not_kept(tmp_path) -> None:
    """必含用例：同名索引已在场时，IF NOT EXISTS 会静默保留旧谓词——revision 必须 DROP 重建。"""
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    with sqlite3.connect(db_path) as connection:
        # Explicitly reproduce production's same-name index with old predicate, independent of baseline coincidence.
        connection.execute("DROP INDEX IF EXISTS idx_sjr_one_running")
        connection.execute(_OLD_RUNNING_UNIQUE_INDEX)
        _insert_run(connection, "sjr_running", status="running")
        # This row pair is legal under the old predicate but must become illegal after migration.
        _insert_run(connection, "sjr_awaiting", status="awaiting_input", started_at="2026-01-01T00:05:00+00:00")
        connection.commit()
        assert "awaiting_input" not in str(_index_sql(connection, "idx_sjr_one_running"))

    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)

    with sqlite3.connect(db_path) as connection:
        assert "awaiting_input" in str(_index_sql(connection, "idx_sjr_one_running"))
        # Heal retains only the earliest in-flight row; later row becomes failed so the unique index can be created.
        assert connection.execute("SELECT status,error FROM scheduled_job_runs WHERE id='sjr_running'").fetchone() == (
            "running",
            None,
        )
        assert connection.execute("SELECT status,error FROM scheduled_job_runs WHERE id='sjr_awaiting'").fetchone() == (
            "failed",
            "scheduled_run_interrupted",
        )
        # Exercise the gate: the new predicate actually rejects concurrency, beyond changing index text.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(connection, "sjr_second", status="awaiting_input", started_at="2026-01-01T01:00:00+00:00")
        connection.rollback()
        # Other jobs unaffected.
        connection.execute(
            "INSERT INTO scheduled_jobs(id,user_id,workspace_id,name,cron,query,created_at,updated_at) "
            "VALUES ('sj_other','u_legacy','ws_legacy','Other','0 9 * * *','x','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
        )
        _insert_run(connection, "sjr_other", job_id="sj_other", status="awaiting_input")
        connection.commit()


def test_store_boot_ddl_and_the_revision_agree_on_the_predicate(tmp_path) -> None:
    """常量分叉守卫：store 每次 boot 都 CREATE IF NOT EXISTS，新库与迁移库语义必须一致。"""
    migrated = tmp_path / "bowhead.db"
    _create_1_0_fixture(migrated)
    assert upgrade(f"sqlite+aiosqlite:///{migrated}", target="core") == ("core",)

    fresh = tmp_path / "fresh.db"
    anyio.run(ScheduledJobStore(str(fresh)).initialize)

    def _normalized(path) -> str:
        with sqlite3.connect(path) as connection:
            return " ".join(str(_index_sql(connection, "idx_sjr_one_running")).split())

    assert "awaiting_input" in _normalized(migrated)
    assert _normalized(migrated) == _normalized(fresh).replace("IF NOT EXISTS ", "")
    assert "IN ('running','awaiting_input')" in _RUNNING_UNIQUE_INDEX


def test_recipe_ref_write_path_works_on_a_migrated_db(tmp_path) -> None:
    """★ 写路径必须在**迁移后的库**上验。

    ``ScheduledJobStore.SCHEMA`` 是 1.0.0 过渡 DDL，按迁移纪律不得再扩，所以新列只存在于
    revision 里。只用 ``store.initialize()`` 建的库没有 ``recipe_ref`` 列 —— 若写路径只被
    那种库测过，线上第一次绑定就会 `no such column`。
    """
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)

    store = ScheduledJobStore(str(db_path))
    ref = '{"form":"personal_skill","id_or_name":"weekly-report"}'

    async def _exercise() -> tuple[object, object, object]:
        await store.initialize()  # Idempotent; must not overwrite migrated shape.
        assert await store.patch("sj_legacy", recipe_ref=ref) is True
        bound = await store.get("sj_legacy")
        # System pauses persist their reason so FE distinguishes user pauses from system pauses.
        assert await store.patch("sj_legacy", status="paused", pause_reason="scheduled_recipe_lost") is True
        paused = await store.get("sj_legacy")
        # Reactivation clears pause reasons (existing semantics, unaffected by new parameters).
        assert await store.patch("sj_legacy", status="active") is True
        reactivated = await store.get("sj_legacy")
        return bound, paused, reactivated

    bound, paused, reactivated = anyio.run(_exercise)

    assert bound.recipe_ref == ref
    assert paused.pause_reason == "scheduled_recipe_lost"
    assert reactivated.pause_reason is None
    assert reactivated.recipe_ref == ref, "重新启用不该顺手解绑流程"


def test_create_with_binding_on_migrated_db_and_without_on_legacy(tmp_path) -> None:
    """create 的 INSERT 必须是**增量列**：未迁移库建普通任务照常，迁移库才写 recipe_ref。

    `SCHEMA` 常量按迁移纪律不再扩，所以无条件把新列写进 INSERT 会让所有未迁移路径
    （含单测 fixture）炸 `no such column`。
    """
    # ⚠ upgrade() uses only the URL's **directory**; filename is always bowhead.db (migrate.TARGET_FILES).
    # Another filename succeeds while migrating a different new database in that directory; separate the two directories.
    legacy_dir = tmp_path / "legacy"
    migrated_dir = tmp_path / "migrated"
    legacy_dir.mkdir()
    migrated_dir.mkdir()
    legacy = legacy_dir / "bowhead.db"
    migrated = migrated_dir / "bowhead.db"
    _create_1_0_fixture(migrated)
    assert upgrade(f"sqlite+aiosqlite:///{migrated}", target="core") == ("core",)

    ref = '{"form":"personal_skill","id_or_name":"weekly-report"}'

    async def _exercise() -> tuple[object, object]:
        legacy_store = ScheduledJobStore(str(legacy))
        await legacy_store.initialize()  # Only 1.0.0 transitional DDL, no recipe_ref column.
        plain = await legacy_store.create(user_id="u1", workspace_id="ws1", name="plain", cron="0 9 * * *", query="q")

        migrated_store = ScheduledJobStore(str(migrated))
        await migrated_store.initialize()
        bound = await migrated_store.create(user_id="u1", workspace_id="ws1", name="bound", cron="0 9 * * *", query="q", recipe_ref=ref)
        return plain, await migrated_store.get(bound.id)

    plain, bound = anyio.run(_exercise)

    assert plain.recipe_ref is None
    assert bound is not None and bound.recipe_ref == ref
