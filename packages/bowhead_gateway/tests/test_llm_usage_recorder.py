"""credit-billing gap 7 第二刀 —— 用量落库 recorder 的语义 pin。

recorder 挂在 `Router.invoke` 的返回路径上（`bowhead_router.usage` 的 sink），所以它的
不变量首先是「便宜且不伤调用方」：
1. sink 只 append 内存, 不写库(逐条写会把 SQLite 单写者争用搬到 chat 热路径)
2. 落库失败要把批放回队首下轮重试, 不静默丢
3. 缓冲满时丢最旧的并计数 —— 用量是可观测数据, 不是钱账(钱账在 credit_ledger)
4. 取消时先 flush 再退出, 正常关停不丢
5. **只写 llm_usage_events, 绝不动余额**
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from bowhead_core.user_store import UserStore
from bowhead_gateway.llm_usage_recorder import LLMUsageRecorder
from bowhead_router.usage import LLMUsageRecord, UsageContext

if TYPE_CHECKING:
    from pathlib import Path


def _record(*, reason: str = "turn", tokens_in: int = 100, tokens_out: int = 20, **ctx: object) -> LLMUsageRecord:
    return LLMUsageRecord(
        agent_type="lead",
        provider="p1",
        model="m1",
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        tokens_cached=0,
        streamed=False,
        context=UsageContext(reason=reason, **ctx),  # type: ignore[arg-type]
    )


@pytest.fixture
async def store(tmp_path: Path) -> UserStore:
    s = UserStore(tmp_path / "usage.db")
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_sink_buffers_without_touching_db(store: UserStore) -> None:
    """★ sink 必须只碰内存 —— flush 之前库里应当一行都没有。"""
    rec = LLMUsageRecorder(store)
    rec.sink(_record())
    rec.sink(_record())

    assert rec.stats["buffered"] == 2
    assert await store.llm_usage_by_reason() == []  # Not flushed yet.

    assert await rec.flush_once() == 2
    rows = await store.llm_usage_by_reason()
    assert rows == [{"reason": "turn", "calls": 2, "tokens_input": 200, "tokens_output": 40, "tokens_cached": 0}]


@pytest.mark.asyncio
async def test_context_fields_land_in_the_row(store: UserStore) -> None:
    rec = LLMUsageRecorder(store)
    rec.sink(_record(team_id="t_1", user_id="u_1", workspace_id="ws_1", turn_id="t_abc"))
    await rec.flush_once()

    import aiosqlite

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM llm_usage_events") as cur:
            row = dict(await cur.fetchone())  # type: ignore[arg-type]
    assert (row["team_id"], row["user_id"], row["workspace_id"], row["turn_id"]) == ("t_1", "u_1", "ws_1", "t_abc")
    assert (row["reason"], row["agent_type"], row["provider"], row["model"]) == ("turn", "lead", "p1", "m1")
    assert row["streamed"] == 0


@pytest.mark.asyncio
async def test_reason_breakdown_separates_background_calls(store: UserStore) -> None:
    """★ gap 7 的产出口: 后台调用与用户请求驱动的用量必须可分开看。"""
    rec = LLMUsageRecorder(store)
    rec.sink(_record(reason="turn", tokens_in=1000, tokens_out=200))
    rec.sink(_record(reason="dreaming", tokens_in=300, tokens_out=50))
    rec.sink(_record(reason="dreaming", tokens_in=200, tokens_out=40))
    rec.sink(_record(reason="unattributed", tokens_in=10, tokens_out=5))
    await rec.flush_once()

    by_reason = {r["reason"]: r for r in await store.llm_usage_by_reason()}
    assert by_reason["turn"]["calls"] == 1
    assert by_reason["dreaming"]["calls"] == 2
    assert by_reason["dreaming"]["tokens_input"] == 500
    assert by_reason["unattributed"]["calls"] == 1
    # Descending total tokens: operators can readily see who is spending.
    assert [r["reason"] for r in await store.llm_usage_by_reason()][0] == "turn"


@pytest.mark.asyncio
async def test_flush_failure_requeues_the_batch(store: UserStore) -> None:
    """★ 落库失败不静默丢: 批回到队首, 下轮重试。"""

    class _BrokenStore:
        db_path = store.db_path
        calls = 0

        async def record_llm_usage_events(self, rows: list[dict[str, object]]) -> int:
            _BrokenStore.calls += 1
            if _BrokenStore.calls == 1:
                raise RuntimeError("disk full")
            return await store.record_llm_usage_events(rows)

    rec = LLMUsageRecorder(_BrokenStore())  # type: ignore[arg-type]
    rec.sink(_record(reason="turn"))

    assert await rec.flush_once() == 0  # First attempt fails.
    assert rec.stats["buffered"] == 1, "失败的批必须留在缓冲里"
    assert await rec.flush_once() == 1  # Second succeeds.
    assert rec.stats["written"] == 1


@pytest.mark.asyncio
async def test_buffer_cap_drops_oldest_and_counts(store: UserStore) -> None:
    """缓冲满时丢最旧的并计数 —— 不能让记账把内存吃穿。"""
    rec = LLMUsageRecorder(store, max_buffer=100)  # Lower bound clamped to 100.
    for i in range(150):
        rec.sink(_record(tokens_in=i + 1))

    assert rec.stats["buffered"] == 100
    assert rec.stats["dropped"] == 50


@pytest.mark.asyncio
async def test_cancel_flushes_remaining(store: UserStore) -> None:
    """★ 正常关停不丢: 取消 run() 时先把缓冲写完。"""
    rec = LLMUsageRecorder(store, interval_s=3600)  # Ensure periodic flush is not responsible.
    task = asyncio.create_task(rec.run())
    await asyncio.sleep(0)  # Let run() reach sleep.
    rec.sink(_record(reason="dreaming"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await store.llm_usage_by_reason())[0]["reason"] == "dreaming"


@pytest.mark.asyncio
async def test_recording_never_moves_any_balance(store: UserStore) -> None:
    """★ 计量 ≠ 计费: recorder 落一堆用量后, 钱账(钱包/ledger)必须逐字节不变。"""
    team = await store.create_team(name="T", max_seats=5)
    user = await store.create_user(team_id=team.id, email="u@t.example", password="Passw0rd!", tier="ultra")
    before = (user.credit_balance, user.credit_quota)

    rec = LLMUsageRecorder(store)
    for _ in range(20):
        rec.sink(_record(reason="dreaming", team_id=team.id, user_id=user.id, tokens_in=5000, tokens_out=5000))
    await rec.flush_once()

    after_user = await store.get_user(user.id)
    assert after_user is not None
    assert (after_user.credit_balance, after_user.credit_quota) == before

    # Query both tables directly, not a helper with hasattr fallback that would make assertions vacuous when methods are absent.
    import aiosqlite

    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM credit_ledger") as cur:
            ledger_rows = (await cur.fetchone())[0]  # type: ignore[index]
        async with db.execute("SELECT COUNT(*) FROM llm_usage_events") as cur:
            usage_rows = (await cur.fetchone())[0]  # type: ignore[index]
        async with db.execute("SELECT team_credit_balance FROM teams WHERE id = ?", (team.id,)) as cur:
            team_balance = (await cur.fetchone())[0]  # type: ignore[index]
    assert ledger_rows == 0, "计量不该往 credit_ledger（钱账）写任何行"
    assert usage_rows == 20, "用量该全部落在 llm_usage_events"
    assert team_balance == team.team_credit_balance, "team 池也不能动"


@pytest.mark.asyncio
async def test_empty_flush_is_noop(store: UserStore) -> None:
    rec = LLMUsageRecorder(store)
    assert await rec.flush_once() == 0


@pytest.mark.asyncio
async def test_failed_flush_at_full_buffer_counts_the_displaced_records(store: UserStore) -> None:
    """★ 落库失败后 requeue：缓冲已满时 extendleft 会从右端挤掉最新记录，必须计入 dropped。

    否则这条路径上的丢失完全静默 —— 「用量数字对不上」时无从判断是漏记还是真没发生。
    """

    class _AlwaysFails:
        async def record_llm_usage_events(self, rows: list[dict[str, object]]) -> int:
            raise RuntimeError("disk full")

    rec = LLMUsageRecorder(_AlwaysFails(), max_buffer=100)  # type: ignore[arg-type]
    for _ in range(100):
        rec.sink(_record())
    assert rec.stats == {"buffered": 100, "written": 0, "dropped": 0}

    # flush takes 100 (_FLUSH_BATCH=500 > 100), requeues all on failure; room=100, so must not falsely count dropped.
    assert await rec.flush_once() == 0
    assert rec.stats["buffered"] == 100
    assert rec.stats["dropped"] == 0

    # Fill again: flush took 100, sink adds 100, requeue room=0 → 100 displaced records must count as dropped.
    assert await rec.flush_once() == 0  # Takes 100; remember before failure.
    for _ in range(100):
        rec.sink(_record())
    assert await rec.flush_once() == 0
    assert rec.stats["dropped"] > 0, "被挤掉的记录必须计数，不能静默丢"
