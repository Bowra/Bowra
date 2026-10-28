"""管理员跨用户读 turn 要留痕（空间隔离批4 ④）。

缺口：`turns.py` 的 `is_same_team_admin` 那条豁免让同团队 owner / team_admin 读本团队
任意空间的 turn，**今天完全不留痕**。为了回答「这条豁免有没有人用」，2026-09-17 那次量
花了一个 40 分钟的 agent 跑，靠 nginx 日志 + 登录事件按 IP ±10s 配对**猜**，
8.9% 的请求根本归因不了；而 nginx 日志跟着容器走，当天发版重建容器后那段历史就没了。

事件名对齐既有 `admin.replay.viewed` 家族（那条今天只覆盖 memory_replay 路由，
**不覆盖 turns.py**）。

★ 本批**不改任何授权判定**：审计是旁路副作用，写失败不得影响请求
  （`user_audit_log` 抖一下不该让管理员读不了 turn）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.user_store import User
from bowhead_gateway.routes.turns import _require_turn_access

from bowhead_core import SessionStore

pytestmark = pytest.mark.integration


class _US:
    """最小 user_store fake + 记录 log_audit 调用。"""

    _admins = {"u_admin_a", "u_admin_b"}
    _team = {"u_alice": "team_a", "u_admin_a": "team_a", "u_admin_b": "team_b", "u_bob": "team_a"}

    def __init__(self) -> None:
        self.db_path = "unused.db"
        self.audits: list[dict] = []
        self.audit_raises = False

    async def is_team_admin(self, uid):
        return uid in self._admins

    async def get_team_role(self, uid, team):
        if self._team.get(uid) != team:
            return None
        return "team_admin" if uid in self._admins else "member"

    async def get_user_team_id(self, uid):
        return self._team.get(uid)

    async def workspace_in_team(self, team_id, workspace_id):
        return team_id == "team_a" and workspace_id == "w1"

    async def get_workspace(self, workspace_id):
        if workspace_id != "w1":
            return None
        return {"id": workspace_id, "team_id": "team_a", "status": "active"}

    async def get_member_role(self, workspace_id, user_id):
        return None if workspace_id != "w1" else "member"

    async def get_workspace_team_id(self, workspace_id):
        return "team_a" if workspace_id == "w1" else None

    async def get_user(self, user_id):
        return User(id=user_id, team_id=self._team.get(user_id) or "", email=f"{user_id}@x.com", status="active")

    async def log_audit(self, **kwargs):
        if self.audit_raises:
            raise RuntimeError("user_audit_log unavailable")
        self.audits.append(kwargs)


async def _ctx(tmp_path):
    sess = SessionStore(str(tmp_path / "t.db"))
    await sess.initialize()
    await sess.ensure_session(session_id="s1", workspace_id="w1", user_id="u_alice")
    await sess.record_turn(turn_id="t1", session_id="s1", user_text="alice's turn")
    store = _US()
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_store=sess,
            user_store=store,
            settings=SimpleNamespace(auth_mode="prod", dev_default_user="x"),
        )
    )
    return SimpleNamespace(app=app, headers={}, cookies={}, client=None), store


def _u(uid, team):
    return User(id=uid, team_id=team, email=f"{uid}@x.com")


async def test_admin_cross_user_read_is_audited(tmp_path, monkeypatch) -> None:
    """同团队管理员读别人的 turn ⇒ 落一条 `admin.turn.viewed`，记清楚谁读了谁的。"""
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req, store = await _ctx(tmp_path)

    assert (await _require_turn_access("t1", req, _u("u_admin_a", "team_a"))).id == "u_admin_a"

    assert len(store.audits) == 1, "管理员跨用户读 turn 没留痕"
    entry = store.audits[0]
    assert entry["event_type"] == "admin.turn.viewed"
    assert entry["user_id"] == "u_admin_a"
    assert entry["team_id"] == "team_a"
    assert entry["payload"]["turn_id"] == "t1"
    assert entry["payload"]["target_user_id"] == "u_alice"


async def test_owner_reading_their_own_turn_is_not_audited(tmp_path, monkeypatch) -> None:
    """★ 正对照：属主读自己的 turn **不**留痕。

    没有这一条，上面那条用例在「无条件每次都写一行」的实现下**同样会绿**——
    那种实现把审计表灌成访问日志，且回答不了「这条豁免有没有人用」这个唯一的问题。
    """
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req, store = await _ctx(tmp_path)

    assert (await _require_turn_access("t1", req, _u("u_alice", "team_a"))).id == "u_alice"
    assert store.audits == []


async def test_audit_failure_does_not_break_the_read(tmp_path, monkeypatch) -> None:
    """审计是旁路：`user_audit_log` 写不动时，管理员照样读得到（不改授权判定）。"""
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req, store = await _ctx(tmp_path)
    store.audit_raises = True

    assert (await _require_turn_access("t1", req, _u("u_admin_a", "team_a"))).id == "u_admin_a"


async def test_store_without_log_audit_still_serves(tmp_path, monkeypatch) -> None:
    """老夹具 / 精简 store 没有 `log_audit` 方法时不得 AttributeError。

    仓里确有这种 fake（`test_turns_owner_access_prod.py` 的 `_US`），
    它们**没有**这个方法；审计接线必须对此免疫，否则一条旁路副作用会掀翻既有用例。
    """
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req, store = await _ctx(tmp_path)
    # ⚠ Use monkeypatch.delattr, not bare delattr: `_US` **is** `type(store)`;
    # deleting directly then retrieving original `_US.log_audit` necessarily raises AttributeError (first-version mistake),
    # and manual class-attribute edits leak into other cases. monkeypatch restores automatically.
    monkeypatch.delattr(_US, "log_audit")
    assert not hasattr(store, "log_audit"), "前置不成立：方法没删掉，这条测试证明不了任何事"

    assert (await _require_turn_access("t1", req, _u("u_admin_a", "team_a"))).id == "u_admin_a"
