"""`is_turn_resume_ready` 对缺失 dispatch 行的 orphan Turn 必须 fail closed.

D0b 后 Web、scheduler 与 IM fresh execution 都经统一 `admit → spawn` 写 dispatch；因此正常
clarification producer 不再需要按来源豁免。这个文件用真实 `SessionStore` + **迁移过的**库
锁住兼容/损坏形状：Turn 虽已 parked，只要缺少 settlement proof 就不能消费用户决议。

`chat_turn_spawn_dispatch` 由 alembic `0010` 建，**不在** `SessionStore.initialize()` 里。
缺表代表部署未迁移完整，同样不是 settlement proof，必须 fail closed；迁移过的库另锁住
“表存在但 Turn 缺 dispatch 行”的损坏/历史形状。
"""

from __future__ import annotations

import sqlite3

import anyio
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.session_store import SessionStore


@pytest.fixture
def migrated_db(tmp_path):
    """1.0.0 基线 → 跑到 head 的库（= 生产形状，dispatch 表在）。"""
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_turn_spawn_dispatch'",
            ).fetchone()
            is not None
        ), "前提没立住：dispatch 表不在，下面测的就只是兼容捷径"
    return db_path


async def _parked_turn(store: SessionStore, turn_id: str) -> None:
    """造一个真·停在澄清点上的 turn（走 store 自己的状态机，不手写 UPDATE）。"""
    sess = await store.ensure_session(session_id=None, workspace_id="ws1", user_id="u1")
    await store.record_turn(turn_id=turn_id, session_id=sess.id, user_text="算一下本月的复购率")
    await store.mark_clarification_pending(turn_id=turn_id, intent=None)


def test_orphaned_parked_turn_without_dispatch_row_is_not_ready(migrated_db) -> None:
    """turn 已 durably `clarification_pending`、但没有 dispatch 行 → **判未就绪**。

    这是 pre-D0b 历史行、部分迁移或损坏数据的保守兼容边界；当前 scheduler 也必须写并收口
    dispatch，不能再凭 `clarification_pending` 状态单独授权 resume。
    """
    store = SessionStore(str(migrated_db))

    async def _run() -> bool:
        await store.initialize()
        await _parked_turn(store, "t_sched_probe")
        return await store.is_turn_resume_ready("t_sched_probe")

    assert anyio.run(_run) is False


def test_parked_turn_without_dispatch_schema_is_not_ready(tmp_path) -> None:
    """缺少 0010 dispatch schema 的部署也不能按 parked 状态消费决议。"""
    store = SessionStore(str(tmp_path / "unmigrated.db"))

    async def _run() -> bool:
        await store.initialize()
        await _parked_turn(store, "t_unmigrated_probe")
        return await store.is_turn_resume_ready("t_unmigrated_probe")

    assert anyio.run(_run) is False


def test_the_same_turn_is_parked_by_status(migrated_db) -> None:
    """单独的 parked 状态不是 dispatch settlement proof。"""
    store = SessionStore(str(migrated_db))

    async def _run() -> str | None:
        await store.initialize()
        await _parked_turn(store, "t_sched_probe2")
        turn = await store.get_turn("t_sched_probe2")
        return None if turn is None else turn.status

    # ⚠ Turn.status is str, not an enum with .value.
    assert anyio.run(_run) == "clarification_pending"


def test_unknown_turn_is_not_ready(migrated_db) -> None:
    """查无此 turn → 未就绪。"""
    store = SessionStore(str(migrated_db))

    async def _run() -> bool:
        await store.initialize()
        return await store.is_turn_resume_ready("t_does_not_exist")

    assert anyio.run(_run) is False
