"""credit-billing gap 7 保留策略 —— 日级汇总 + 90 天裁剪的语义 pin。

为什么要有保留策略：`llm_usage_events` 逐次 LLM 调用一行，实测每行 368 bytes（含索引）。
300 人常规用量（每人每工作日约 10 turn、每 turn 约 15 次调用）≈ 90 万行/月 = 3.7 GiB/年；
整库是单文件 SQLite 且备份按整文件复制，留 3 份历史备份就是 ~15 GiB 占盘 —— 而 116 那台机器
的盘本来就要靠清 docker 镜像腾空间。

设计是「先汇总再裁剪」（对应 model §9.1 的 `user_usage_rollups`）：日级汇总永久保留且极小，
所以裁掉原始明细**不损失任何分析能力**。本文件钉住三条不能破的性质：

1. **汇总幂等** —— 重跑不能把用量翻倍（`ON CONFLICT` 必须整段覆盖而非累加，这是这类汇总
   最经典的坑）
2. **没汇总的天绝不删** —— 顺序错会造成不可逆数据丢失，所以 prune 自身也要核对
3. **裁剪后历史仍可查** —— 从汇总表查得到超窗口的数据
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.user_cron import (
    DEFAULT_LLM_USAGE_RETENTION_DAYS,
    llm_usage_retention_days,
    rollup_and_prune_llm_usage,
)
from bowhead_core.user_store import UserStore

if TYPE_CHECKING:
    from pathlib import Path


def _day(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


async def _raw_rows(store: UserStore) -> int:
    async with aiosqlite.connect(store.db_path) as db, db.execute("SELECT COUNT(*) FROM llm_usage_events") as cur:
        return (await cur.fetchone())[0]  # type: ignore[index]


async def _seed(store: UserStore, *, days_ago: int, n: int = 3, reason: str = "turn") -> None:
    created = _day(days_ago)
    await store.record_llm_usage_events(
        [
            {
                "team_id": "team_a",
                "user_id": "u_1",
                "workspace_id": "ws_1",
                "reason": reason,
                "agent_type": "lead",
                "tokens_input": 1000,
                "tokens_output": 100,
                "created_at": created,
            }
            for _ in range(n)
        ]
    )


@pytest.fixture
async def store(tmp_path: Path) -> UserStore:
    s = UserStore(tmp_path / "ret.db")
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_rollup_aggregates_by_day_and_reason(store: UserStore) -> None:
    await _seed(store, days_ago=1, n=3, reason="turn")
    await _seed(store, days_ago=1, n=2, reason="dreaming")
    await _seed(store, days_ago=2, n=4, reason="turn")

    assert await store.rollup_llm_usage_daily() > 0

    rows = await store.llm_usage_daily_by_reason()
    by_reason = {r["reason"]: r for r in rows}
    assert by_reason["turn"]["calls"] == 7
    assert by_reason["turn"]["tokens_input"] == 7000
    assert by_reason["dreaming"]["calls"] == 2


@pytest.mark.asyncio
async def test_rollup_is_idempotent(store: UserStore) -> None:
    """★ 最经典的坑：重跑汇总不能把用量累加成翻倍。"""
    await _seed(store, days_ago=1, n=5)

    await store.rollup_llm_usage_daily()
    first = await store.llm_usage_daily_by_reason()
    for _ in range(3):
        await store.rollup_llm_usage_daily()
    again = await store.llm_usage_daily_by_reason()

    assert first == again, "重跑汇总后数字变了 —— ON CONFLICT 写成累加了"
    assert again[0]["calls"] == 5


@pytest.mark.asyncio
async def test_rollup_picks_up_late_arriving_rows_for_same_day(store: UserStore) -> None:
    """同一天后到的数据要能被下次汇总纳入（整段覆盖的另一面收益）。"""
    await _seed(store, days_ago=1, n=2)
    await store.rollup_llm_usage_daily()
    await _seed(store, days_ago=1, n=3)
    await store.rollup_llm_usage_daily()

    assert (await store.llm_usage_daily_by_reason())[0]["calls"] == 5


@pytest.mark.asyncio
async def test_prune_refuses_days_not_rolled_up(store: UserStore) -> None:
    """★ 没进汇总的天绝不删 —— 顺序错会造成不可逆丢失，prune 自身也要核对。"""
    await _seed(store, days_ago=200, n=4)
    assert await _raw_rows(store) == 4

    deleted = await store.prune_llm_usage_events(older_than=_day(90))

    assert deleted == 0
    assert await _raw_rows(store) == 4, "未汇总的原始行被删了 —— 数据不可恢复"


@pytest.mark.asyncio
async def test_prune_removes_old_rows_after_rollup(store: UserStore) -> None:
    await _seed(store, days_ago=200, n=4)
    await store.rollup_llm_usage_daily()

    deleted = await store.prune_llm_usage_events(older_than=_day(90))

    assert deleted == 4
    assert await _raw_rows(store) == 0


@pytest.mark.asyncio
async def test_prune_keeps_rows_inside_the_window(store: UserStore) -> None:
    await _seed(store, days_ago=200, n=4)  # Outside the window.
    await _seed(store, days_ago=5, n=6)  # Inside the window.
    await store.rollup_llm_usage_daily()

    await store.prune_llm_usage_events(older_than=_day(90))

    assert await _raw_rows(store) == 6, "窗口内的原始明细必须留着（排查用）"


@pytest.mark.asyncio
async def test_history_still_queryable_after_prune(store: UserStore) -> None:
    """★ 裁剪的前提是「不损失分析能力」：超窗口历史必须仍能从汇总查到。"""
    await _seed(store, days_ago=200, n=4, reason="dreaming")
    await rollup_and_prune_llm_usage(str(store.db_path), retention_days=90)

    assert await _raw_rows(store) == 0
    assert await store.llm_usage_by_reason() == [], "原始表已空"
    daily = await store.llm_usage_daily_by_reason()
    assert daily[0]["reason"] == "dreaming"
    assert daily[0]["calls"] == 4, "汇总里还在 —— 这才叫零信息损失"


@pytest.mark.asyncio
async def test_job_rolls_up_before_pruning(store: UserStore) -> None:
    """整合任务必须自己保证顺序：只调它一次，老数据应当既进汇总又被裁掉。"""
    await _seed(store, days_ago=200, n=7)

    res = await rollup_and_prune_llm_usage(str(store.db_path), retention_days=90)

    assert res["rolled"] > 0
    assert res["pruned"] == 7
    assert (await store.llm_usage_daily_by_reason())[0]["calls"] == 7


@pytest.mark.asyncio
async def test_retention_zero_means_rollup_only(store: UserStore) -> None:
    """保留天数设 0 = 只汇总不裁（给不想删数据的部署留出口）。"""
    await _seed(store, days_ago=200, n=3)

    res = await rollup_and_prune_llm_usage(str(store.db_path), retention_days=0)

    assert res["pruned"] == 0
    assert await _raw_rows(store) == 3


@pytest.mark.asyncio
async def test_job_on_empty_table_is_noop(store: UserStore) -> None:
    res = await rollup_and_prune_llm_usage(str(store.db_path), retention_days=90)
    assert res == {"rolled": 0, "pruned": 0, "retention_days": 90}


def test_retention_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_LLM_USAGE_RETENTION_DAYS", raising=False)
    assert llm_usage_retention_days() == DEFAULT_LLM_USAGE_RETENTION_DAYS

    monkeypatch.setenv("BOWHEAD_LLM_USAGE_RETENTION_DAYS", "30")
    assert llm_usage_retention_days() == 30

    monkeypatch.setenv("BOWHEAD_LLM_USAGE_RETENTION_DAYS", "0")
    assert llm_usage_retention_days() == 0

    # Invalid values fall back to defaults instead of crashing cron.
    monkeypatch.setenv("BOWHEAD_LLM_USAGE_RETENTION_DAYS", "ninety")
    assert llm_usage_retention_days() == DEFAULT_LLM_USAGE_RETENTION_DAYS


@pytest.mark.asyncio
async def test_null_attribution_rows_survive_rollup(store: UserStore) -> None:
    """归属为空的行（后台调用常见）不能因主键 NULL 而丢失或撞键。"""
    await store.record_llm_usage_events(
        [
            {
                "reason": "unattributed",
                "agent_type": "fast",
                "tokens_input": 500,
                "tokens_output": 50,
                "created_at": _day(1),
            }
        ]
        * 2
    )
    await store.rollup_llm_usage_daily()

    daily = await store.llm_usage_daily_by_reason()
    assert daily[0]["reason"] == "unattributed"
    assert daily[0]["calls"] == 2, "空归属的两行应聚成一条 calls=2, 而非因 NULL 主键丢失"


@pytest.mark.asyncio
async def test_prune_handles_many_days_without_hitting_sqlite_var_limit(store: UserStore) -> None:
    """★ 待裁天数 = IN 子句绑定参数数；年份级积压（从未跑过 cron 的老库）不能撞变量上限。"""
    for d in range(300, 100, -1):  # 200 distinct days.
        await _seed(store, days_ago=d, n=1)
    await store.rollup_llm_usage_daily()

    deleted = await store.prune_llm_usage_events(older_than=_day(90))

    assert deleted == 200
    assert await _raw_rows(store) == 0
