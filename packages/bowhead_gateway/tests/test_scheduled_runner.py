"""run_scheduled_turn — the §3.3③ scheduled-turn gate (tech-design d3).

Denied team → a finite error code with NO run_turn call (zero LLM);
allowed/shadow → runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace

import aiosqlite
import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core._db import close_pooled_readers
from bowhead_core.entitlements import CommunityResolver
from bowhead_core.events import EventBusRegistry
from bowhead_core.session_store import SessionStore
from bowhead_core.user_store import UserStore
from bowhead_gateway._scheduled_runner import (
    admit_scheduled_execution,
    run_scheduled_turn,
)

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

# Real SessionStore objects always expose ``db_path``; the chat worker hands it to the
# physical-usage receipt observer (cfd302117, default on since 9c6fe1654). Billing also
# reads the persisted Turn after execution. Tests asserting settlement must provide
# a persisted Session/Turn instead of this default path.
_DOUBLE_SESSION_DB_PATH = "/nonexistent/test-session-store.db"


async def _persist_event(_event: object) -> bool:
    return True


class _FakeSessionStore:
    """Minimal stateful Store matching the shared legacy dispatch contract."""

    db_path = _DOUBLE_SESSION_DB_PATH

    def __init__(self, backing_store: SessionStore | None = None) -> None:
        self._backing_store = backing_store
        if backing_store is not None:
            self.db_path = backing_store.db_path
        self.materialized: list[tuple[str, str, str]] = []
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
        if self._backing_store is not None:
            await self._backing_store.ensure_session(session_id=session_id, workspace_id=workspace_id, user_id=user_id)
        return

    async def record_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        user_text: str,
    ) -> None:
        if self._backing_store is not None:
            await self._backing_store.record_turn(turn_id=turn_id, session_id=session_id, user_text=user_text)
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
        self.materialized.append((session_id, current_turn_id, next_turn_id))
        return []


class _FakeOrch:
    def __init__(self, *, tokens_input=0, tokens_output=0, final_answer="", status="completed", session_store: _FakeSessionStore | None = None):
        self.called = False
        self.request = None
        self.session_store = session_store if session_store is not None else _FakeSessionStore()
        self.event_registry = EventBusRegistry(default_log_sink=_persist_event)
        self.dispatch_owner_id = "scheduled_runner_fixture"
        self._ti = tokens_input
        self._to = tokens_output
        self._fa = final_answer
        self._status = status  # real TurnStatus value; "completed" == success

    async def run_turn(self, _tr):
        self.called = True
        self.request = _tr
        self.session_store.turns[_tr.turn_id].status = self._status
        return SimpleNamespace(
            status=SimpleNamespace(value=self._status),
            tokens_input=self._ti,
            tokens_output=self._to,
            final_answer=self._fa,
        )


@pytest.fixture
async def store(tmp_path):
    us = UserStore(str(tmp_path / "r.db"))
    await us.initialize()
    return us


@pytest.fixture
async def billing_stores(tmp_path):
    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}", target="core") == ("core",)
    users = UserStore(str(core_path))
    sessions = SessionStore(core_path)
    try:
        await users.initialize()
        await sessions.initialize()
        yield users, sessions
    finally:
        await close_pooled_readers()


async def _set_team(db_path, team_id, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE teams SET {cols} WHERE id = ?", (*fields.values(), team_id))
        await db.commit()


async def _set_user(db_path, user_id, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE users SET {cols} WHERE id = ?", (*fields.values(), user_id))
        await db.commit()


async def _user_balance(db_path, user_id):
    async with aiosqlite.connect(db_path) as db, db.execute("SELECT credit_balance FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
        return row[0] if row else None


async def _member_job(store, team, user, *, name="Sales"):
    """Make `user` a workspace member (creator) and return a job bound to that ws — so the turn
    clears entitlement + authz and reaches the credit gate."""
    await store.set_team_creator(team.id, user.id)
    ws_id = await store.create_local_workspace(team_id=team.id, name=name, creator_user_id=user.id)
    return _job(user.id, workspace_id=ws_id)


class _MemberResolver:
    """routine 的 memberUid 按**创建时快照**的工作区、用 owner 已验证身份解析."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def resolve(self, identity: str, datarc_workspace_id: str, *, datarc: object = None) -> str:
        self.calls.append((identity, datarc_workspace_id, datarc))
        return "m_owner"


class _ConnectionStore:
    async def list_connections(self, team_id: str):
        return [SimpleNamespace(id="conn_sched", base_url="https://datarc.example")]

    async def get_connection_auth(self, conn_id: str, team_id: str) -> str:
        return "auth_sched"


class _DatarcFactory:
    def __init__(self, client: object) -> None:
        self.client = client

    def get_client(self, base_url: str, auth_key: str) -> object:
        return self.client


class _EmbedFactory:
    def __init__(self, client: object) -> None:
        self.client = client

    async def get_verified_client(
        self,
        base_url: str,
        auth_key: str,
        *,
        project_ids: Sequence[str] = (),
    ) -> object:
        # Real factory now determines collection tenant segment from visible Datarc workspaces; replacement must follow the contract,
        # otherwise resolve's except swallows signature-mismatch TypeError as no embed.
        self.project_ids = list(project_ids)
        return self.client


class _TeamRouterRegistryStub:
    """credential-readiness D3：runner 现要求 turn 携带团队 router，未配置即
    暂停。本文件绝大多数用例测的是 run 语义（配置视为就绪），默认给一个恒返
    stub router 的注册表；测「未配置 → 暂停」的用例显式传 None 注册表。"""

    def __init__(self, router: object | None = object()) -> None:
        self._router = router

    async def get(self, team_id: str, cfg: object, version: int) -> object | None:
        return self._router


def _app(orch, ent, *, user_store=None, access_control_store=None, **state_overrides):
    state_overrides.setdefault("team_router_registry", _TeamRouterRegistryStub())
    return SimpleNamespace(
        state=SimpleNamespace(
            orchestrator=orch,
            entitlements=ent,
            user_store=user_store,
            access_control_store=access_control_store,
            **state_overrides,
        )
    )


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch: pytest.MonkeyPatch):
    """Deployments always set BOWHEAD_FERNET_KEY; the receipt observer's cipher requires it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


@pytest.fixture(autouse=True)
def _team_router_via_stub(monkeypatch: pytest.MonkeyPatch):
    """credential-readiness D3：runner 现经 resolve_team_byo_overrides 解析团队
    router。真解析需要 user_store + 注册表双件套——本文件的 fixture 谱系刻意保持
    user_store=None（authz 走跳过分支），故把解析 patch 成直接读 app.state 上的
    _TeamRouterRegistryStub；「未配置 → 暂停」用例传 _TeamRouterRegistryStub(None)。"""

    async def _fake(app_state, team_id):
        reg = getattr(app_state, "team_router_registry", None)
        if isinstance(reg, _TeamRouterRegistryStub):
            return await reg.get(team_id, {}, 0), None
        return None, None

    monkeypatch.setattr("bowhead_gateway.turn_service.resolve_team_byo_overrides", _fake)


def _job(user_id="u1", workspace_id="ws", datarc_workspace_id=None):
    return SimpleNamespace(
        id="sj1",
        user_id=user_id,
        workspace_id=workspace_id,
        name="j",
        query="q",
        anchor_session_id=None,
        notify_channels={},
        # Batch 2/§3.9 decision 10: routine queries the Datarc workspace **snapshotted at creation** (pin short-circuit),
        # independent of owner preference changes.
        datarc_workspace_id=datarc_workspace_id,
    )


def _resolver(us):
    return CommunityResolver(mode="saas", user_store=us)


async def test_allowed_team_runs(store):
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    orch = _FakeOrch()
    _outcome = await run_scheduled_turn(_app(orch, _resolver(store)), _job(user.id))
    tid, err = _outcome.turn_id, _outcome.error
    assert err is None and tid.startswith("t_sched_")
    assert orch.called is True


class _SavedLanguageSettings:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    async def list_for_user(self, _user_id: str) -> dict[str, str]:
        return dict(self.values)


@pytest.mark.parametrize(
    ("query", "saved", "expected"),
    [
        ("每周一汇总上周各省销售额", {}, "zh-CN"),
        ("Summarize last week's revenue by province", {}, "en-US"),
        ("每周一汇总上周各省销售额", {"output_language": "en-US"}, "en-US"),
    ],
    ids=["zh-prompt", "en-prompt", "saved-preference"],
)
async def test_scheduled_turn_language_follows_the_job_prompt_when_unset(store, query, saved, expected):
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    orch = _FakeOrch()
    job = _job(user.id)
    job.query = query
    app = _app(orch, _resolver(store), user_settings_store=_SavedLanguageSettings(saved))
    outcome = await run_scheduled_turn(app, job)
    assert outcome.error is None
    assert orch.request.language_context is not None
    assert orch.request.language_context.output_language == expected


@pytest.mark.parametrize(
    ("job_tz", "expected_today"),
    [
        # 2026-10-09T01:00Z is 18:00 on Oct 8 in Los Angeles but already 09:00 on Oct 9 at the +8 deploy clock.
        ("America/Los_Angeles", "2026-10-08"),
        ("Mars/Base", "2026-10-09"),  # an unknown saved zone keeps the deploy clock
    ],
    ids=["job-zone", "invalid-zone"],
)
async def test_scheduled_run_dates_the_model_on_the_job_zone(store, monkeypatch, job_tz, expected_today):
    from datetime import UTC, datetime

    from bowhead_core.model_clock import model_today, turn_clock_scope

    from bowhead_router import model_clock

    instant = datetime(2026, 10, 9, 1, 0, tzinfo=UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(model_clock, "datetime", _Frozen)
    monkeypatch.delenv("BOWHEAD_TZ_OFFSET_HOURS", raising=False)

    class _ClockOrch(_FakeOrch):
        async def run_turn(self, _tr):
            with turn_clock_scope(_tr):  # what TurnRunner.run binds for every turn
                self.today = model_today().isoformat()
            return await super().run_turn(_tr)

    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    orch = _ClockOrch()
    job = _job(user.id)
    job.tz = job_tz
    outcome = await run_scheduled_turn(_app(orch, _resolver(store)), job)
    assert outcome.error is None
    assert orch.today == expected_today


async def test_bus_close_failure_does_not_reclassify_success(store):
    class CloseFailRegistry(EventBusRegistry):
        def __init__(self) -> None:
            super().__init__(default_log_sink=_persist_event)
            self.close_calls: list[tuple[str, object | None]] = []

        async def close(self, turn_id: str, *, expected=None) -> None:
            self.close_calls.append((turn_id, expected))
            raise RuntimeError("close boom")

    team = await store.create_team(name="A")
    user = await store.create_user(
        team_id=team.id,
        email="close-fault@a.com",
        password="Passw0rd!x",
    )
    orch = _FakeOrch()
    registry = CloseFailRegistry()
    orch.event_registry = registry

    outcome = await run_scheduled_turn(
        _app(orch, _resolver(store)),
        _job(user.id),
    )

    assert outcome.error is None
    assert outcome.turn_id is not None
    assert len(registry.close_calls) == 1
    assert registry.close_calls[0][0] == outcome.turn_id


async def test_no_orchestrator():
    _outcome = await run_scheduled_turn(_app(None, None), _job())
    tid, err = _outcome.turn_id, _outcome.error
    assert tid is None and err == "orchestrator_not_ready"


async def test_authz_denied_non_member_no_run(store):
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    orch = _FakeOrch()
    _outcome = await run_scheduled_turn(
        _app(orch, _resolver(store), user_store=store),
        _job(user.id),
    )
    tid, err = _outcome.turn_id, _outcome.error
    assert tid is None
    assert err == "ws_access_denied"
    assert orch.called is False


async def test_authz_member_turn_has_access_context(store):
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    await store.set_team_creator(team.id, user.id)
    ws_id = await store.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=user.id,
    )
    orch = _FakeOrch()
    _outcome = await run_scheduled_turn(
        _app(orch, _resolver(store), user_store=store),
        _job(user.id, workspace_id=ws_id),
    )
    tid, err = _outcome.turn_id, _outcome.error
    assert err is None
    assert tid and tid.startswith("t_sched_")
    assert orch.called is True
    assert orch.request.team_id == team.id
    assert orch.request.access_context["workspace_role"] == "ws_admin"


async def test_archived_space_denied_before_scheduler_claim(store):
    team = await store.create_team(name="A")
    user = await store.create_user(
        team_id=team.id,
        email="u@a.com",
        password="Passw0rd!x",
    )
    job = await _member_job(store, team, user)
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "UPDATE workspaces SET status='archived' WHERE id=?",
            (job.workspace_id,),
        )
        await db.commit()

    admitted = await admit_scheduled_execution(
        _app(_FakeOrch(), _resolver(store), user_store=store),
        job,
    )

    assert admitted is False


async def test_scheduled_turn_threads_datarc_runtime_context(store):
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    await store.set_team_creator(team.id, user.id)
    # Owner identity must be verified to resolve memberUid (unverified email must not impersonate a real member).
    await _set_user(store.db_path, user.id, email_verified=1)
    ws_id = await store.create_local_workspace(
        team_id=team.id,
        name="Sales",
        creator_user_id=user.id,
    )
    datarc_client = object()
    vector_client = object()
    orch = _FakeOrch()
    member_resolver = _MemberResolver()

    _outcome = await run_scheduled_turn(
        _app(
            orch,
            _resolver(store),
            user_store=store,
            member_resolver=member_resolver,
            datarc_connection_store=_ConnectionStore(),
            datarc_client_factory=_DatarcFactory(datarc_client),
            embed_client_factory=_EmbedFactory(vector_client),
        ),
        _job(user.id, workspace_id=ws_id, datarc_workspace_id="dp_sched"),
    )
    tid, err = _outcome.turn_id, _outcome.error

    assert err is None
    assert tid and tid.startswith("t_sched_")
    assert orch.called is True
    # Datarc workspace = creation snapshot (no longer reads project_memory default under ws key; that path retired).
    assert orch.request.active_datarc_project == "dp_sched"
    assert orch.request.datarc_client is datarc_client
    assert orch.request.datarc_unavailable is False
    assert orch.request.vector_client is vector_client
    # Resolve memberUid with owner's verified identity + **client from team connection** (since 9e186d5d, client required;
    # omission makes every scheduled task fail with TypeError).
    assert orch.request.member_uid == "m_owner"
    assert member_resolver.calls == [
        ("u@a.com", "dp_sched", datarc_client),
        ("u@a.com", "dp_sched", datarc_client),
    ]


# ── credit gate (Routines Phase A) ──────────────────────────────────────────────


async def test_credit_exhausted_user_skipped_no_run(store):
    """User out of credit returns a stable code with no LLM turn."""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    await _set_user(store.db_path, user.id, credit_balance=0)
    orch = _FakeOrch(tokens_input=100, tokens_output=100)
    _outcome = await run_scheduled_turn(_app(orch, _resolver(store), user_store=store), job)
    tid, err = _outcome.turn_id, _outcome.error
    assert tid is None and err == "user_credit_exhausted"
    assert orch.called is False


async def test_credit_exhausted_team_skipped_no_run(store):
    """An empty team pool returns a stable code with no LLM turn."""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    await _set_user(store.db_path, user.id, credit_balance=100)
    await _set_team(store.db_path, team.id, team_credit_balance=0)
    orch = _FakeOrch(tokens_input=100, tokens_output=100)
    _outcome = await run_scheduled_turn(_app(orch, _resolver(store), user_store=store), job)
    tid, err = _outcome.turn_id, _outcome.error
    assert tid is None and err == "team_pool_exhausted"
    assert orch.called is False


@pytest.mark.parametrize(("tokens_output", "cost"), [(500, 2), (501, 3)], ids=["exact-thousand", "round-up"])
async def test_credit_deducted_by_tokens(billing_stores, tokens_output, cost):
    """A persisted v2 Turn bills projected residual usage against both balances."""
    store, sessions = billing_stores
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    await _set_user(store.db_path, user.id, credit_balance=1000)
    await _set_team(store.db_path, team.id, team_credit_balance=1000)
    orch = _FakeOrch(tokens_input=1500, tokens_output=tokens_output, session_store=_FakeSessionStore(sessions))
    _outcome = await run_scheduled_turn(_app(orch, _resolver(store), user_store=store), job)
    _tid, err = _outcome.turn_id, _outcome.error
    assert err is None and orch.called is True
    assert _tid is not None
    turn = await sessions.get_turn(_tid)
    assert turn is not None and turn.session_id == orch.request.session_id
    assert await _user_balance(store.db_path, user.id) == 1000 - cost
    billed_team = await store.get_team(team.id)
    assert billed_team is not None and billed_team.team_credit_balance == 1000 - cost
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute(
            "SELECT user_id,team_id,workspace_id,turn_id,delta,reason,tokens_input,tokens_output,rate_card_version "
            "FROM credit_ledger WHERE turn_id=?",
            (_tid,),
        ) as cursor:
            assert await cursor.fetchall() == [(user.id, team.id, job.workspace_id, _tid, -cost, "turn", 1500, tokens_output, "v2")]
        async with db.execute(
            "SELECT m.provider,m.model,m.tokens_input,m.tokens_cached,m.tokens_output "
            "FROM credit_model_usage m JOIN credit_ledger l ON l.id=m.ledger_id WHERE l.turn_id=?",
            (_tid,),
        ) as cursor:
            assert await cursor.fetchall() == [(None, None, 1500, 0, tokens_output)]


# ── cadence guardrail (Routines Phase A) ────────────────────────────────────────


def test_validate_cron_rejects_subhourly() -> None:
    import pytest as _pytest
    from bowhead_gateway.routes.scheduled import _validate_cron
    from fastapi import HTTPException

    # hourly-or-slower is allowed
    _validate_cron("0 9 * * *")  # daily
    _validate_cron("30 * * * *")  # hourly at :30
    _validate_cron("0 10 * * 1")  # weekly
    _validate_cron("*/60 * * * *")  # exactly hourly
    _validate_cron("30 8 * * 1-5")  # Workdays (weekday range).
    _validate_cron("@manual")  # manual routine — no schedule

    for bad in ("* * * * *", "*/5 * * * *", "*/59 * * * *"):
        with _pytest.raises(HTTPException) as ei:
            _validate_cron(bad)
        assert ei.value.status_code == 400

    with _pytest.raises(HTTPException):  # malformed (not 5 fields) still rejected
        _validate_cron("bad cron")

    # calendar patterns (day-of-month / specific month / month-end) are schedulable since the
    # cronsim migration — hourly-or-slower, so the frequency policy admits them.
    _validate_cron("0 9 1 * *")  # monthly
    _validate_cron("0 9 * 6 *")  # specific month
    _validate_cron("0 23 L * *")  # last day of month

    # 5-field but never fires → still rejected (job would sit with next_run_at NULL forever)
    with _pytest.raises(HTTPException) as ei:
        _validate_cron("0 0 31 2 *")
    assert ei.value.status_code == 400


# ── Phase B delivery (summary + deep link) ──────────────────────────────────────


def test_run_summary_helper() -> None:
    from bowhead_gateway._scheduled_runner import _run_summary

    assert _run_summary(SimpleNamespace(final_answer="GMV +8%"), "J", "zh-CN") == "GMV +8%"
    assert _run_summary(SimpleNamespace(final_answer=""), "日报", "zh-CN") == "日报 已完成"  # empty → fallback
    assert _run_summary(SimpleNamespace(final_answer=""), "Daily", "en-US") == "Daily completed"
    assert _run_summary(SimpleNamespace(final_answer="x" * 300), "J", "en-US").endswith("…")  # truncated


def test_scheduled_summary_is_plain_text_without_markers_or_markdown() -> None:
    from bowhead_gateway._scheduled_runner import scheduled_summary_plain_text

    answer = (
        "<response_kind>chat</response_kind>\n\n<headline>GMV 增长</headline>\n\n"
        "## 结论\n\n**昨日 GMV 1.2 亿**[cite:1]，环比 *+8%*[cite:2]。\n\n"
        "- 华东 `+12%`\n- 详见 [看板](https://x.example/d)\n\n"
        "| 区域 | 增速 |\n|---|---|\n| 华东 | 12% |\n\n[data:art_1]"
    )
    text = scheduled_summary_plain_text(answer)
    assert text == "结论 昨日 GMV 1.2 亿，环比 +8%。 华东 +12% 详见 看板 区域 · 增速 华东 · 12%"
    for leaked in ("<", "[cite", "[1]", "**", "##", "`", "](", "|", "---"):
        assert leaked not in text
    long_text = scheduled_summary_plain_text("**" + "字" * 300 + "**")
    assert len(long_text) == 241 and long_text.endswith("…")
    assert scheduled_summary_plain_text("<headline>只有标题</headline>[cite:1]") == ""


@pytest.mark.asyncio
async def test_deep_link_helper() -> None:
    from bowhead_gateway._scheduled_runner import _deep_link

    app = SimpleNamespace(state=SimpleNamespace())
    job = SimpleNamespace(public_entry_id=None)
    assert await _deep_link(app, job, "s1", None) is None  # No public entry → degrade.


@pytest.mark.asyncio
async def test_deep_link_ignores_entry_from_another_team(monkeypatch) -> None:
    from bowhead_gateway._scheduled_runner import _deep_link

    wrong = SimpleNamespace(
        id="pe_wrong",
        team_id="team_b",
        status="active",
        base_url="https://wrong.example",
    )
    default = SimpleNamespace(
        id="pe_default",
        team_id="team_a",
        status="active",
        base_url="https://right.example/bowhead",
    )

    class _Entries:
        async def get(self, entry_id):
            assert entry_id == wrong.id
            return wrong

        async def default_for_team(self, team_id):
            assert team_id == "team_a"
            return default

    app = SimpleNamespace(state=SimpleNamespace(public_entry_store=_Entries()))
    job = SimpleNamespace(public_entry_id=wrong.id)

    assert await _deep_link(app, job, "s1", "team_a") is None


@pytest.mark.asyncio
async def test_deep_link_does_not_fall_back_when_team_has_no_active_entry() -> None:
    from bowhead_gateway._scheduled_runner import _deep_link

    class _Entries:
        async def get(self, _entry_id):
            return None

        async def default_for_team(self, _team_id):
            return None

    app = SimpleNamespace(state=SimpleNamespace(public_entry_store=_Entries()))
    job = SimpleNamespace(public_entry_id=None)

    assert await _deep_link(app, job, "s1", "team_a") is None


async def test_phase_b_delivery_emits_routine_delivered(store):
    """On success with delivery opted-in, the runner emits routine.delivered with the run's summary."""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    job.notify_channels = {"deliver_on_success": True}  # opt-in, no email → skip SMTP

    class _Notif:
        def __init__(self) -> None:
            self.calls: list = []

        async def emit_routine_delivered(self, j, *, run_id, session_id, summary):
            self.calls.append((j.id, summary))

    notif = _Notif()
    orch = _FakeOrch(final_answer="昨日 GMV 1.2 亿, 环比 +8%")
    _outcome = await run_scheduled_turn(
        _app(orch, _resolver(store), user_store=store, notification_service=notif),
        job,
    )
    tid, err = _outcome.turn_id, _outcome.error
    assert err is None and tid is not None
    assert notif.calls == [(job.id, "昨日 GMV 1.2 亿, 环比 +8%")]


async def test_phase_b_no_delivery_when_not_opted_in(store):
    """No notify_channels → silent success (no routine.delivered, low-noise §5.5)."""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)  # notify_channels == {}

    class _Notif:
        def __init__(self) -> None:
            self.calls: list = []

        async def emit_routine_delivered(self, *a, **k):
            self.calls.append(1)

    notif = _Notif()
    _outcome = await run_scheduled_turn(
        _app(_FakeOrch(final_answer="x"), _resolver(store), user_store=store, notification_service=notif),
        job,
    )
    _tid, err = _outcome.turn_id, _outcome.error
    assert err is None and notif.calls == []


async def test_feature_unlicensed_skipped_no_run(store):
    """scheduling feature gate (§5): team health OK but scheduling not licensed → entitlement skip on
    EVERY execution path (covers the conversational tool that skips the route's require_feature_licensed)."""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")

    class _Ent:
        async def resolve_team_for_user(self, _uid):
            return team.id

        async def assert_can_run_turn(self, _team_id):
            return None

        async def is_feature_licensed(self, _team_id, feature):
            return feature != "scheduling"  # scheduling specifically unlicensed

    orch = _FakeOrch()
    _outcome = await run_scheduled_turn(_app(orch, _Ent()), _job(user.id))
    tid, err = _outcome.turn_id, _outcome.error
    assert tid is None and err == "feature_unlicensed"
    assert orch.called is False  # zero LLM cost


async def test_delivery_allowlist_drops_non_member_email(store, monkeypatch):
    """Recipient allowlist: deliver only to team-member addresses; an external address is dropped
    (exfil guard), team members (incl. owner) are delivered."""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="owner@a.com", password="Passw0rd!x")
    await store.create_user(team_id=team.id, email="teammate@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    job.notify_channels = {
        "email": ["teammate@a.com", "attacker@evil.com", "owner@a.com"],
        "deliver_on_success": True,
    }
    sent: list[str] = []

    async def _fake_send(addr, _subject, _body, *, team_id=None, store=None):
        sent.append(addr)
        return True, None  # ⚠ Real signature is (ok, error); bare True fake is more permissive.

    monkeypatch.setattr("bowhead_core.smtp_notifier.send_notification", _fake_send)

    class _Notif:
        async def emit_routine_delivered(self, *a, **k):
            return None

    _outcome = await run_scheduled_turn(
        _app(_FakeOrch(final_answer="x"), _resolver(store), user_store=store, notification_service=_Notif()),
        job,
    )
    tid, err = _outcome.turn_id, _outcome.error
    assert err is None and tid is not None
    assert set(sent) == {"owner@a.com", "teammate@a.com"}  # members delivered
    assert "attacker@evil.com" not in sent  # external recipient dropped


async def test_delivery_email_and_in_app_share_the_plain_text_summary(store, monkeypatch):
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="owner@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    job.notify_channels = {"email": ["owner@a.com"], "deliver_on_success": True}
    bodies: list[str] = []

    async def _fake_send(_addr, _subject, body, *, team_id=None, store=None):
        bodies.append(body)
        return True, None

    monkeypatch.setattr("bowhead_core.smtp_notifier.send_notification", _fake_send)
    summaries: list[str] = []

    class _Notif:
        async def emit_routine_delivered(self, _job, *, run_id, session_id, summary):
            summaries.append(summary)

    answer = "<response_kind>chat</response_kind>\n\n**昨日 GMV 1.2 亿**[cite:1]，环比 +8%。"
    _outcome = await run_scheduled_turn(
        _app(_FakeOrch(final_answer=answer), _resolver(store), user_store=store, notification_service=_Notif()),
        job,
    )
    assert _outcome.error is None
    assert summaries == ["昨日 GMV 1.2 亿，环比 +8%。"]
    assert len(bodies) == 1 and "昨日 GMV 1.2 亿，环比 +8%。" in bodies[0]
    assert "**" not in bodies[0] and "cite" not in bodies[0] and "response_kind" not in bodies[0]


async def test_email_failure_is_logged_not_silent(store, monkeypatch, caplog):
    """邮件发不出去必须留痕（116 dogfood 2026-08-06 逮到的静默失败）。

    `send_notification` 返 `(ok, error)` 而**不抛**；调用方原本丢弃返回值，而投递路径成败
    两条路都不打日志 ⇒ run 照记 success、in-app 照发、日志一片干净，用户以为收到了邮件。
    """
    import logging

    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="owner@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    job.notify_channels = {"email": ["owner@a.com"], "deliver_on_success": True}

    async def _failing_send(_addr, _subject, _body, *, team_id=None, store=None):
        return False, "smtp connect timeout"

    monkeypatch.setattr("bowhead_core.smtp_notifier.send_notification", _failing_send)

    class _Notif:
        def __init__(self) -> None:
            self.calls: list = []

        async def emit_routine_delivered(self, *a, **k):
            self.calls.append(1)

    notif = _Notif()
    with caplog.at_level(logging.WARNING, logger="bowhead_gateway._scheduled_runner"):
        outcome = await run_scheduled_turn(
            _app(_FakeOrch(final_answer="x"), _resolver(store), user_store=store, notification_service=notif),
            job,
        )

    assert outcome.error is None  # Email failure must not fail the whole turn; execution succeeded.
    assert notif.calls == [1]  # In-app delivery proceeds (email is only one channel).
    failed_logs = [r.message for r in caplog.records if "delivery_email_failed" in r.getMessage()]
    assert failed_logs, "邮件发失败却零日志 —— 静默失败回归了"
    assert "smtp connect timeout" in caplog.text  # Cause must be discoverable.


async def test_non_completed_status_is_failure_no_delivery(store):
    """A non-COMPLETED turn (e.g. clarification_pending — can't finish unattended) → returns an error so
    the scheduler classifies it failed, and does NOT deliver a non-result as a result."""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    job.notify_channels = {"deliver_on_success": True}

    class _Notif:
        def __init__(self) -> None:
            self.calls: list = []

        async def emit_routine_delivered(self, *a, **k):
            self.calls.append(1)

    notif = _Notif()
    orch = _FakeOrch(final_answer="请问指哪个指标?", status="clarification_pending")
    _outcome = await run_scheduled_turn(
        _app(orch, _resolver(store), user_store=store, notification_service=notif),
        job,
    )
    tid, err = _outcome.turn_id, _outcome.error
    assert tid is None and err == "scheduled_turn_requires_input"
    assert notif.calls == []  # not delivered


async def test_scheduled_turn_releases_active_turn_marker(store):
    """empty-canvas follow-up（真机逮到的 P1）：`record_turn` 在 store 层认领
    `sessions.active_turn_id`（所有通道），释放由共享 steer materializer 收口 ——
    定时任务直接调 orchestrator.run_turn，跑完必须自己释放。

    不释放的后果不是「看着脏」：steer_or_proceed 读到非空指针就把用户后续消息 buffer
    进那个早已结束的 turn（chat.py 直接返回 "buffered" 不 spawn）⇒ 消息静默消失；
    该会话也永远无法归档（archive_session 判非空即返 "active"）。
    """
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="rel@a.com", password="Passw0rd!x")
    orch = _FakeOrch()
    _outcome = await run_scheduled_turn(_app(orch, _resolver(store)), _job(user.id))
    tid, err = _outcome.turn_id, _outcome.error
    assert err is None
    assert len(orch.session_store.materialized) == 1
    session_id, current_turn_id, next_turn_id = orch.session_store.materialized[0]
    assert session_id == orch.request.session_id
    assert current_turn_id == tid
    assert next_turn_id and next_turn_id != current_turn_id
    # Returning [] means empty buffer did not materialize a child; preallocated next ID does not change this.


async def test_scheduled_turn_releases_marker_even_when_turn_raises(store):
    """异常路径同样要释放——否则一次崩溃就永久卡住该会话。"""

    class _BoomOrch(_FakeOrch):
        async def run_turn(self, tr):
            self.request = tr
            raise RuntimeError("boom")

    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="boom@a.com", password="Passw0rd!x")
    orch = _BoomOrch()
    _outcome = await run_scheduled_turn(_app(orch, _resolver(store)), _job(user.id))
    tid, err = _outcome.turn_id, _outcome.error
    assert tid is None and err == "scheduled_turn_failed"
    assert len(orch.session_store.materialized) == 1


# ─── scheduled-recipe-runs S1: bound solidified workflow ─────


class _RecordingSchedStore:
    """记录 patch 调用，验「解析失败 → 暂停 + 稳定码」而不是每 tick 重试刷通知。"""

    def __init__(self) -> None:
        self.patched: list[dict] = []

    async def patch(self, job_id: str, **fields):
        self.patched.append({"job_id": job_id, **fields})
        return True


class _RecordingNotifier:
    def __init__(self) -> None:
        self.paused: list[str] = []

    async def emit_scheduled_paused(self, job, *, reason: str = "repeated_failures"):
        self.paused.append(reason)
        return


def _bound_job(user_id, workspace_id, ref):
    job = _job(user_id, workspace_id=workspace_id)
    job.recipe_ref = ref
    return job


def _write_personal_skill(tmp_path, name: str, body: str = "1. 取数\n2. 出报告\n"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 周报流程\nversion: 1.0.0\n---\n\n{body}",
        encoding="utf-8",
    )
    return d


class _PersonalSkillStore:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, *, user_id: str, name: str):
        return self._rows.get((user_id, name))


async def test_bound_workflow_injected_into_turn_request(store, tmp_path):
    """绑定成功 → 内容随 TurnRequest 进 turn（服务端预读，不让 Lead 自己按名再读一次）。"""
    import json

    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    skill_dir = _write_personal_skill(tmp_path, "weekly-report")
    job = _bound_job(user.id, job.workspace_id, json.dumps({"form": "personal_skill", "id_or_name": "weekly-report"}))
    orch = _FakeOrch(final_answer="done")

    _outcome = await run_scheduled_turn(
        _app(
            orch,
            _resolver(store),
            user_store=store,
            skill_instance_store=_PersonalSkillStore(
                {(user.id, "weekly-report"): SimpleNamespace(source_path=str(skill_dir), description="周报", enabled=True)}
            ),
        ),
        job,
    )
    tid, err = _outcome.turn_id, _outcome.error

    assert err is None and tid is not None
    bw = getattr(orch.request, "bound_workflow", None)
    assert bw is not None, "解析出的工作流必须挂上 TurnRequest"
    assert "出报告" in (bw.body_md or "")


async def test_bound_workflow_lost_pauses_job_without_running(store, tmp_path):
    """流程被删/失权 = 永久性条件 → 暂停 + skipped 码 + 零 LLM（不每 tick 重跑重失败）。"""
    import json

    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    job = _bound_job(user.id, job.workspace_id, json.dumps({"form": "personal_skill", "id_or_name": "gone"}))
    orch = _FakeOrch()
    sched = _RecordingSchedStore()
    notif = _RecordingNotifier()

    _outcome = await run_scheduled_turn(
        _app(
            orch,
            _resolver(store),
            user_store=store,
            skill_instance_store=_PersonalSkillStore({}),
            scheduled_store=sched,
            notification_service=notif,
        ),
        job,
    )
    tid, err = _outcome.turn_id, _outcome.error

    assert tid is None and err == "scheduled_recipe_lost"
    assert orch.called is False, "注定失败的 run 不该烧 LLM"
    assert sched.patched and sched.patched[0]["status"] == "paused"
    assert sched.patched[0]["pause_reason"] == "scheduled_recipe_lost"
    assert notif.paused == ["scheduled_recipe_lost"]


async def test_unbound_job_unchanged(store):
    """没绑流程的 routine 行为字节级不变（本 sprint 对存量任务零影响）。"""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    orch = _FakeOrch(final_answer="ok")

    _outcome = await run_scheduled_turn(_app(orch, _resolver(store), user_store=store), job)
    tid, err = _outcome.turn_id, _outcome.error

    assert err is None and tid is not None
    assert getattr(orch.request, "bound_workflow", None) is None


async def test_pause_reason_accepted_by_real_store(tmp_path):
    """★真 store 守卫：上面的暂停用例用的是 fake store（接受任意 kwargs）。

    真 ``ScheduledJobStore.patch`` 若不认 ``pause_reason``，runner 里的 ``except Exception``
    会把 TypeError 吞掉 → **job 永不 paused、每 tick 重跑重失败刷通知**。这与
    「方法名是 patch 不是 update」那次事故是同一形状，故必须由真实现来证伪。
    """
    from bowhead_core.scheduled_store import ScheduledJobStore

    real = ScheduledJobStore(str(tmp_path / "sched.db"))
    await real.initialize()
    job = await real.create(user_id="u1", workspace_id="ws1", name="j", cron="0 9 * * *", query="q")

    ok = await real.patch(job.id, status="paused", pause_reason="scheduled_recipe_lost")

    assert ok is True
    reloaded = await real.get(job.id)
    assert reloaded is not None
    assert reloaded.status == "paused"
    assert reloaded.pause_reason == "scheduled_recipe_lost"


# ─── S3: suspension criteria ────────────────────────────────


class _ClarificationStore:
    def __init__(self, rows_by_turn: dict[str, list[object]] | None = None, *, boom: bool = False) -> None:
        self._rows = rows_by_turn or {}
        self._boom = boom

    async def get_pending_for_turn(self, turn_id: str):
        if self._boom:
            raise RuntimeError("store down")
        # Real implementation queries turn_id; here any turn gets configured rows (runner generates random turn_id).
        return next(iter(self._rows.values()), []) if self._rows else []


async def test_clarification_pending_with_open_row_suspends(store):
    """跑到歧义点且**真有**一条待回答的行 → 挂起等人，不记失败。"""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    orch = _FakeOrch(status="clarification_pending")

    outcome = await run_scheduled_turn(
        _app(orch, _resolver(store), user_store=store, clarification_store=_ClarificationStore({"any": [object()]})),
        job,
    )

    assert outcome.awaiting is True
    assert outcome.turn_id is not None, "挂起必须带 turn_id，否则 reconciliation 找不回它在等哪个 turn"
    assert outcome.session_id is not None, "通知深链要靠它"
    assert outcome.error is None


async def test_plan_proposal_shape_does_not_suspend(store):
    """★plan-proposal 复用了 clarification_pending 这个 status 但**不建 clarification 行**。

    只看 status 会挂起一个无卡可答的 turn：通知发出去、用户点进来一片空白、到点再记超时。
    """
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    orch = _FakeOrch(status="clarification_pending")

    outcome = await run_scheduled_turn(
        _app(orch, _resolver(store), user_store=store, clarification_store=_ClarificationStore({})),
        job,
    )

    assert outcome.awaiting is False
    assert outcome.error == "scheduled_turn_requires_input"


async def test_clarification_probe_failure_falls_back_to_failed(store):
    """探测不了就退回老语义：宁可让 owner 看到一次失败，也别把 run 挂在可能无卡可答的状态上
    （那会占着在飞位直到下次 fire）。"""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    orch = _FakeOrch(status="clarification_pending")

    outcome = await run_scheduled_turn(
        _app(orch, _resolver(store), user_store=store, clarification_store=_ClarificationStore(boom=True)),
        job,
    )

    assert outcome.awaiting is False
    assert outcome.error == "scheduled_turn_requires_input"


async def test_awaiting_approval_still_fails(store):
    """审批挂起本期不做（Q4）：审批人可能不是任务 owner，通知路由与超时语义都要另设计。"""
    team = await store.create_team(name="A")
    user = await store.create_user(team_id=team.id, email="u@a.com", password="Passw0rd!x")
    job = await _member_job(store, team, user)
    orch = _FakeOrch(status="awaiting_approval")

    outcome = await run_scheduled_turn(
        _app(orch, _resolver(store), user_store=store, clarification_store=_ClarificationStore({"any": [object()]})),
        job,
    )

    assert outcome.awaiting is False
    assert outcome.error == "scheduled_turn_requires_approval"


@pytest.mark.parametrize("failure", ["missing", "failed"])
async def test_community_scheduler_denies_missing_or_failed_availability(failure):
    class FailedResolver:
        async def resolve_team_for_user(self, _user_id):
            raise RuntimeError("Simulated unavailable store")

    orch = _FakeOrch()
    resolver = None if failure == "missing" else FailedResolver()
    outcome = await run_scheduled_turn(_app(orch, resolver, edition="community"), _job("usr_owner"))
    assert outcome.error == ("authz_unavailable" if failure == "missing" else "authz_lookup_failed")
    assert orch.called is False
