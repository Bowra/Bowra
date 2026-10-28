"""归属打点 → `metrics_event_log` → `/admin/metrics/rollup` 的端到端用例。

`test_artifact_attribution_guard.py` 里那几条用的是**桩** `metrics_log`：它只能证明
「我调了 `track`」，证明不了「行真的落了库」，更证明不了「rollup 能把分布读回来」，
也证明不了「遇到真的 sqlite 锁时请求不被拖住」。这条用真 `MetricsLog` + 真 `create_app()` +
真 rollup 路由把整条链跑一遍。

★ 每条「读到了」的断言都配一个 flag 关闭的对照：同一批调用、同一个读取窗口，
关着必须是 0。打点是后台任务，读之前先 `drain_attribution_metrics`——所以「读到 0」
只可能是「没排过任务」，不会是「还没写完」。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import aiosqlite
import httpx2
import pytest
from bowhead_core.metrics_log import MetricsLog
from bowhead_core.user_store import UserStore, _now
from bowhead_gateway.artifact_attribution import (
    ATTRIBUTION_METRIC_NAME,
    ATTRIBUTION_NONE,
    ATTRIBUTION_SESSION,
    ATTRIBUTION_SPACE,
    ATTRIBUTION_TEAM,
    drain_attribution_metrics,
    track_attribution,
)
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings

from bowhead_core import SessionStore

pytestmark = pytest.mark.integration

_FLAG = "FEATURE_ARTIFACT_ATTRIBUTION_METRICS"
_SECRET = "test_attribution_metrics_secret_xxxxxxxx"


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", _SECRET)
    monkeypatch.delenv("BOWHEAD_PROD", raising=False)
    monkeypatch.delenv(_FLAG, raising=False)
    db = tmp_path / "metrics.db"
    sess = SessionStore(str(db))
    await sess.initialize()
    user_store = UserStore(str(db))
    await user_store.initialize()
    metrics_log = MetricsLog(str(db))  # ★ Same SQLite file as artifacts/users, matching main.py assembly.
    await metrics_log.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = sess
    app.state.user_store = user_store
    app.state.metrics_log = metrics_log
    app.state.canvas_store = None
    app.state.orchestrator = None

    async with aiosqlite.connect(user_store.db_path) as conn:
        await conn.execute(
            "INSERT INTO teams(id, name, domain, max_seats, default_user_tier, "
            "team_credit_balance, team_credit_quota, tier_credits_json, status, created_at) "
            "VALUES ('team_a', 'Test Team', 'test.com', 10, 'ultra', 100000, 100000, '{}', 'active', ?)",
            (_now(),),
        )
        await conn.execute(
            "INSERT INTO users(id, team_id, email, email_verified, password_hash, "
            "oauth_providers_json, tier, credit_balance, credit_quota, "
            "credit_period_start, locale, timezone, preferences_json, status, created_at) "
            "VALUES ('u_admin', 'team_a', 'u_admin@test.com', 1, '', '[]', 'ultra', 30000, 30000, ?, "
            "'zh-CN', 'UTC', '{}', 'active', ?)",
            (_now(), _now()),
        )
        await conn.execute("UPDATE teams SET created_by_user_id = 'u_admin' WHERE id = 'team_a'")
        await conn.commit()
    return app


def _request_for(app) -> SimpleNamespace:
    """`track_attribution` 只读 `request.app.state`——传真 app，不传桩。"""
    return SimpleNamespace(app=app)


async def _rollup(app) -> dict:
    import jwt

    token = jwt.encode(
        {"sub": "u_admin", "team_id": "team_a", "iat": int(time.time()), "exp": int(time.time()) + 3600},
        _SECRET,
        algorithm="HS256",
    )
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as c:
        c.cookies.set("bowhead_jwt", token)
        r = await c.get("/api/v1/admin/metrics/rollup", params={"name": ATTRIBUTION_METRIC_NAME, "hours": 1})
    assert r.status_code == 200, r.text
    return r.json()


async def _emit_mixed_traffic(app) -> None:
    req = _request_for(app)
    for _ in range(3):
        await track_attribution(req, route="kb_single_upload", reason=ATTRIBUTION_NONE, scope="personal")
    await track_attribution(req, route="kb_single_upload", reason=ATTRIBUTION_SPACE, scope="personal")
    await track_attribution(req, route="kb_single_upload", reason=ATTRIBUTION_TEAM, scope="team")
    await track_attribution(req, route="files_upload", reason=ATTRIBUTION_NONE, source="upload")
    await track_attribution(req, route="files_upload", reason=ATTRIBUTION_NONE, source="python_sandbox")
    await track_attribution(req, route="files_presign", reason=ATTRIBUTION_SESSION)
    await drain_attribution_metrics(app)


async def test_flag_on_writes_rows_and_rollup_returns_the_distribution(app_ctx, monkeypatch) -> None:
    """★ 整条链：flag 开 → 真落库 → 一次 rollup 读出「各路由 × 各 reason」的分布。

    这就是打点存在的全部理由（支撑「没有归属输入时该落什么值」那个未拍板的决定）。
    ★ `source` 让 `files_upload` 里「用户上传」与「沙箱产出」分成两个桶——不带它，`reason=none` 的占比
      会被机器产出的量淹没（沙箱 runner 把每个输出文件都 POST 到 `/files/upload`）。
    """
    monkeypatch.setenv(_FLAG, "1")
    await _emit_mixed_traffic(app_ctx)

    data = await _rollup(app_ctx)

    assert data["total"] == 8
    assert data["by_attrs"] == {
        "reason=none,route=kb_single_upload,scope=personal": 3,
        "reason=space,route=kb_single_upload,scope=personal": 1,
        "reason=team,route=kb_single_upload,scope=team": 1,
        "reason=none,route=files_upload,source=upload": 1,
        "reason=none,route=files_upload,source=python_sandbox": 1,
        "reason=session,route=files_presign": 1,
    }


async def test_flag_off_control_same_calls_same_window_writes_nothing(app_ctx, monkeypatch) -> None:
    """★ 对照：**同一批调用、同一个读取窗口**，flag 关着必须是 0。

    没有它，上面「读到 8 条」不能排除别的解释；没有它，「读到 0」也分不清是没写还是窗口错了。
    """
    monkeypatch.delenv(_FLAG, raising=False)
    await _emit_mixed_traffic(app_ctx)

    data = await _rollup(app_ctx)

    assert data["total"] == 0
    assert data["by_attrs"] == {}


async def test_no_user_identifier_is_persisted(app_ctx, monkeypatch) -> None:
    """★ 不落用户标识：`user_id` 列必须是空。

    `metrics_event_log` 的 rollup 读不到 `user_id`，白存 90 天的「按人上传时间戳」没有消费方，
    而这张表不随用户删除清理（`memory_gc` 只按时间裁剪）。
    """
    monkeypatch.setenv(_FLAG, "1")
    await _emit_mixed_traffic(app_ctx)

    async with aiosqlite.connect(app_ctx.state.metrics_log._db_path) as db:
        rows = await (await db.execute("SELECT user_id FROM metrics_event_log WHERE name=?", (ATTRIBUTION_METRIC_NAME,))).fetchall()
    assert len(rows) == 8, "反空转：确实落了 8 行"
    assert {r[0] for r in rows} == {None}


async def test_locked_database_does_not_hold_the_request_and_the_row_lands_after_release(app_ctx, monkeypatch) -> None:
    """★★★ A 评审的 MEDIUM，用**真的 sqlite 写锁**复现。

    `MetricsLog` 与产物库是同一个文件，`track()` 自开连接、`busy_timeout=5000ms`。内联 `await` 时，
    别人持有写锁 ⇒ 请求被拖住最多 5 秒，而且**改变结果**（实测锁被占 7 秒：flag 关 500、flag 开 200）。

    这里让另一个连接 `BEGIN IMMEDIATE` 占住写锁：`track_attribution` 必须**立刻返回**（< 0.5s，
    对比 5s 的 busy_timeout 有 10 倍余量）；锁释放后那一行必须真的写进去（正对照：不是任务被丢了）。
    若被改回内联 `await`，这条会卡满 5 秒后失败（且那行会丢）。
    """
    monkeypatch.setenv(_FLAG, "1")
    db_path = app_ctx.state.metrics_log._db_path

    async with aiosqlite.connect(db_path) as holder:
        await holder.execute("BEGIN IMMEDIATE")  # Hold write lock.
        started = time.perf_counter()
        await track_attribution(_request_for(app_ctx), route="files_upload", reason=ATTRIBUTION_NONE, source="upload")
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"请求路径被写锁拖住了 {elapsed:.2f}s——打点必须脱离请求路径"
        await holder.rollback()  # Release lock.

    await drain_attribution_metrics(app_ctx)
    data = await _rollup(app_ctx)
    assert data["total"] == 1, "锁释放后那一行必须写进去（busy_timeout 未用尽）"
