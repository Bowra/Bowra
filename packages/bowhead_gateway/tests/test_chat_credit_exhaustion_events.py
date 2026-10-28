from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.events import EventBusRegistry
from bowhead_core.rate_card import RATE_CARD_VERSION
from bowhead_core.session_store import SessionStore
from bowhead_core.terminal_effect_contract import (
    EFFECT_CONTRACT_VERSION,
    resolve_effect_execution_contract,
)
from bowhead_core.terminal_protocol import (
    TerminalProtocolFutureVersionError,
    TerminalProtocolMalformedError,
    TerminalScope,
    TerminalStatus,
)
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.turn_service import admit_legacy_chat_execution
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _instance_encryption_root(monkeypatch):
    """Deployments always set BOWHEAD_FERNET_KEY; since 9c6fe1654 every chat worker turn builds the
    physical-usage receipt cipher from it. Without .env in the checkout nothing else provides it."""
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")


class _CreditEventStore:
    def __init__(self, *, user_balance: int = 0, team_balance: int = 1_000) -> None:
        self.user = SimpleNamespace(
            id="u_1",
            team_id="team_a",
            credit_balance=user_balance,
            credit_quota=10_000,
        )
        self.team = SimpleNamespace(
            id="team_a",
            team_credit_balance=team_balance,
            team_credit_quota=100_000,
        )
        self.events: list[dict] = []

    async def get_user(self, user_id: str):
        return self.user

    async def get_team(self, team_id: str):
        return self.team

    async def record_credit_exhaustion_event(self, **kwargs):
        self.events.append(kwargs)
        return {"id": "cex_test", **kwargs}


class _MetricsLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def track(self, name: str, *, user_id=None, attrs=None) -> None:
        self.events.append({"name": name, "user_id": user_id, "attrs": attrs or {}})


async def _prepare_worker(orchestrator: Any, req: Any, tmp_path: Any) -> Any:
    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}", target="core") == ("core",)
    session_store = SessionStore(core_path)
    await session_store.initialize()
    await session_store.ensure_session(
        session_id=req.session_id,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
    )
    await session_store.record_turn(
        turn_id=req.turn_id,
        session_id=req.session_id,
        user_text=req.text,
    )
    turn_log_store = TurnLogStore(str(core_path))
    await turn_log_store.initialize()
    orchestrator.session_store = session_store
    orchestrator.event_registry = EventBusRegistry(
        default_log_sink=OrderedTurnLogSink(turn_log_store),
    )
    return await admit_legacy_chat_execution(orchestrator, req)


def _app(store: _CreditEventStore, metrics_log: _MetricsLog | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = SimpleNamespace(dev_default_user="dev_user", auth_mode="prod")
    app.state.user_store = store
    app.state.metrics_log = metrics_log
    app.state.orchestrator = SimpleNamespace()
    app.include_router(chat_routes.router, prefix="/api/v1/chat")
    return app


def test_send_records_user_credit_exhaustion_before_402(monkeypatch) -> None:
    monkeypatch.delenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", raising=False)

    async def _stub_resolve(_request):
        return ("u_1", "team_a")

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", _stub_resolve)
    store = _CreditEventStore(user_balance=0, team_balance=1_000)
    metrics_log = _MetricsLog()
    client = TestClient(_app(store, metrics_log=metrics_log))

    r = client.post("/api/v1/chat/send", json={"workspace_id": "ws_1", "message": "hi"})

    assert r.status_code == 402, r.text
    assert store.events
    assert store.events[0]["code"] == "user_credit_exhausted"
    assert store.events[0]["credit_balance"] == 0
    assert store.events[0]["team_credit_balance"] == 1_000
    assert metrics_log.events
    assert metrics_log.events[0]["name"] == "credit.exhausted_block"
    assert metrics_log.events[0]["user_id"] == "u_1"
    assert metrics_log.events[0]["attrs"]["code"] == "user_credit_exhausted"
    assert metrics_log.events[0]["attrs"]["credit_exhaustion_event_id"] == "cex_test"


@pytest.mark.asyncio
async def test_run_turn_records_postflight_deduct_failure(tmp_path: Any) -> None:
    class _Orchestrator:
        async def run_turn(self, req):
            assert await self.session_store.mark_completed(
                turn_id=req.turn_id,
                intent="lookup",
                final_answer="ok",
                tokens_input=1_500,
                tokens_output=500,
                cost_usd=0.0,
            )
            return SimpleNamespace(
                status="completed",
                tokens_input=1_500,
                tokens_output=500,
            )

    class _Store(_CreditEventStore):
        async def try_deduct_credit(self, **kwargs):
            return False, "team_pool_zero"

    req = SimpleNamespace(
        user_id="u_1",
        workspace_id="ws_1",
        turn_id="t_1",
        session_id="s_1",
        text="hi",
        execution_generation=1,
        plan_execution_id=None,
    )
    store = _Store(user_balance=100, team_balance=0)
    metrics_log = _MetricsLog()

    orchestrator = _Orchestrator()
    admitted_bus = await _prepare_worker(orchestrator, req, tmp_path)
    await chat_routes._run_turn_and_cleanup(
        orchestrator,
        req,
        user_store=store,
        team_id="team_a",
        metrics_log=metrics_log,
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )

    assert store.events
    assert store.events[0]["code"] == "credit_deduct_failed"
    assert store.events[0]["turn_id"] == "t_1"
    assert [event["name"] for event in metrics_log.events] == [
        "credit.exhausted_block",
        "credit.deduct_failed",
    ]
    exhausted, deduct_failed = metrics_log.events
    assert exhausted["attrs"]["code"] == "credit_deduct_failed"
    assert exhausted["attrs"]["turn_id"] == "t_1"
    assert deduct_failed["attrs"]["deduct_reason"] == "team_pool_zero"
    assert deduct_failed["attrs"]["cost_credits"] == 2
    assert deduct_failed["attrs"]["credit_exhaustion_event_id"] == "cex_test"


@pytest.mark.asyncio
async def test_cancelled_turn_deducts_partial_usage_once(tmp_path: Any) -> None:
    class _Orchestrator:
        async def run_turn(self, req):
            assert await self.session_store.mark_cancelled(
                turn_id=req.turn_id,
                tokens_input=750,
                tokens_output=250,
            )
            return SimpleNamespace(
                status="cancelled",
                tokens_input=750,
                tokens_output=250,
            )

    class _Store(_CreditEventStore):
        def __init__(self) -> None:
            super().__init__(user_balance=100, team_balance=100)
            self.deductions: list[dict] = []

        async def try_deduct_credit(self, **kwargs):
            self.deductions.append(kwargs)
            return True, "ok"

    req = SimpleNamespace(
        user_id="u_1",
        workspace_id="ws_1",
        turn_id="t_cancelled_partial",
        session_id="s_cancelled_partial",
        text="hi",
        execution_generation=1,
        plan_execution_id=None,
    )
    store = _Store()

    orchestrator = _Orchestrator()
    admitted_bus = await _prepare_worker(orchestrator, req, tmp_path)
    await chat_routes._run_turn_and_cleanup(
        orchestrator,
        req,
        user_store=store,
        team_id="team_a",
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )

    assert len(store.deductions) == 1
    assert store.deductions[0]["turn_id"] == "t_cancelled_partial"
    assert store.deductions[0]["tokens_input"] == 750
    assert store.deductions[0]["tokens_output"] == 250
    assert store.deductions[0]["cost_credits"] == 1


@pytest.mark.asyncio
async def test_turn_that_outspends_balance_blocks_next_send(tmp_path: Any, monkeypatch) -> None:
    """余额 100、本轮花 150 ⇒ 照实扣到 -50 ⇒ 下一次 send 402。

    旧实现余额不足整笔拒扣：余额停在 100，preflight 的 `<= 0` 永远放行，此后每轮白跑。
    """
    from bowhead_core.user_store import UserStore, _now

    user_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{user_path}", target="core") == ("core",)
    user_store = UserStore(str(user_path))
    await user_store.initialize()
    async with aiosqlite.connect(user_store.db_path) as db:
        await db.execute(
            "INSERT INTO teams(id,name,domain,max_seats,default_user_tier,"
            "team_credit_balance,team_credit_quota,tier_credits_json,status,created_at) "
            "VALUES ('team_a','Test Team','test.com',10,'pro',1000,1000,'{}','active',?)",
            (_now(),),
        )
        await db.execute(
            "INSERT INTO users(id,team_id,email,email_verified,password_hash,"
            "oauth_providers_json,tier,credit_balance,credit_quota,credit_period_start,"
            "locale,timezone,preferences_json,status,created_at) "
            "VALUES ('u_1','team_a','a@test.com',1,'','[]','pro',100,1000,?,"
            "'zh-CN','UTC','{}','active',?)",
            (_now(), _now()),
        )
        await db.commit()

    class _Orchestrator:
        async def run_turn(self, req):
            assert await self.session_store.mark_completed(
                turn_id=req.turn_id,
                intent="lookup",
                final_answer="ok",
                tokens_input=120_000,
                tokens_output=30_000,
                cost_usd=0.0,
            )
            return SimpleNamespace(
                status="completed",
                tokens_input=120_000,
                tokens_output=30_000,
            )

    req = SimpleNamespace(
        user_id="u_1",
        workspace_id="ws_1",
        turn_id="t_overdraw",
        session_id="s_overdraw",
        text="hi",
        execution_generation=1,
        plan_execution_id=None,
    )
    orchestrator = _Orchestrator()
    admitted_bus = await _prepare_worker(orchestrator, req, tmp_path)
    await chat_routes._run_turn_and_cleanup(
        orchestrator,
        req,
        user_store=user_store,
        team_id="team_a",
        admitted_legacy_bus=admitted_bus,
        cleanup_grace_s=0,
    )

    user = await user_store.get_user("u_1")
    team = await user_store.get_team("team_a")
    assert (user.credit_balance, team.team_credit_balance) == (-50, 850)

    monkeypatch.delenv("BOWHEAD_WS_MEMBERSHIP_ENFORCE", raising=False)

    async def _stub_resolve(_request):
        return ("u_1", "team_a")

    monkeypatch.setattr(auth_routes, "resolve_user_or_stub", _stub_resolve)
    client = TestClient(_app(user_store))  # type: ignore[arg-type]

    r = client.post("/api/v1/chat/send", json={"workspace_id": "ws_1", "message": "hi again"})

    assert r.status_code == 402, r.text
    assert r.json()["detail"]["error"]["code"] == "user_credit_exhausted"


@pytest.mark.asyncio
async def test_terminal_cancel_billing_uses_frozen_durable_usage() -> None:
    class _SessionStore:
        async def get_turn(self, _turn_id: str):
            return SimpleNamespace(
                status="cancelled",
                tokens_input=400,
                tokens_output=100,
            )

    orchestrator = SimpleNamespace(session_store=_SessionStore())
    terminal_runtime = SimpleNamespace(
        bus=SimpleNamespace(
            receipt=SimpleNamespace(status=TerminalStatus.CANCELLED),
        ),
    )
    late_provider_result = SimpleNamespace(
        tokens_input=4_000,
        tokens_output=1_000,
    )

    usage = await chat_routes._billable_turn_usage(
        orchestrator,
        terminal_runtime,
        late_provider_result,
        turn_id="t_cancelled_terminal",
    )

    assert usage == (400, 100)


@pytest.mark.asyncio
async def test_terminal_cancel_billing_reads_real_turn_projection(tmp_path) -> None:
    session_store = SessionStore(tmp_path / "session.db")
    await session_store.initialize()
    await session_store.ensure_session(
        session_id="s_cancelled_terminal",
        workspace_id="ws_1",
        user_id="u_1",
    )
    await session_store.record_turn(
        turn_id="t_cancelled_terminal",
        session_id="s_cancelled_terminal",
        user_text="run",
    )
    await session_store.mark_cancelled(
        turn_id="t_cancelled_terminal",
        tokens_input=400,
        tokens_output=100,
    )
    orchestrator = SimpleNamespace(session_store=session_store)
    terminal_runtime = SimpleNamespace(
        bus=SimpleNamespace(
            receipt=SimpleNamespace(status=TerminalStatus.CANCELLED),
        ),
    )

    usage = await chat_routes._billable_turn_usage(
        orchestrator,
        terminal_runtime,
        SimpleNamespace(tokens_input=4_000, tokens_output=1_000),
        turn_id="t_cancelled_terminal",
    )

    persisted = await session_store.get_turn("t_cancelled_terminal")
    assert persisted is not None
    assert (persisted.tokens_input, persisted.tokens_output) == (400, 100)
    assert usage == (400, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contract_kind", "expected_deductions", "blocked_state"),
    [
        ("absent", 1, None),
        ("absent_n_plus_one", 1, None),
        ("v2", 0, None),
        ("malformed", 0, "malformed"),
        ("future", 0, "future"),
        ("unavailable", 0, "unavailable"),
        ("missing_reader", 0, "unavailable"),
        ("invalid_contract", 0, "malformed"),
        ("runtime_contract_mismatch", 0, "malformed"),
        ("scope_mismatch", 0, "malformed"),
    ],
)
async def test_terminal_postflight_billing_uses_persisted_contract_five_state_router(
    monkeypatch,
    contract_kind: str,
    expected_deductions: int,
    blocked_state: str | None,
) -> None:
    legacy_contract = resolve_effect_execution_contract({})
    v2_contract = resolve_effect_execution_contract(
        {
            "billing_rate_card_version": RATE_CARD_VERSION,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
        }
    )

    class _ContractStore:
        async def read_effect_execution_contract(self, _scope):
            if contract_kind == "malformed":
                raise TerminalProtocolMalformedError("bad persisted contract")
            if contract_kind == "future":
                raise TerminalProtocolFutureVersionError("future persisted contract")
            if contract_kind == "unavailable":
                raise OSError("database unavailable")
            if contract_kind == "invalid_contract":
                return self
            return v2_contract if contract_kind in {"v2", "runtime_contract_mismatch"} else legacy_contract

    class _Runtime:
        def __init__(self) -> None:
            generation = 2 if contract_kind in {"absent_n_plus_one", "scope_mismatch"} else 1
            self.scope = TerminalScope(
                team_id="team_a",
                session_id="s_contract",
                turn_id="t_contract",
                generation=generation,
            )
            self.store = object() if contract_kind == "missing_reader" else _ContractStore()
            if contract_kind == "invalid_contract":
                self.effect_contract = self.store
            else:
                self.effect_contract = v2_contract if contract_kind == "v2" else legacy_contract
            self.bus = SimpleNamespace(
                receipt=SimpleNamespace(status=TerminalStatus.COMPLETED),
            )

        async def start_execution(self) -> None:
            return None

        async def retire(self) -> None:
            return None

    class _Orchestrator:
        # Real orchestrators always carry a session store; the worker hands its db_path to the
        # physical-usage receipt observer (default on since 9c6fe1654). run_turn makes no model
        # call here, so the path is never opened.
        session_store = SimpleNamespace(db_path="/nonexistent/test-session-store.db")

        async def run_turn(self, _req):
            return SimpleNamespace(
                status="completed",
                tokens_input=1_500,
                tokens_output=500,
            )

    class _Store(_CreditEventStore):
        def __init__(self) -> None:
            super().__init__(user_balance=100, team_balance=100)
            self.deductions: list[dict] = []

        async def try_deduct_credit(self, **kwargs):
            self.deductions.append(kwargs)
            return True, "ok"

    async def _no_sleep(_seconds: float) -> None:
        return None

    async def _legacy_usage(*_args, **_kwargs):
        return "v1", ()

    monkeypatch.setattr(chat_routes.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("bowhead_core.physical_usage.read_turn_credit_usage", _legacy_usage)
    runtime = _Runtime()
    req = SimpleNamespace(
        user_id="u_1",
        workspace_id="ws_1",
        turn_id="t_contract",
        session_id="s_contract",
        execution_generation=(2 if contract_kind == "absent_n_plus_one" else 1),
        plan_execution_id=None,
        text="hi",
        terminal_runtime=runtime,
    )
    store = _Store()
    metrics = _MetricsLog()

    await chat_routes._run_turn_and_cleanup(
        _Orchestrator(),
        req,
        user_store=store,
        team_id="team_a",
        metrics_log=metrics,
        cleanup_grace_s=0,
    )

    assert len(store.deductions) == expected_deductions
    blocked = [event for event in metrics.events if event["name"] == "credit.postflight_contract_blocked"]
    assert [event["attrs"]["state"] for event in blocked] == ([] if blocked_state is None else [blocked_state])
