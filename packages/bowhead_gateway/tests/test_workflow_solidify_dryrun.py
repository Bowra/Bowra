"""dry-run + judge 端点 e2e（workflow-solidify S3b / tech-design §7）.

真 SessionStore + SkillInstanceStore（验隔离 + verdict 落库）；fake orchestrator/judge/限流
（重/外部依赖）. 覆盖 §7 四断言：独立 scratch session≠原 / origin=workflow_dryrun / 原 session
不受影响 / 限流 429；外加 judge verdict 落 skill_instance + dispatch 失败走 error verdict.
⚠️ run_turn 真 dispatch 行为在 task#8 全栈 e2e 验.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core.events import EventBusRegistry
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.user_store import User
from bowhead_gateway.main import create_app
from bowhead_gateway.routes import workflow_solidify as ws
from bowhead_gateway.settings import Settings

from bowhead_core import SessionStore


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch):
    """Deployments always set BOWHEAD_FERNET_KEY; since 9c6fe1654 every chat worker turn builds the
    physical-usage receipt cipher from it. Without .env in the checkout nothing else provides it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


_JUDGE_PASS = json.dumps(
    {
        "verdict": "pass",
        "checkpoints": [{"text": "rows 含 channel+gmv", "pass": True, "reason": "答案给了各渠道金额"}],
    },
    ensure_ascii=False,
)


async def _persist_event(_event: object) -> bool:
    return True


# Real SessionStore objects always expose ``db_path``; the chat worker hands it to the
# physical-usage receipt observer (cfd302117, default on since 9c6fe1654). The observer only
# opens it when a physical model call starts, and this double's run_turn makes none.
_DOUBLE_SESSION_DB_PATH = "/nonexistent/test-session-store.db"


class _FakeDispatchStore:
    """Stateful subset required by the shared legacy admission/worker tail."""

    db_path = _DOUBLE_SESSION_DB_PATH

    def __init__(self) -> None:
        self.turns: dict[str, SimpleNamespace] = {}
        self.dispatch_owners: dict[str, str] = {}

    async def get_session(self, _session_id: str) -> None:
        return None

    async def ensure_session(
        self,
        *,
        session_id: str,
        workspace_id: str,
        user_id: str,
    ) -> None:
        return None

    async def record_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        user_text: str,
    ) -> None:
        self.turns.setdefault(
            turn_id,
            SimpleNamespace(
                id=turn_id,
                session_id=session_id,
                user_text=user_text,
                status="running",
            ),
        )

    async def get_turn(self, turn_id: str) -> SimpleNamespace | None:
        return self.turns.get(turn_id)

    async def claim_chat_spawn_dispatch_for_worker(
        self,
        *,
        turn_id: str,
        owner_id: str,
    ) -> str:
        if turn_id not in self.turns:
            return "untracked"
        if turn_id in self.dispatch_owners:
            return "rejected"
        self.dispatch_owners[turn_id] = owner_id
        return "claimed"

    async def heartbeat_chat_spawn_dispatch(
        self,
        *,
        turn_id: str,
        owner_id: str,
        settling: bool = False,
    ) -> str:
        del settling
        return "renewed" if self.dispatch_owners.get(turn_id) == owner_id else "lost"

    async def mark_chat_spawn_dispatch_terminal(
        self,
        *,
        turn_id: str,
        owner_id: str | None = None,
        failed: bool = False,
    ) -> bool:
        del failed
        if owner_id is not None and self.dispatch_owners.get(turn_id) != owner_id:
            return False
        self.dispatch_owners.pop(turn_id, None)
        return True

    async def mark_failed(
        self,
        *,
        turn_id: str,
        error: str,
        error_kind: str,
    ) -> bool:
        del error, error_kind
        turn = self.turns.get(turn_id)
        if turn is None or turn.status != "running":
            return False
        turn.status = "failed"
        return True

    async def materialize_steer_follow_up(
        self,
        *,
        session_id: str,
        current_turn_id: str,
        next_turn_id: str,
    ) -> list[str]:
        del session_id, current_turn_id, next_turn_id
        return []


class _FakeOrch:
    def __init__(self, *, final="渠道毛利：直营120w、分销80w，已按渠道降序。", error=None):
        self.final = final
        self.error = error
        self.calls: list = []
        self.session_store = _FakeDispatchStore()
        self.event_registry = EventBusRegistry(default_log_sink=_persist_event)
        self.dispatch_owner_id = "workflow_dryrun_fixture"
        self._cancel_events: dict[str, object] = {}

    async def run_turn(self, req):
        self.calls.append(req)
        status = "failed" if self.error else "completed"
        self.session_store.turns[req.turn_id].status = status
        return SimpleNamespace(
            final_answer=self.final,
            error=self.error,
            status=SimpleNamespace(value=status),
            tokens_input=0,
            tokens_output=0,
        )


class _FakeJudge:
    def __init__(self, content=_JUDGE_PASS):
        self._content = content
        self.calls: list = []

    def resolve(self, agent_type, profile=None, user_pref=None):
        return object()

    async def invoke(self, agent_type, req, profile=None):
        self.calls.append(agent_type)
        return SimpleNamespace(content=self._content)


class _PoisonRouter:
    """装配占位替身：被碰即计数（计数器 + 显式断言，不用 pytest.fail/AssertionError）。"""

    def __init__(self) -> None:
        self.touches = 0

    def __getattr__(self, name):
        self.touches += 1
        raise RuntimeError(f"assembly router must not be touched (attr={name})")


class _FakeUserStore:
    """rate_limit_check + review-fix 用的 ws-membership / team-verdict 方法."""

    def __init__(self, *, member_role="member", ws_team="team_a", team_skill=None, credit_balance=1_000):
        self.n = 0
        self.member_role = member_role  # None → not_a_member (authorization denial).
        self.ws_team = ws_team
        self.team_skill = team_skill  # SimpleNamespace(id=...) → get_team_skill_by_name hit.
        self.verdict_writes: list = []
        self.credit_balance = credit_balance
        self.credit_events: list = []

    async def get_user(self, user_id):
        return SimpleNamespace(id=user_id, credit_balance=self.credit_balance, credit_quota=1_000)

    async def get_team(self, team_id):
        return SimpleNamespace(id=team_id, team_credit_balance=100_000, team_credit_quota=100_000)

    async def record_credit_exhaustion_event(self, **kwargs):
        self.credit_events.append(kwargs)
        return {"id": "cex_test", **kwargs}

    async def rate_limit_check(self, key, *, limit, window_s):
        self.n += 1
        return self.n <= limit

    async def get_workspace_team_id(self, ws_id):
        return self.ws_team

    async def get_member_role(self, ws_id, user_id):
        return self.member_role

    async def get_team_skill_by_name(self, team_id, name, *, only_enabled=True, only_approved=True):
        # P1b: route must pass only_approved=False (management opt-out), otherwise pending-row verdict writeback is no-op.
        assert only_approved is False, "verdict 写回必须 opt-out only_approved"
        return self.team_skill

    async def set_team_skill_verdict(self, team_id, skill_id, *, verdict, verdict_json=None):
        self.verdict_writes.append((team_id, skill_id, verdict))
        return True


@pytest.fixture
async def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "1")
    db = tmp_path / "dry.db"
    sess = SessionStore(db)
    sis = SkillInstanceStore(db)
    await sess.initialize()
    await sis.initialize()
    # Original session (test isolation) + saved recipe (test verdict persistence).
    await sess.ensure_session(session_id="s_orig", workspace_id="w1", user_id="u_a")
    await sis.upsert(user_id="u_a", name="channel-gm", source_type="user_generated", source_path="/x")

    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = sess
    app.state.skill_instance_store = sis
    app.state.user_store = _FakeUserStore()
    app.state.orchestrator = _FakeOrch()
    # Batch 2-a site 3: injection moved from `app.state.router` (process assembly placeholder) to team router
    # resolver, with poison assembly substitute. **All assertions unchanged**. Patch consumer module:
    # workflow_solidify imports at module level; patching definition has no effect.
    _judge = _FakeJudge()
    poison = _PoisonRouter()
    app.state.router = poison
    app.state._poison_router = poison

    async def _fake_resolve(app_state, team_id, *, agent_type=None, site="unknown"):
        return _judge, None

    monkeypatch.setattr("bowhead_gateway.routes.workflow_solidify.resolve_team_router", _fake_resolve)

    # main #4 datarc per-user identity refactor renamed chat._resolve_turn_datarc_runtime →
    # datarc_context.resolve_datarc_turn_context (returns a ctx object, not a tuple).
    async def _stub_resolve(request, *, team_id, workspace_id, user_id=None, log=None):
        from bowhead_gateway.datarc_context import DatarcTurnContext

        return DatarcTurnContext(
            active_datarc_project=None,
            datarc_client=None,
            datarc_unavailable=False,
            vector_client=None,
            # No Datarc connection by default: the turn runs without BI and no
            # memberUid is required. Datarc-specific tests override this stub.
            datarc_project_for_calls="",
        )

    monkeypatch.setattr(
        "bowhead_gateway.datarc_context.resolve_datarc_turn_context",
        _stub_resolve,
    )
    from bowhead_gateway.turn_service import TeamModelRuntimeResolution

    async def _team_runtime(app_state, team_id, **_kwargs):
        return TeamModelRuntimeResolution(
            router=_judge,
            result="team_safe_success",
            reason="resolved",
        )

    monkeypatch.setattr(
        "bowhead_gateway.turn_service.resolve_team_model_runtime",
        _team_runtime,
    )
    app.dependency_overrides[ws._current_user] = lambda: User(
        id="u_a",
        team_id="team_a",
        email="a@x.com",
    )
    return app, sess, sis


def _client(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw")


def _body(**kw):
    return {"name": "channel-gm", "trigger_query": "今年各渠道毛利", "checkpoints": ["rows 含 channel+gmv", "按渠道降序"], "workspace_id": "w1", **kw}


@pytest.mark.integration
async def test_dryrun_isolated_scratch_session(app_ctx):
    app, sess, _sis = app_ctx
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
        assert r.status_code == 200, r.text
        data = r.json()
        scratch_id = data["scratch_session_id"]
        # (a) Separate scratch session ≠ original.
        assert scratch_id != "s_orig"
        # (b) scratch origin=workflow_dryrun.
        scratch = await sess.get_session(scratch_id)
        assert scratch is not None and scratch.origin == "workflow_dryrun"
        # (c) Original session unaffected (origin still normal).
        orig = await sess.get_session("s_orig")
        assert orig is not None and orig.origin == "normal"


@pytest.mark.integration
async def test_dryrun_judge_writes_personal_verdict(app_ctx):
    app, _sess, sis = app_ctx
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and data["verdict"] == "pass"
        assert data["checkpoints"] and data["checkpoints"][0]["pass"] is True
    # Persist verdict in skill_instance.
    inst = await sis.get(user_id="u_a", name="channel-gm")
    assert inst is not None and inst.verdict == "pass"
    assert "trigger_query" in (inst.verdict_json or "")


@pytest.mark.integration
async def test_dryrun_bus_close_failure_does_not_reclassify_success(app_ctx):
    class CloseFailRegistry(EventBusRegistry):
        def __init__(self) -> None:
            super().__init__(default_log_sink=_persist_event)
            self.close_calls: list[tuple[str, object | None]] = []

        async def close(self, turn_id: str, *, expected=None) -> None:
            self.close_calls.append((turn_id, expected))
            raise RuntimeError("close boom")

    app, _sess, _sis = app_ctx
    registry = CloseFailRegistry()
    app.state.orchestrator.event_registry = registry

    async with _client(app) as client:
        response = await client.post(
            "/api/v1/workflow-solidify/dry-run",
            json=_body(),
        )

    assert response.status_code == 200
    assert response.json()["verdict"] == "pass"
    assert len(registry.close_calls) == 2
    assert len({turn_id for turn_id, _expected in registry.close_calls}) == 1
    assert all(expected is not None for _turn_id, expected in registry.close_calls)


@pytest.mark.integration
async def test_dryrun_rate_limit_429(app_ctx):
    app, _sess, _sis = app_ctx
    async with _client(app) as c:
        for _ in range(6):  # limit=6 → first 6 allowed.
            assert (await c.post("/api/v1/workflow-solidify/dry-run", json=_body())).status_code == 200
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
        assert r.status_code == 429


@pytest.mark.integration
async def test_dryrun_credit_exhausted_402_without_turn_or_rate_slot(app_ctx):
    """试跑起的是真 turn：与 chat/send 同一 credit 准入；被拦下不吃限流名额。"""
    app, _sess, _sis = app_ctx
    us = _FakeUserStore(credit_balance=0)
    app.state.user_store = us
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
    assert r.status_code == 402, r.text
    assert r.json()["detail"]["code"] == "user_credit_exhausted"
    assert app.state.orchestrator.calls == []
    assert us.n == 0
    assert [e["code"] for e in us.credit_events] == ["user_credit_exhausted"]


@pytest.mark.integration
async def test_dryrun_dispatch_error_yields_error_verdict(app_ctx):
    app, _sess, sis = app_ctx
    app.state.orchestrator = _FakeOrch(final="", error="LLM exhausted")
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is False and data["verdict"] == "error"
        assert data["dispatch_error"]
        assert data["dispatch_error_code"] == "workflow_dry_run_execution_failed"
        assert data["dispatch_error_diagnostics"]["diagnostic_code"] == ("skill_workflow_failure")
        assert "LLM exhausted" not in str(data)
    inst = await sis.get(user_id="u_a", name="channel-gm")
    assert inst.verdict == "error"  # error also persisted (for UI review).


@pytest.mark.integration
async def test_dryrun_judge_skipped_when_no_checkpoints(app_ctx):
    app, _sess, _sis = app_ctx
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body(checkpoints=[]))
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == "skipped"


@pytest.mark.integration
async def test_dryrun_flag_off_404(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_CCA_WORKFLOW_CREATOR", "0")
    db = tmp_path / "off.db"
    sess = SessionStore(db)
    await sess.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.session_store = sess
    app.dependency_overrides[ws._current_user] = lambda: User(
        id="u_a",
        team_id="team_a",
        email="a@x.com",
    )
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
        assert r.status_code == 404


# ─── review fixes: dry-run space authorization (P1) + team verdict persisted in team store (P2) ──


@pytest.mark.integration
async def test_dryrun_cross_ws_denied_under_enforce(app_ctx, monkeypatch):
    """P1（review）：membership-enforce 开 + 非成员 workspace → 403 ws_access_denied，且不跑 turn."""
    app, _sess, _sis = app_ctx
    app.state.user_store = _FakeUserStore(member_role=None, ws_team="team_a")  # Nonmember.
    orch = _FakeOrch()
    app.state.orchestrator = orch
    monkeypatch.setattr(
        "bowhead_gateway.ws_authz.ws_membership_enforce_enabled",
        lambda: True,
    )
    async with _client(app) as c:
        r = await c.post(
            "/api/v1/workflow-solidify/dry-run",
            json=_body(workspace_id="ws_not_mine"),
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["error"]["code"] == "ws_access_denied"
    assert orch.calls == []  # Reject before dispatch.


@pytest.mark.integration
async def test_dryrun_team_skill_verdict_written_to_team_store(app_ctx):
    """P2（review）：form=team-skill → verdict 按 name→id 落 team_skill 表（不再 no-op 到 skill_instance）."""
    app, _sess, _sis = app_ctx
    us = _FakeUserStore(team_skill=SimpleNamespace(id="osk_1"))  # Member passes authorization by default.
    app.state.user_store = us
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body(form="team-skill"))
        assert r.status_code == 200, r.text
    assert us.verdict_writes and us.verdict_writes[0][1] == "osk_1"


@pytest.mark.integration
async def test_dryrun_null_workspace_id_400(app_ctx):
    """批1/c10 (impl review P1): FE 未选空间时 workspace_id=null——后端 fail-closed 400
    missing_workspace_context（c4 退役 team 兜底后不许静默跑错作用域）。FE 侧已改为从
    localStorage canonical key 带 workspaceId，本测试 pin 住 null 契约不回退成 500/静默跑。"""
    app, _sess, _sis = app_ctx
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body(workspace_id=None))
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"]["code"] == "missing_workspace_context"


@pytest.mark.integration
async def test_dryrun_turn_carries_team_router(app_ctx):
    """批 2-a 站点 3（C2 渠道）真闸：dry-run 的 TurnRequest 必须携带团队 router。

    不带的话 `turn_runner._effective_router` 会回退 `orch.router`＝进程级装配占位，
    整轮 dry-run 跑在假 router 上；而且 turn 先失败会**短路掉 judge 分支**，所以
    「只改 judge 的 router 来源」是死代码。摘掉实现里的 `team_router=team_router`
    必须让本条红。
    """
    app, _sess, _sis = app_ctx
    orch = app.state.orchestrator
    poison = app.state._poison_router
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
        assert r.status_code == 200, r.text
    assert orch.calls, "dry-run 应当真的跑了一轮 turn"
    req = orch.calls[0]
    assert getattr(req, "team_router", None) is not None, "TurnRequest 必须携带团队 router"
    assert req.team_router is not poison, "绝不能是进程级装配占位"
    assert poison.touches == 0, "全程不得碰装配 router"


@pytest.mark.integration
async def test_dryrun_turnrequest_carries_access_context(app_ctx):
    """dry-run 派发的 TurnRequest 必须带 `access_context`。

    ★ 2026-08-07 116 真机 dogfood 逮到的缺陷补闸：此前本处是全仓 8 个 TurnRequest
    构造点里**唯一**没设这个字段的，于是在开了 `BOWHEAD_RESOURCE_GRANTS_ENFORCE=1`
    的部署上，`turn_runner` 的守卫（access_store 存在 + 无 access_context + 强制开）
    直接判 `access_context_missing` —— **dry-run 100% 失败，0 秒返回**。

    为什么本文件既有的 8 条测试全绿却没逮到：它们断言的是端点结构、隔离、verdict、
    限流，没有一条看**派发出去的 TurnRequest 长什么样**。`_FakeOrch` 一直在
    `self.calls` 里记着它，只是没人断言。
    """
    app, _sess, _sis = app_ctx
    orch = app.state.orchestrator

    # `build_access_context` is **imported inside the tested function**, so patch its definition module.
    # (Shared `_FakeUserStore` cannot support real construction: missing get_user/get_team_role/
    # get_user_team_id; adding them affects 10 other cases in this file.)
    import bowhead_gateway.access_context as ac

    sentinel = SimpleNamespace(team_id="team_a", team_role="member", workspace_role="member", is_team_admin=False)
    captured: dict = {}

    async def _fake_build(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ac, "build_access_context", _fake_build)
    monkeypatch.setattr(ac, "access_context_to_dict", lambda ctx: {"team_id": ctx.team_id, "team_role": ctx.team_role})
    try:
        async with _client(app) as c:
            r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
            assert r.status_code == 200, r.text
    finally:
        monkeypatch.undo()

    # Positive control: actually dispatched a turn, or assertions below are vacuous.
    assert orch.calls, "run_turn 一次都没被调用——断言会空转"
    req = orch.calls[0]
    assert getattr(req, "access_context", None) == {"team_id": "team_a", "team_role": "member"}, (
        "dry-run 的 TurnRequest 没带 access_context —— 开启 resource-grants 强制的部署上，turn 会被 turn_runner 判 access_context_missing 而 0 秒失败"
    )
    # Context must use this request's space and actual user, never an arbitrary default.
    assert captured.get("workspace_id") == _body()["workspace_id"], captured
    assert captured.get("user_id"), captured


@pytest.mark.integration
async def test_dryrun_still_dispatches_when_access_context_unavailable(app_ctx):
    """建不起 access_context 时**仍要派发**，不新增拒绝分支。

    ★ 这条守的是我自己改坏过的地方：第一版写成「建不起来就返回 ws_access_denied」，
    把没开 resource-grants 强制的部署上本来能跑的 dry-run 也拦了。best-effort 才对——
    真该拒的时候由 turn_runner 按自己的规则判。
    """
    app, _sess, _sis = app_ctx
    orch = app.state.orchestrator
    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
        assert r.status_code == 200, r.text
    # Shared fixture _FakeUserStore cannot support real construction, exercising the failed-build path.
    assert orch.calls, "access_context 建不起来时被错误地拦掉了——这是新增的失败模式，不允许"


class _FakeMemberResolver:
    def __init__(self, *, member_uid: str | None) -> None:
        self.member_uid = member_uid
        self.calls: list = []

    async def resolve(self, identity, datarc_workspace_id, *, datarc=None):
        self.calls.append((identity, datarc_workspace_id, datarc))
        if self.member_uid is None:
            from bowhead_gateway.member_resolver import MemberNotFoundError

            raise MemberNotFoundError(identity, datarc_workspace_id)
        return self.member_uid


def _wire_datarc(monkeypatch, app, *, resolver, datarc_client):
    async def _stub_resolve(request, *, team_id, workspace_id, user_id=None, log=None):
        from bowhead_gateway.datarc_context import DatarcTurnContext

        return DatarcTurnContext(
            active_datarc_project="dw_sales",
            datarc_client=datarc_client,
            datarc_unavailable=False,
            vector_client=None,
            datarc_project_for_calls="dw_sales",
        )

    async def _verified_email(_request):
        return "a@x.com"

    monkeypatch.setattr("bowhead_gateway.datarc_context.resolve_datarc_turn_context", _stub_resolve)
    monkeypatch.setattr("bowhead_gateway.routes.chat._caller_verified_email", _verified_email)
    app.state.member_resolver = resolver


@pytest.mark.integration
async def test_dryrun_passes_resolved_datarc_member_uid(app_ctx, monkeypatch):
    """The dry-run turn carries the caller's real memberUid, resolved through the team connection."""
    app, _sess, _sis = app_ctx
    datarc_client = object()
    resolver = _FakeMemberResolver(member_uid="mu_123")
    _wire_datarc(monkeypatch, app, resolver=resolver, datarc_client=datarc_client)

    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
        assert r.status_code == 200, r.text
        assert r.json()["dispatch_error_code"] is None

    assert resolver.calls == [("a@x.com", "dw_sales", datarc_client)]
    assert app.state.orchestrator.calls, "the dry run must dispatch a turn"
    assert app.state.orchestrator.calls[0].member_uid == "mu_123"


@pytest.mark.integration
async def test_dryrun_unresolvable_member_disables_only_datarc(app_ctx, monkeypatch):
    """An unresolved Datarc member cannot block unrelated workflow steps."""
    app, _sess, _sis = app_ctx
    resolver = _FakeMemberResolver(member_uid=None)
    _wire_datarc(monkeypatch, app, resolver=resolver, datarc_client=object())

    async with _client(app) as c:
        r = await c.post("/api/v1/workflow-solidify/dry-run", json=_body())
        assert r.status_code == 200, r.text
        data = r.json()

    assert resolver.calls, "member resolution must have been attempted"
    assert app.state.orchestrator.calls
    req = app.state.orchestrator.calls[0]
    assert req.member_uid == ""
    assert req.datarc_client is None
    assert req.datarc_unavailable is True
    assert req.active_datarc_project is None
    assert req.datarc_absence_reason == "member_unavailable"
    assert data["dispatch_error_code"] is None
