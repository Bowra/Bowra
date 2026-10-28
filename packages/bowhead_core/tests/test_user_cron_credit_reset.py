"""credit-billing P0-1 — 月度 credit reset 的 fail-safe 语义 pin。

`monthly_credit_reset` 曾用 `tier_credits.get(tier, 0)`: 只要 `user.tier` 不在
`teams.tier_credits_json` 里 (新增/改名 tier 档位、license 漏签 tier_credits 致其为 `{}`),
月初就把**全员** quota+balance 写成 0 → 此后每轮 chat 撞 credit 402。该函数此前零测试覆盖。

本文件钉死: 缺键 = 保留现有 quota (fail-safe) + 计数上报, 绝不归零。口径与
`UserStore.admin_period_reset` 的 `balance_and_quota` 分支一致。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.user_cron import monthly_credit_reset, next_monthly_credit_reset, teams_due_for_monthly_reset
from bowhead_core.user_store import UserStore

if TYPE_CHECKING:
    from pathlib import Path


async def _set_team_period_start(db_path: Path, team_id: str, value: str | None) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE teams SET team_credit_period_start = ? WHERE id = ?", (value, team_id))
        await db.commit()


async def _ledger(db_path: Path, user_id: str) -> list[tuple[int, str]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT delta, reason FROM credit_ledger WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ) as cur:
            return [(int(r["delta"]), r["reason"]) for r in await cur.fetchall()]


async def _wallet(db_path: Path, user_id: str) -> tuple[int, int]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT credit_quota, credit_balance FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    return int(row["credit_quota"]), int(row["credit_balance"])


async def _set_tier_credits(db_path: Path, team_id: str, tier_credits: dict[str, int]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE teams SET tier_credits_json = ? WHERE id = ?", (json.dumps(tier_credits), team_id))
        await db.commit()


async def _spend(db_path: Path, user_id: str, amount: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE users SET credit_balance = credit_balance - ? WHERE id = ?", (amount, user_id))
        await db.commit()


@pytest.fixture
async def store(tmp_path: Path) -> UserStore:
    s = UserStore(tmp_path / "cron.db")
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_reset_refills_balance_to_tier_quota(store: UserStore, tmp_path: Path) -> None:
    """基线: tier 键存在时按 tier default 重算 quota, balance 回满。"""
    team = await store.create_team(name="T", max_seats=5)
    u = await store.create_user(team_id=team.id, email="u@t.example", password="Passw0rd!", tier="ultra")
    await _spend(store.db_path, u.id, 25_000)

    res = await monthly_credit_reset(str(store.db_path))

    quota, balance = await _wallet(store.db_path, u.id)
    assert (quota, balance) == (30_000, 30_000)
    assert res["users_reset"] == 1
    assert res["tier_key_missing"] == 0


@pytest.mark.asyncio
async def test_missing_tier_key_keeps_quota_and_does_not_zero(store: UserStore, tmp_path: Path) -> None:
    """★核心回归: user.tier 不在 tier_credits 键内 → 保留现有 quota, 不归零。"""
    team = await store.create_team(name="T", max_seats=5)
    u = await store.create_user(team_id=team.id, email="u@t.example", password="Passw0rd!", tier="ultra")
    await _spend(store.db_path, u.id, 21_000)
    # Simulate added/renamed tiers: tier_credits contains only pro + enterprise, without ultra.
    await _set_tier_credits(store.db_path, team.id, {"pro": 5000, "enterprise": 50_000})

    res = await monthly_credit_reset(str(store.db_path))

    quota, balance = await _wallet(store.db_path, u.id)
    assert quota == 30_000, "缺键必须保留现有 quota, 不能写 0"
    assert balance == 30_000, "balance 仍回满到保留的 quota"
    assert res["tier_key_missing"] == 1, "缺键必须被计数上报"


@pytest.mark.asyncio
async def test_empty_tier_credits_does_not_zero_anyone(store: UserStore, tmp_path: Path) -> None:
    """license 漏签 tier_credits → tier_credits_json='{}' 时不得把全员打成 0。"""
    team = await store.create_team(name="T", max_seats=5)
    a = await store.create_user(team_id=team.id, email="a@t.example", password="Passw0rd!", tier="ultra")
    b = await store.create_user(team_id=team.id, email="b@t.example", password="Passw0rd!", tier="pro")
    await _spend(store.db_path, a.id, 30_000)  # Spend down to a zero balance.
    await _set_tier_credits(store.db_path, team.id, {})

    res = await monthly_credit_reset(str(store.db_path))

    assert await _wallet(store.db_path, a.id) == (30_000, 30_000)
    assert await _wallet(store.db_path, b.id) == (5000, 5000)
    assert res["tier_key_missing"] == 2


@pytest.mark.asyncio
async def test_quota_rewrite_is_counted(store: UserStore, tmp_path: Path) -> None:
    """admin 手调过的 quota 被按 tier 重写时必须计数 (设计要求覆盖可见)。"""
    team = await store.create_team(name="T", max_seats=5)
    u = await store.create_user(team_id=team.id, email="u@t.example", password="Passw0rd!", tier="ultra")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE users SET credit_quota = 20000 WHERE id = ?", (u.id,))
        await db.commit()

    res = await monthly_credit_reset(str(store.db_path))

    quota, _ = await _wallet(store.db_path, u.id)
    assert quota == 30_000
    assert res["quota_changed"] == 1


@pytest.mark.asyncio
async def test_stale_period_start_is_due(store: UserStore, tmp_path: Path) -> None:
    """★补跑回归: 上次重置在更早的月份 → due。

    旧实现把「本月跑过没」记在进程内存的 `last_monthly_run`, 且触发条件卡死
    `day == 1 and hour == 0`; 进程只要在 1 号那一小时整段不在(部署重建就够), 这个月就被
    静默跳过且永不补跑。判据改到库里之后, 恢复后的任意一次 tick 都能认出这笔欠账。
    """
    team = await store.create_team(name="T", max_seats=5)
    await _set_team_period_start(store.db_path, team.id, "2026-07-01T00:09:11.705837+00:00")

    due = await teams_due_for_monthly_reset(str(store.db_path), datetime(2026, 8, 12, 13, 0, tzinfo=UTC))

    assert due == [team.id], "上个月重置的 team 在这个月必须是 due"


@pytest.mark.asyncio
async def test_current_month_period_start_is_not_due(store: UserStore, tmp_path: Path) -> None:
    """★负对照: 本月已重置 → 不 due。

    没有这条, 上面那个「due」断言可能只是因为闸恒真 —— 那会变成每小时给全员重发额度。
    """
    team = await store.create_team(name="T", max_seats=5)
    await _set_team_period_start(store.db_path, team.id, "2026-08-01T00:09:11.705837+00:00")

    due = await teams_due_for_monthly_reset(str(store.db_path), datetime(2026, 8, 12, 13, 0, tzinfo=UTC))

    assert due == [], "本月已重置的 team 不能再次 due(否则每 tick 重发额度)"


@pytest.mark.asyncio
async def test_reset_is_idempotent_within_month(store: UserStore, tmp_path: Path) -> None:
    """重置完立刻再问一次 → 不再 due(period_start 已被推到本月)。"""
    team = await store.create_team(name="T", max_seats=5)
    await store.create_user(team_id=team.id, email="u@t.example", password="Passw0rd!", tier="ultra")
    await _set_team_period_start(store.db_path, team.id, "2026-07-01T00:00:00+00:00")

    due = await teams_due_for_monthly_reset(str(store.db_path))
    assert due == [team.id]
    await monthly_credit_reset(str(store.db_path), team_ids=due)

    assert await teams_due_for_monthly_reset(str(store.db_path)) == []


@pytest.mark.asyncio
async def test_create_team_persists_period_start_so_fresh_team_is_not_due(store: UserStore, tmp_path: Path) -> None:
    """★新建 team 必须把周期起点落库。

    `create_team` 的 dataclass 里算了 `team_credit_period_start=_now()`, 但 INSERT 的列清单
    漏了它 → 库里是 NULL → 「这个 team 的额度周期从哪天算」在首次重置前无从得知, 且按月度
    cron 的判据它一建出来就是「欠本月额度」。
    """
    team = await store.create_team(name="T", max_seats=5)

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT team_credit_period_start FROM teams WHERE id = ?", (team.id,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["team_credit_period_start"], "周期起点必须落库, 不能是 NULL"
    assert await teams_due_for_monthly_reset(str(store.db_path)) == [], "刚建的 team 本月不该欠额度"


@pytest.mark.asyncio
async def test_null_period_start_is_due(store: UserStore, tmp_path: Path) -> None:
    """历史行 period_start 为空 → 当作从没重置过, 至少发一次。"""
    team = await store.create_team(name="T", max_seats=5)
    await _set_team_period_start(store.db_path, team.id, None)

    assert await teams_due_for_monthly_reset(str(store.db_path)) == [team.id]


@pytest.mark.asyncio
async def test_team_ids_filter_leaves_other_teams_untouched(store: UserStore, tmp_path: Path) -> None:
    """★只重置 due 的 team: 本月已重置的 team 的用量不能被顺带抹掉。"""
    stale = await store.create_team(name="stale", max_seats=5)
    fresh = await store.create_team(name="fresh", max_seats=5)
    su = await store.create_user(team_id=stale.id, email="s@t.example", password="Passw0rd!", tier="ultra")
    fu = await store.create_user(team_id=fresh.id, email="f@t.example", password="Passw0rd!", tier="ultra")
    await _spend(store.db_path, su.id, 20_000)
    await _spend(store.db_path, fu.id, 20_000)
    await _set_team_period_start(store.db_path, stale.id, "2026-07-01T00:00:00+00:00")

    due = await teams_due_for_monthly_reset(str(store.db_path))
    assert due == [stale.id]
    await monthly_credit_reset(str(store.db_path), team_ids=due)

    assert await _wallet(store.db_path, su.id) == (30_000, 30_000), "due 的 team 该回满"
    assert await _wallet(store.db_path, fu.id) == (30_000, 10_000), "非 due 的 team 用量必须原样保留"


@pytest.mark.asyncio
async def test_reset_writes_period_reset_ledger_row(store: UserStore, tmp_path: Path) -> None:
    """★重置必须在账本上留痕: reason='period_reset', delta=余额变化。

    116 实测全库零条 reset 记录 —— 「这个月刷没刷」只能靠 credit_period_start 反推,
    而对账和用量分析读的都是 credit_ledger。形状与 admin_period_reset 对齐。
    """
    team = await store.create_team(name="T", max_seats=5)
    u = await store.create_user(team_id=team.id, email="u@t.example", password="Passw0rd!", tier="ultra")
    await _spend(store.db_path, u.id, 29_965)  # Leave 35, matching the actual liuyixin account on 116.

    res = await monthly_credit_reset(str(store.db_path))

    assert await _ledger(store.db_path, u.id) == [(29_965, "period_reset")]
    assert res["ledger_rows"] == 1


@pytest.mark.asyncio
async def test_reset_writes_no_ledger_row_when_balance_unchanged(store: UserStore, tmp_path: Path) -> None:
    """负对照: 余额本来就是满的 → delta=0 → 不写账本行(不制造噪音)。"""
    team = await store.create_team(name="T", max_seats=5)
    u = await store.create_user(team_id=team.id, email="u@t.example", password="Passw0rd!", tier="ultra")

    res = await monthly_credit_reset(str(store.db_path))

    assert await _ledger(store.db_path, u.id) == []
    assert res["ledger_rows"] == 0


@pytest.mark.asyncio
async def test_team_pool_refilled_and_inactive_user_skipped(store: UserStore, tmp_path: Path) -> None:
    """team 池回满到 team_credit_quota; 非 active 用户不参与。"""
    team = await store.create_team(name="T", max_seats=5)
    u = await store.create_user(team_id=team.id, email="u@t.example", password="Passw0rd!", tier="ultra")
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("UPDATE teams SET team_credit_balance = 7 WHERE id = ?", (team.id,))
        await db.execute("UPDATE users SET status = 'deleted' WHERE id = ?", (u.id,))
        await db.commit()

    res = await monthly_credit_reset(str(store.db_path))

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT team_credit_balance, team_credit_quota FROM teams WHERE id = ?", (team.id,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert int(row["team_credit_balance"]) == int(row["team_credit_quota"])
    assert res["users_reset"] == 0


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 8, 12, 13, 0, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
        (datetime(2026, 12, 31, 23, 59, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
        (datetime(2026, 8, 1, 0, 0, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
    ],
)
def test_next_monthly_credit_reset_is_next_utc_month_start(now: datetime, expected: datetime) -> None:
    assert next_monthly_credit_reset(now) == expected


@pytest.mark.asyncio
async def test_next_reset_matches_when_the_cron_reset_becomes_due(store: UserStore) -> None:
    """The advertised reset date is exactly when the cron starts treating the team as due."""
    team = await store.create_team(name="T", max_seats=5)
    period_start = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    await _set_team_period_start(store.db_path, team.id, period_start.isoformat())
    resets_at = next_monthly_credit_reset(period_start)

    just_before = await teams_due_for_monthly_reset(str(store.db_path), resets_at.replace(microsecond=0) - timedelta(seconds=1))
    at_boundary = await teams_due_for_monthly_reset(str(store.db_path), resets_at)

    assert just_before == []
    assert at_boundary == [team.id]
