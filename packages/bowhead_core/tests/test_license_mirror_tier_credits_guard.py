"""dev 2026-07-23 事故的根因回归 —— license mirror 不得用空 tier_credits 覆盖 team 配置。

真实事故链（本文件先完整复现，再钉住修复）：

1. 签发 license 时漏了 `--tier-credits` → payload `tier_credits={}`
2. 激活时 `create_team` **有兜底**（`tier_credits or {默认}`），所以 team 行一开始是对的，
   首个管理员也拿到正常额度 —— 事故因此不会在激活当场暴露
3. **首次启动的 `apply_local_license_mirror` 用 license 的 `{}` 覆盖了 `teams.tier_credits_json`**
   （这一层原先没有兜底）
4. 此后每个新开通用户 `credit_quota=0` → 每轮 chat 撞 402；而前端把 credit-402 显示成
   「授权未通过」，于是诊断整体跑偏到 license 方向

修复口径与 `monthly_credit_reset` 的缺键 fail-safe 一致：空额度表一定是签发疏漏，不是
「该团队所有 tier 都是零额度」这种意图，所以保留现值 + ERROR 告警。
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest
from bowhead_core.user_store import UserStore

if TYPE_CHECKING:
    from pathlib import Path

_GOOD = {"pro": 5000, "ultra": 30000}


def _team_row(db_path: Path, team_id: str) -> dict[str, object]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT tier_credits_json, max_seats, default_user_tier, contract_expires_at FROM teams WHERE id = ?",
            (team_id,),
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    return dict(row)


async def _mirror(store: UserStore, team_id: str, tier_credits: dict[str, int], **over: object) -> None:
    kwargs: dict[str, object] = {
        "team_id": team_id,
        "max_seats": 10,
        "default_user_tier": "ultra",
        "tier_credits": tier_credits,
        "contract_expires_at": None,
        "last_verified_at": "2026-07-25T00:00:00+00:00",
        "last_good_health": "active",
    }
    kwargs.update(over)
    await store.apply_local_license_mirror(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
async def store(tmp_path: Path) -> UserStore:
    s = UserStore(tmp_path / "mirror.db")
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_empty_tier_credits_does_not_wipe_the_team_config(store: UserStore) -> None:
    """★ 核心回归: license 漏签 tier_credits 时不得把 team 的额度表覆盖成 {}。"""
    team = await store.create_team(name="datarc", max_seats=10)
    assert team.tier_credits, "前置: create_team 的兜底应给出非空默认值"

    await _mirror(store, team.id, {})

    row = _team_row(store.db_path, team.id)
    assert json.loads(str(row["tier_credits_json"])) == _GOOD, "空 tier_credits 把 team 配置擦掉了"


@pytest.mark.asyncio
async def test_new_user_after_empty_mirror_still_gets_quota(store: UserStore) -> None:
    """★ 事故的用户可见后果: 空覆盖之后开通的用户额度为 0、每轮 402。"""
    team = await store.create_team(name="datarc", max_seats=10)
    await _mirror(store, team.id, {})

    later = await store.create_user(team_id=team.id, email="later@datarc.cn", password="Passw0rd!", tier="ultra")

    assert later.credit_quota == 30_000, "新用户被开通成 0 额度 —— 这就是 dev 的现象"
    assert later.credit_balance == 30_000


@pytest.mark.asyncio
async def test_non_credit_fields_still_mirror_when_tier_credits_empty(store: UserStore) -> None:
    """兜底只保护额度表；席位/默认 tier/到期时间仍必须按 license 更新, 否则会漏掉真实变更。"""
    team = await store.create_team(name="datarc", max_seats=10)

    await _mirror(
        store,
        team.id,
        {},
        max_seats=42,
        default_user_tier="pro",
        contract_expires_at="2027-01-01T00:00:00+00:00",
    )

    row = _team_row(store.db_path, team.id)
    assert row["max_seats"] == 42
    assert row["default_user_tier"] == "pro"
    assert row["contract_expires_at"] == "2027-01-01T00:00:00+00:00"
    assert json.loads(str(row["tier_credits_json"])) == _GOOD


@pytest.mark.asyncio
async def test_non_empty_tier_credits_still_overwrites(store: UserStore) -> None:
    """正常情况必须照写 —— 别把兜底做成「永不更新额度表」。"""
    team = await store.create_team(name="datarc", max_seats=10)

    await _mirror(store, team.id, {"pro": 1, "ultra": 2})

    row = _team_row(store.db_path, team.id)
    assert json.loads(str(row["tier_credits_json"])) == {"pro": 1, "ultra": 2}


@pytest.mark.asyncio
async def test_recovery_sequence_restores_existing_zero_quota_users(store: UserStore) -> None:
    """已经坏掉的实例怎么救（dev 就处在这个状态）: 装对 license 只修 team 配置，
    存量 0 额度用户要靠 period_reset(balance_and_quota) 按 tier 回填。"""
    team = await store.create_team(name="datarc", max_seats=10)
    admin = await store.create_user(team_id=team.id, email="admin@datarc.cn", password="Passw0rd!", tier="ultra")
    # Manually put team config and one user into the post-incident state (bypassing the fixed mirror).
    con = sqlite3.connect(store.db_path)
    con.execute("UPDATE teams SET tier_credits_json = '{}' WHERE id = ?", (team.id,))
    con.commit()
    con.close()
    broken = await store.create_user(team_id=team.id, email="later@datarc.cn", password="Passw0rd!", tier="ultra")
    assert broken.credit_quota == 0, "前置: 复现出 0 额度用户"

    # Step 1: install a new license with tier_credits (equivalent to a correct mirror write after restart).
    await _mirror(store, team.id, _GOOD)
    still = await store.get_user(broken.id)
    assert still is not None and still.credit_quota == 0, "mirror 不碰钱包 —— 存量用户仍需回填"

    # Step 2: backfill existing wallets by tier.
    await store.admin_period_reset(team_id=team.id, by_user_id=admin.id, mode="balance_and_quota", confirm=True)

    fixed = await store.get_user(broken.id)
    assert fixed is not None
    assert (fixed.credit_quota, fixed.credit_balance) == (30_000, 30_000)
