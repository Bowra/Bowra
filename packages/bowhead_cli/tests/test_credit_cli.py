"""credit-billing gap 15 —— CLI credit 命令组的行为 pin。

此前只有 admin HTTP API 能查/改额度。license 事故时（典型：签发漏 tier_credits → 全员
quota=0 → 每轮 402，而 local 部署下改配置接口又恒返回 409 managed_by_license）唯一自救
手段是手写 SQL 改库。这组命令是那种场合的逃生门。

重点钉两条容易写成空壳的行为：
1. `reconcile` 必须**真能检出**差额（只会打 0 的对账等于没有对账）
2. `topup` 必须**两层都补**（发送前预检同时查用户钱包和 team 池，只补一层仍会 402）
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from bowhead_cli.main import app
from bowhead_core.user_store import UserStore
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _seed(db: Path) -> tuple[str, str]:
    """CLI 是同步入口（内部自己 asyncio.run），所以测试也保持同步 —— 否则会撞
    「asyncio.run() cannot be called from a running event loop」。"""

    async def _inner() -> tuple[str, str]:
        store = UserStore(db)
        await store.initialize()
        team = await store.create_team(name="Acme", max_seats=5)
        user = await store.create_user(team_id=team.id, email="a@acme.example", password="Passw0rd!", tier="ultra")
        return team.id, user.id

    return asyncio.run(_inner())


def _run(args: list[str], db: Path) -> str:
    result = runner.invoke(app, ["credit", *args, "--db", f"sqlite+aiosqlite:///{db}"])
    assert result.exit_code == 0, result.output
    return result.output


def test_show_reports_wallets_and_flags_exhaustion(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    team_id, user_id = _seed(db)
    con = sqlite3.connect(db)
    con.execute("UPDATE users SET credit_balance = 0 WHERE id = ?", (user_id,))
    con.commit()
    con.close()

    out = _run(["show"], db)
    assert "a@acme.example" in out
    assert team_id in out
    assert "402" in out, "余额为 0 必须明确提示会 402（这是最常见的误诊点）"


def test_show_warns_on_empty_tier_credits(tmp_path: Path) -> None:
    """★ 空 tier_credits 是 dev 2026-07-23 全员 0 额度事故的根因，必须一眼可见。"""
    db = tmp_path / "c.db"
    team_id, _ = _seed(db)
    con = sqlite3.connect(db)
    con.execute("UPDATE teams SET tier_credits_json = '{}' WHERE id = ?", (team_id,))
    con.commit()
    con.close()

    out = _run(["show"], db)
    assert "tier_credits 为空" in out


def test_usage_separates_billed_turn_from_unbilled_background(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    team_id, _ = _seed(db)
    store = UserStore(db)
    asyncio.run(
        store.record_llm_usage_events(
            [
                {"reason": "turn", "agent_type": "lead", "tokens_input": 100_000, "tokens_output": 20_000, "team_id": team_id},
                {"reason": "dreaming", "agent_type": "echo", "tokens_input": 4_000, "tokens_output": 300, "team_id": team_id},
            ]
        )
    )

    out = _run(["usage"], db)
    assert "turn" in out
    assert "dreaming" in out
    assert "已计费" in out
    assert "未计费" in out, "后台调用当前不计费，必须标出来，否则看不出这份数据的用途"


def test_usage_on_empty_table_says_so(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    _seed(db)
    out = _run(["usage"], db)
    assert "没有 llm_usage_events 记录" in out


def test_reconcile_reports_zero_gap_when_books_match(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    team_id, user_id = _seed(db)
    store = UserStore(db)
    ok, _ = asyncio.run(
        store.try_deduct_credit(
            user_id=user_id,
            team_id=team_id,
            cost_credits=120,
            turn_id="t_1",
            workspace_id="ws_1",
            tokens_input=100_000,
            tokens_output=20_000,
        )
    )
    assert ok

    out = _run(["reconcile"], db)
    assert "未解释差额=0" in out
    assert "差额非 0" not in out


def test_reconcile_actually_detects_a_gap(tmp_path: Path) -> None:
    """★ 只会打 0 的对账等于没有对账 —— 人为造账差，必须报出来。"""
    db = tmp_path / "c.db"
    team_id, _ = _seed(db)
    con = sqlite3.connect(db)
    con.execute("UPDATE teams SET team_credit_balance = team_credit_balance - 777 WHERE id = ?", (team_id,))
    con.commit()
    con.close()

    out = _run(["reconcile"], db)
    assert "未解释差额=777" in out
    assert "差额非 0" in out


def test_reconcile_explains_gap_by_admin_grant(tmp_path: Path) -> None:
    """grant 是从 team 池划拨到用户钱包，属正常运营痕迹，不该被算成漏账。"""
    db = tmp_path / "c.db"
    team_id, user_id = _seed(db)
    store = UserStore(db)
    asyncio.run(store.admin_grant(user_id=user_id, team_id=team_id, amount=500, by_user_id="admin_1"))

    out = _run(["reconcile"], db)
    assert "未解释差额=0" in out, out


def test_reconcile_surfaces_deduct_failures(tmp_path: Path) -> None:
    """扣费失败此前只落事件表、没有任何对账面。"""
    db = tmp_path / "c.db"
    team_id, user_id = _seed(db)
    store = UserStore(db)
    asyncio.run(
        store.record_credit_exhaustion_event(
            code="credit_deduct_failed",
            user_id=user_id,
            team_id=team_id,
            credit_balance=0,
            team_credit_balance=0,
        )
    )

    out = _run(["reconcile"], db)
    assert "credit_deduct_failed" in out


def test_topup_raises_both_layers(tmp_path: Path) -> None:
    """★ 预检同时查用户钱包和 team 池，只补一层仍会 402 —— 必须两层都动。"""
    db = tmp_path / "c.db"
    team_id, user_id = _seed(db)
    con = sqlite3.connect(db)
    con.execute("UPDATE users SET credit_balance = 0, credit_quota = 0 WHERE id = ?", (user_id,))
    con.execute("UPDATE teams SET team_credit_balance = 0 WHERE id = ?", (team_id,))
    con.commit()
    con.close()

    out = _run(["topup", "-e", "a@acme.example", "-a", "5000", "--yes"], db)
    assert "已充值" in out

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    user = dict(con.execute("SELECT credit_balance, credit_quota FROM users WHERE id=?", (user_id,)).fetchone())
    team = dict(con.execute("SELECT team_credit_balance FROM teams WHERE id=?", (team_id,)).fetchone())
    con.close()
    assert user["credit_balance"] == 5000
    assert user["credit_quota"] == 5000, "quota 要跟上，否则下次月度重置会把余额压回去"
    assert team["team_credit_balance"] == 5000, "team 池必须同步补，否则仍撞 team_pool_exhausted"


def test_topup_warns_that_manual_quota_does_not_survive_reset(tmp_path: Path) -> None:
    """手工调的 quota 会被月初 cron 按 tier 重算 —— 不告知等于给人埋坑。"""
    db = tmp_path / "c.db"
    _seed(db)
    out = _run(["topup", "-e", "a@acme.example", "-a", "10", "--yes"], db)
    assert "月初 cron" in out


def test_topup_rejects_unknown_email_and_nonpositive_amount(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    _seed(db)
    dburl = f"sqlite+aiosqlite:///{db}"

    missing = runner.invoke(app, ["credit", "topup", "-e", "nope@x.example", "-a", "10", "--yes", "--db", dburl])
    assert missing.exit_code != 0

    bad = runner.invoke(app, ["credit", "topup", "-e", "a@acme.example", "-a", "0", "--yes", "--db", dburl])
    assert bad.exit_code != 0


def test_topup_can_skip_team_pool_when_asked(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    team_id, _ = _seed(db)
    con = sqlite3.connect(db)
    before = con.execute("SELECT team_credit_balance FROM teams WHERE id=?", (team_id,)).fetchone()[0]
    con.close()

    _run(["topup", "-e", "a@acme.example", "-a", "100", "--no-team-pool", "--yes"], db)

    con = sqlite3.connect(db)
    after = con.execute("SELECT team_credit_balance FROM teams WHERE id=?", (team_id,)).fetchone()[0]
    con.close()
    assert after == before


def test_reconcile_does_not_mistake_period_reset_for_a_grant(tmp_path: Path) -> None:
    """period_reset 也写正 delta，但它是两层各自重置、不是池→钱包划拨。

    早期实现用 `delta > 0` 一把抓，会把重置行当成 grant 算进转移项 → 报出假差额。
    """
    db = tmp_path / "c.db"
    team_id, user_id = _seed(db)
    con = sqlite3.connect(db)
    # Create a period_reset row with a positive delta (the two balances remain consistent, so the ledgers should reconcile).
    con.execute(
        "INSERT INTO credit_ledger(id,user_id,team_id,workspace_id,turn_id,delta,reason,"
        "tokens_input,tokens_output,created_at) VALUES (?,?,?,NULL,NULL,?,?,0,0,?)",
        ("led_reset", user_id, team_id, 900, "period_reset", "2026-07-01T00:00:00+00:00"),
    )
    con.commit()
    con.close()

    out = _run(["reconcile"], db)
    assert "未解释差额=0" in out, out


def test_usage_on_legacy_db_without_the_table_gives_guidance(tmp_path: Path) -> None:
    """★ llm_usage_events 是新表；比它更早的库里不存在，不该甩 traceback。"""
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    # Create only an unrelated table to simulate a database created by an older version.
    con.execute("CREATE TABLE teams (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()

    result = runner.invoke(app, ["credit", "usage", "--db", f"sqlite+aiosqlite:///{db}"])
    assert result.exit_code == 0, result.output
    assert "还没有 llm_usage_events" in result.output


def test_usage_switches_to_rollup_beyond_retention_window(tmp_path: Path) -> None:
    """★ 原始明细只留 90 天：问更长窗口必须改查日级汇总，否则静默少报。"""
    db = tmp_path / "c.db"
    team_id, _ = _seed(db)
    store = UserStore(db)
    old_day = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    asyncio.run(
        store.record_llm_usage_events(
            [
                {
                    "reason": "dreaming",
                    "agent_type": "echo",
                    "tokens_input": 4_000,
                    "tokens_output": 300,
                    "team_id": team_id,
                    "created_at": old_day,
                }
            ]
        )
    )
    asyncio.run(store.rollup_llm_usage_daily())
    asyncio.run(store.prune_llm_usage_events(older_than=(datetime.now(UTC) - timedelta(days=90)).isoformat()))

    # Within the window (30 days by default) → raw records, which have already been pruned here.
    short = _run(["usage", "--days", "30"], db)
    assert "原始明细" in short

    # Beyond the window → aggregates, which still include the entry from 200 days ago.
    long = _run(["usage", "--days", "365"], db)
    assert "日级汇总" in long
    assert "dreaming" in long
