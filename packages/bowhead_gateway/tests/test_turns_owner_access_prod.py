"""turns 跨-user 鉴权 prod-mode 单测（review P1：补回删掉的 test_turns_owner_access 覆盖）.

main 的 ``_require_turn_access``：owner OR 同 team admin → 放行，否则 404（反枚举）。dev-fallback
（``BOWHEAD_DEV_USER_FALLBACK=1`` 且 auth_mode=dev）下信任 dev 单用户、跳所有权检查——故本测显式
关 dev-fallback，验 prod 真 enforce（events/cancel/satisfaction/retry_*/skip/replay 全挂这把锁）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.user_store import User
from bowhead_gateway.routes import turns as turns_routes
from bowhead_gateway.routes.turns import _require_turn_access, get_agent_binding_detail
from fastapi import HTTPException

from bowhead_core import SessionStore


class _US:
    """team-admin / team 关系 fake（_is_team_admin_for_turn_debug + _same_team_user 用）."""

    _admins = {"u_admin_a", "u_admin_b"}
    _team = {"u_alice": "team_a", "u_admin_a": "team_a", "u_admin_b": "team_b", "u_bob": "team_a"}

    def __init__(self) -> None:
        self.db_path = "unused.db"
        self.workspace_status = "active"
        self.removed: set[str] = set()

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
        return {
            "id": workspace_id,
            "team_id": "team_a",
            "status": self.workspace_status,
        }

    async def get_member_role(self, workspace_id, user_id):
        if workspace_id != "w1" or user_id in self.removed:
            return None
        return "admin" if user_id == "u_admin_a" else "member"

    # The following two methods serve membership checks (build_access_context).
    async def get_workspace_team_id(self, workspace_id):
        return "team_a" if workspace_id == "w1" else None

    async def get_user(self, user_id):
        return User(
            id=user_id,
            team_id=self._team.get(user_id) or "",
            email=f"{user_id}@x.com",
            status="active",
        )


async def _ctx(tmp_path):
    sess = SessionStore(str(tmp_path / "t.db"))
    await sess.initialize()
    await sess.ensure_session(session_id="s1", workspace_id="w1", user_id="u_alice")
    await sess.record_turn(turn_id="t1", session_id="s1", user_text="alice's turn")
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_store=sess,
            user_store=_US(),
            settings=SimpleNamespace(auth_mode="prod", dev_default_user="x"),
        )
    )
    return SimpleNamespace(app=app, headers={}, cookies={})


def _u(uid, team):
    return User(id=uid, team_id=team, email=f"{uid}@x.com")


@pytest.mark.integration
async def test_turn_access_prod_enforcement(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")  # Production: dev fallback off, real enforcement.
    req = await _ctx(tmp_path)
    # Owner alice → allow.
    assert (await _require_turn_access("t1", req, _u("u_alice", "team_a"))).id == "u_alice"
    # Other user bob (same team, not admin) → 404 (read leak / cancel-DoS exposure).
    with pytest.raises(HTTPException) as e_bob:
        await _require_turn_access("t1", req, _u("u_bob", "team_a"))
    assert e_bob.value.status_code == 404
    # Same-team admin → allow (debug override, main design).
    assert (await _require_turn_access("t1", req, _u("u_admin_a", "team_a"))).id == "u_admin_a"
    # Other-team admin → 404 (no cross-tenant leak).
    with pytest.raises(HTTPException) as e_xorg:
        await _require_turn_access("t1", req, _u("u_admin_b", "team_b"))
    assert e_xorg.value.status_code == 404


@pytest.mark.integration
async def test_agent_binding_detail_reuses_turn_access_and_returns_safe_ledger(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req = await _ctx(tmp_path)
    current_user = _u("u_alice", "team_a")

    async def fake_current_user(_request):
        return current_user

    class _BindingStore:
        def __init__(self, db_path):
            assert db_path == "unused.db"

        async def read_turn_binding_detail(self, *, team_id, turn_id):
            assert (team_id, turn_id) == ("team_a", "t1")
            return {
                "turn_id": turn_id,
                "root_binding": None,
                "executions": [
                    {
                        "agent_execution_id": "aex_1",
                        "parent_execution_id": None,
                        "agent_id": "system:compute",
                        "agent_source_kind": "system",
                        "agent_revision_id": "sar_1",
                        "agent_release_id": None,
                        "dispatch_source": "lead",
                        "dispatch_key": "compute",
                        "runtime_profile_name": "compute",
                        "display_name_snapshot": "Compute",
                        "capability_fingerprint": "sha256:capabilities",
                        "status": "completed",
                        "created_at": "2026-09-04T00:00:00Z",
                        "started_at": "2026-09-04T00:00:01Z",
                        "ended_at": "2026-09-04T00:00:02Z",
                        "failure_code": None,
                    }
                ],
            }

    monkeypatch.setattr(turns_routes, "_current_user", fake_current_user)
    monkeypatch.setattr(
        "bowhead_core.agent_execution_binding.AgentExecutionBindingStore",
        _BindingStore,
    )

    detail = await get_agent_binding_detail("t1", req)
    assert detail.turn_id == "t1"
    assert detail.executions[0].agent_id == "system:compute"
    assert "policy_fingerprint" not in detail.model_dump()["executions"][0]


@pytest.mark.integration
async def test_collaboration_details_require_original_actor_and_current_history_access(tmp_path, monkeypatch):
    from bowhead_core.agent_collaboration import authorize_result_exchange
    from bowhead_gateway.settings import Settings
    from cryptography.fernet import Fernet
    from fastapi import Response

    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    req = await _ctx(tmp_path)
    req.app.state.settings = Settings(_env_file=None, auth_mode="prod")
    current_user = _u("u_alice", "team_a")
    reads = []

    async def current(_request):
        return current_user

    class Store:
        def __init__(self, db_path, *, cipher):
            assert db_path == "unused.db"

        async def projection(self, reader, *, authorize):
            assert authorize is authorize_result_exchange
            reads.append(reader)
            return {"turn_id": reader.turn_id, "revision": 3, "participants": [], "topics": []}

    monkeypatch.setattr(turns_routes, "_current_user", current)
    monkeypatch.setattr("bowhead_core.agent_collaboration.CollaborationStore", Store)
    response = Response()
    assert (await turns_routes.get_collaboration_trace("t1", req, response))["revision"] == 3
    assert response.headers["Cache-Control"] == "private, no-store"
    assert reads[0].user_id == "u_alice" and reads[0].workspace_id == "w1"
    req.app.state.user_store.workspace_status = "archived"
    await turns_routes.get_collaboration_trace("t1", req, Response())
    current_user = _u("u_admin_a", "team_a")
    with pytest.raises(HTTPException) as admin_denied:
        await turns_routes.get_collaboration_trace("t1", req, Response())
    assert admin_denied.value.status_code == 404
    current_user = _u("u_alice", "team_a")
    req.app.state.user_store.removed.add("u_alice")
    with pytest.raises(HTTPException):
        await turns_routes.get_collaboration_trace("t1", req, Response())
    assert len(reads) == 2

    current_user = _u("u_bob", "team_a")
    with pytest.raises(HTTPException) as denied:
        await get_agent_binding_detail("t1", req)
    assert denied.value.status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize("fernet_root", [None, "invalid-root"])
async def test_collaboration_details_require_valid_instance_encryption(tmp_path, monkeypatch, fernet_root):
    from bowhead_gateway.settings import Settings
    from fastapi import Response

    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    if fernet_root is None:
        monkeypatch.delenv("BOWHEAD_FERNET_KEY", raising=False)
    else:
        monkeypatch.setenv("BOWHEAD_FERNET_KEY", fernet_root)
    req = await _ctx(tmp_path)
    req.app.state.settings = Settings(_env_file=None, auth_mode="prod")

    async def current(_request):
        return _u("u_alice", "team_a")

    def unexpected_store(*args, **kwargs):
        pytest.fail("Invalid encryption must not open collaboration storage")

    monkeypatch.setattr(turns_routes, "_current_user", current)
    monkeypatch.setattr("bowhead_core.agent_collaboration.CollaborationStore", unexpected_store)
    with pytest.raises(HTTPException) as unavailable:
        await turns_routes.get_collaboration_trace("t1", req, Response())
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail == "collaboration unavailable"


@pytest.mark.integration
async def test_turn_lifecycle_uses_persisted_session_space(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req = await _ctx(tmp_path)
    store = req.app.state.user_store
    store.workspace_status = "archived"
    alice = _u("u_alice", "team_a")

    historical = await _require_turn_access(
        "t1",
        req,
        alice,
        operation=SpaceOperation.READ_HISTORY,
    )
    assert historical.id == "u_alice"

    with pytest.raises(HTTPException) as execute_error:
        await _require_turn_access(
            "t1",
            req,
            alice,
            operation=SpaceOperation.EXECUTE,
        )
    assert execute_error.value.status_code == 409
    assert execute_error.value.detail["error"]["code"] == "workspace_archived"

    store.removed.add("u_alice")
    with pytest.raises(HTTPException) as removed_error:
        await _require_turn_access(
            "t1",
            req,
            alice,
            operation=SpaceOperation.READ_HISTORY,
        )
    assert removed_error.value.status_code == 404


@pytest.mark.integration
async def test_active_space_team_admin_reads_turn_without_space_membership(
    tmp_path,
    monkeypatch,
):
    """★ 正对照（此前零覆盖）：**active** 空间 + 同团队管理员 + **非空间成员** → 放行.

    这是 ``_require_turn_access`` 唯一真正靠 ``is_same_team_admin``（``turns.py:304``）
    活着的通路：管理员读本团队任意空间的 turn，不需要空间成员资格。PRD §5.5 **选 A**
    拍板**保留**它（理由不是「管理员理应有特权」，而是没有人决定过要收紧）。

    本文件此前唯一相关的用例 ``test_archived_effect_control_has_narrow_team_admin_override``
    量的是 **archived** + ``allow_archived_team_admin``，**不是** active——即这条通路
    在改造前没有任何测试钉住。没有它，把 turn 改走成员校验时撤掉这条路，测试不会红。
    """
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req = await _ctx(tmp_path)
    store = req.app.state.user_store
    # Validate fixture: active space, admin actually **not** a space member.
    assert store.workspace_status == "active"
    store.removed.add("u_admin_a")
    assert await store.get_member_role("w1", "u_admin_a") is None

    granted = await _require_turn_access(
        "t1",
        req,
        _u("u_admin_a", "team_a"),
        operation=SpaceOperation.READ_HISTORY,
    )
    assert granted.id == "u_admin_a"


@pytest.mark.integration
async def test_archived_effect_control_has_narrow_team_admin_override(
    tmp_path,
    monkeypatch,
):
    """★ archived 那一档**照旧收窄**——最容易做反的一格.

    ``turns.py`` 的 archived 分支不是「补成员校验」，是**带豁免的条件拒绝**。若把它当补校验删掉、
    再无条件传 ``allow_team_admin_override=True``，「archived + 管理员 + 非空间成员」会从 404 变成放行。
    默认路由仍 404，显式传 ``allow_archived_team_admin`` 才放行。
    """
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req = await _ctx(tmp_path)
    store = req.app.state.user_store
    store.workspace_status = "archived"
    store.removed.add("u_admin_a")
    admin = _u("u_admin_a", "team_a")

    with pytest.raises(HTTPException) as ordinary_history:
        await _require_turn_access(
            "t1",
            req,
            admin,
            operation=SpaceOperation.READ_HISTORY,
        )
    assert ordinary_history.value.status_code == 404

    control = await _require_turn_access(
        "t1",
        req,
        admin,
        operation=SpaceOperation.READ_HISTORY,
        allow_archived_team_admin=True,
    )
    assert control.id == "u_admin_a"

    with pytest.raises(HTTPException) as member:
        await _require_turn_access(
            "t1",
            req,
            _u("u_bob", "team_a"),
            operation=SpaceOperation.READ_HISTORY,
            allow_archived_team_admin=True,
        )
    assert member.value.status_code == 404


@pytest.mark.integration
async def test_removed_owner_is_blocked_while_team_admin_is_kept(tmp_path, monkeypatch):
    """批2：active 空间也查空间成员资格。三条腿必须在同一夹具里一起量.

    - **正对照**：仍是空间成员的属主 → 放行（证明不是把所有人都拦了）；
    - **管理员**：同团队 owner/team_admin、非空间成员 → **仍放行**（PRD §5.5 选 A）；
    - **主断言**：被移出空间的属主 → **404**（PRD §5.1）。口径是 404 反枚举而不是 403
      （docs-review D3）——统一入口抛的是 403，路由必须把授权失败映射回自己的口径。
    """
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req = await _ctx(tmp_path)
    store = req.app.state.user_store
    assert store.workspace_status == "active"

    assert (await _require_turn_access("t1", req, _u("u_alice", "team_a"))).id == "u_alice"

    store.removed.add("u_admin_a")
    assert (await _require_turn_access("t1", req, _u("u_admin_a", "team_a"))).id == "u_admin_a"

    store.removed.add("u_alice")
    with pytest.raises(HTTPException) as removed_owner:
        await _require_turn_access("t1", req, _u("u_alice", "team_a"))
    assert removed_owner.value.status_code == 404
    assert removed_owner.value.detail == "turn not found"


@pytest.mark.integration
async def test_membership_check_does_not_swallow_lifecycle_rejections(tmp_path, monkeypatch):
    """**生命周期**拒绝不得被 404 吞掉：archived + EXECUTE 仍是 409 workspace_archived.

    这是 D3 的另一半。把授权失败映射成 404 时很容易顺手把 409 一起映射掉，
    那会让「空间已归档」对用户显示成「turn 不存在」——错误分类不能说谎。
    """
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    req = await _ctx(tmp_path)
    req.app.state.user_store.workspace_status = "archived"

    with pytest.raises(HTTPException) as execute_error:
        await _require_turn_access(
            "t1",
            req,
            _u("u_alice", "team_a"),
            operation=SpaceOperation.EXECUTE,
        )
    assert execute_error.value.status_code == 409
    assert execute_error.value.detail["error"]["code"] == "workspace_archived"
