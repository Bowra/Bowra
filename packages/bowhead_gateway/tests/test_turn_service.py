"""Unit tests for TurnService — the extracted steer + spawn choreography.

``steer_or_proceed``: asserts the exact side-effect sequence the live chat.py steer block must
preserve — append True -> buffered; append False / empty text / no active turn -> proceed.

``spawn_turn``: asserts durable dispatch claim, worker arguments/task identity,
lease settlement, exact bus retirement and _inflight_tasks bookkeeping.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

import bowhead_gateway.routes.chat as chat_mod
import bowhead_gateway.turn_service as ts_mod
import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.events import TerminalDurabilityAbort
from bowhead_core.terminal_protocol import TerminalScope
from bowhead_gateway.turn_service import (
    SteerOutcome,
    TerminalProtocolSurfaceUnavailable,
    admit_legacy_chat_execution,
    build_resume_turn_request,
    require_terminal_surface_capability,
    steer_or_proceed,
    wait_for_legacy_turn_resume_ready,
)
from bowhead_gateway.turn_service import (
    spawn_turn as _real_spawn_turn,
)


def test_legacy_untracked_orphan_age_tracks_effective_lead_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_gateway.main import _legacy_untracked_orphan_min_age_minutes

    monkeypatch.delenv("BOWHEAD_LEAD_LOOP_HARD_TIMEOUT_SEC", raising=False)
    assert _legacy_untracked_orphan_min_age_minutes() == 60
    monkeypatch.setenv("BOWHEAD_LEAD_LOOP_HARD_TIMEOUT_SEC", "2400")
    assert _legacy_untracked_orphan_min_age_minutes() == 80
    monkeypatch.setenv("BOWHEAD_LEAD_LOOP_HARD_TIMEOUT_SEC", "inf")
    assert _legacy_untracked_orphan_min_age_minutes() is None


@pytest.mark.asyncio
async def test_legacy_resume_fence_fails_closed_without_protocol_probe() -> None:
    class StatusOnlyStore:
        async def get_turn(self, _turn_id: str) -> SimpleNamespace:
            return SimpleNamespace(status="clarification_pending")

    assert not await wait_for_legacy_turn_resume_ready(
        StatusOnlyStore(),
        "turn-1",
        timeout_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_legacy_resume_fence_fails_closed_when_protocol_probe_raises() -> None:
    class BrokenProbeStore:
        async def is_turn_resume_ready(self, _turn_id: str) -> bool:
            raise RuntimeError("store unavailable")

    assert not await wait_for_legacy_turn_resume_ready(
        BrokenProbeStore(),
        "turn-1",
        timeout_seconds=0.0,
    )


class FakeSessionStore:
    def __init__(
        self,
        *,
        active=None,
        active_protocol_version=None,
        append_result=True,
        raise_get=False,
        active_on_append=None,
    ) -> None:
        self._active = active
        self._active_protocol_version = active_protocol_version
        self._append_result = append_result
        self._raise_get = raise_get
        self._active_on_append = active_on_append
        self.append_calls: list[dict] = []
        self.buffer: list[str] = []

    async def get_active_turn(self, session_id: str):
        if self._raise_get:
            raise RuntimeError("boom")
        return self._active

    async def get_active_turn_identity(self, session_id: str):
        if self._raise_get:
            raise RuntimeError("boom")
        return self._active, self._active_protocol_version

    async def append_steer_buffer(self, *, session_id, text, expected_turn_id=None):
        self.append_calls.append(
            {
                "session_id": session_id,
                "text": text,
                "expected_turn_id": expected_turn_id,
            }
        )
        if self._active_on_append is not None:
            self._active = self._active_on_append
        appended = self._append_result and (expected_turn_id is None or expected_turn_id == self._active)
        if appended:
            self.buffer.append(text)
        return appended


class FakeOrchestrator:
    def __init__(self, store: FakeSessionStore) -> None:
        self.session_store = store
        self.cancelled: list[str] = []

    def signal_cancel(self, turn_id: str) -> None:
        self.cancelled.append(turn_id)


class _SurfaceUserStore:
    def __init__(self, *, team_id: str | None = "team_1", error=None) -> None:
        self.team_id = team_id
        self.error = error

    async def get_user_team_id(self, _user_id: str) -> str | None:
        if self.error is not None:
            raise self.error
        return self.team_id


class _SurfaceProtocolStore:
    def __init__(self, binding=("s_1", None), *, error=None) -> None:
        self.binding = binding
        self.error = error

    async def read_persisted_turn_protocol_binding(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.binding


def _surface_app(*, protocol_store, user_store):
    return SimpleNamespace(
        state=SimpleNamespace(
            terminal_protocol_store=protocol_store,
            user_store=user_store,
        )
    )


def _surface_clarification():
    return SimpleNamespace(
        turn_id="t_1",
        session_id="s_1",
        resume_context={},
    )


@pytest.mark.parametrize(
    ("protocol_store", "user_store"),
    [
        (None, _SurfaceUserStore()),
        (_SurfaceProtocolStore(), None),
        (
            _SurfaceProtocolStore(),
            _SurfaceUserStore(error=RuntimeError("team lookup failed")),
        ),
        (
            _SurfaceProtocolStore(error=RuntimeError("binding failed")),
            _SurfaceUserStore(),
        ),
        (_SurfaceProtocolStore(binding=("s_1", 2)), _SurfaceUserStore()),
        (_SurfaceProtocolStore(binding=("s_other", None)), _SurfaceUserStore()),
    ],
)
async def test_non_web_surface_guard_fails_closed_with_stable_error(
    protocol_store,
    user_store,
) -> None:
    with pytest.raises(
        TerminalProtocolSurfaceUnavailable,
        match="terminal_protocol_surface_unavailable",
    ):
        await require_terminal_surface_capability(
            _surface_app(
                protocol_store=protocol_store,
                user_store=user_store,
            ),
            _surface_clarification(),
            user_id="u_1",
            surface="feishu",
        )


async def test_non_web_surface_guard_allows_only_explicit_legacy_binding() -> None:
    await require_terminal_surface_capability(
        _surface_app(
            protocol_store=_SurfaceProtocolStore(binding=("s_1", None)),
            user_store=_SurfaceUserStore(),
        ),
        _surface_clarification(),
        user_id="u_1",
        surface="external_im",
    )


async def test_non_web_surface_guard_blocks_persisted_v1() -> None:
    with pytest.raises(TerminalProtocolSurfaceUnavailable):
        await require_terminal_surface_capability(
            _surface_app(
                protocol_store=_SurfaceProtocolStore(binding=("s_1", 1)),
                user_store=_SurfaceUserStore(),
            ),
            _surface_clarification(),
            user_id="u_1",
            surface="external_im",
        )


async def test_no_active_turn_proceeds() -> None:
    orch = FakeOrchestrator(FakeSessionStore(active=None))
    out = await steer_or_proceed(orch, session_id="s1", text="hello", user_msg_id="u1")
    assert out == SteerOutcome("proceed")


async def test_no_active_turn_with_attachments_proceeds_to_fresh_turn() -> None:
    orch = FakeOrchestrator(FakeSessionStore(active=None))

    out = await steer_or_proceed(
        orch,
        session_id="s1",
        text="hello",
        user_msg_id="u1",
        has_attachments=True,
    )

    assert out == SteerOutcome("proceed")
    assert orch.session_store.append_calls == []


async def test_active_legacy_turn_with_attachments_fails_before_buffer() -> None:
    orch = FakeOrchestrator(FakeSessionStore(active="t_active"))

    out = await steer_or_proceed(
        orch,
        session_id="s1",
        text="follow up",
        user_msg_id="u1",
        has_attachments=True,
    )

    assert out == SteerOutcome("attachments_unsupported", "t_active")
    assert orch.session_store.append_calls == []


async def test_empty_session_id_proceeds() -> None:
    orch = FakeOrchestrator(FakeSessionStore(active="t1"))
    out = await steer_or_proceed(orch, session_id="", text="hi", user_msg_id="u1")
    assert out.kind == "proceed"


async def test_protocol_identity_read_error_fails_closed() -> None:
    orch = FakeOrchestrator(FakeSessionStore(active="t1", raise_get=True))
    out = await steer_or_proceed(orch, session_id="s1", text="hi", user_msg_id="u1")
    assert out == SteerOutcome("lookup_failed")
    assert orch.session_store.append_calls == []
    assert orch.cancelled == []


async def test_normal_text_buffers_and_acks() -> None:
    registry = ts_mod.get_event_registry()
    await registry.close("t_remote_ack")
    store = FakeSessionStore(active="t_remote_ack", append_result=True)
    orch = FakeOrchestrator(store)
    out = await steer_or_proceed(orch, session_id="s1", text="follow up", user_msg_id="u1")
    assert out == SteerOutcome("buffered", "t_remote_ack")
    assert store.append_calls == [
        {
            "session_id": "s1",
            "text": "follow up",
            "expected_turn_id": "t_remote_ack",
        }
    ]
    assert orch.cancelled == []  # buffering never cancels
    producer_bus, _floor = await registry.projection_checkpoint("t_remote_ack")
    assert producer_bus is None


async def test_fatal_auxiliary_ack_does_not_reverse_committed_steer() -> None:
    registry = ts_mod.get_event_registry()
    await registry.close("t_fatal_ack")
    bus = await registry.get_or_create("t_fatal_ack")
    bus.latch_fatal_error(RuntimeError("durable sink failed"))
    store = FakeSessionStore(active="t_fatal_ack", append_result=True)
    orch = FakeOrchestrator(store)
    try:
        outcome = await steer_or_proceed(
            orch,
            session_id="s1",
            text="follow up once",
            user_msg_id="u1",
        )
    finally:
        await registry.close("t_fatal_ack", expected=bus)

    assert outcome == SteerOutcome("buffered", "t_fatal_ack")
    assert store.buffer == ["follow up once"]
    assert len(store.append_calls) == 1


def test_chat_send_has_no_text_meta_cancel_writer() -> None:
    import inspect

    from bowhead_core.session_store import SessionStore

    send_source = inspect.getsource(chat_mod._send_impl)
    steer_source = inspect.getsource(ts_mod.steer_or_proceed)
    submission_source = inspect.getsource(
        SessionStore.finalize_steer_chat_submission,
    )
    combined = "\n".join((send_source, steer_source, submission_source))

    for removed_symbol in (
        "is_meta_keyword",
        "meta_cancel",
        "commit_active_web_cancel",
        "signal_cancel",
    ):
        assert removed_symbol not in combined
    assert "UPDATE turns SET status='cancelled'" not in submission_source


@pytest.mark.parametrize("text", ["follow up", "取消"])
async def test_active_v1_never_enters_legacy_steer(text: str) -> None:
    store = FakeSessionStore(active="t_v1", active_protocol_version=1)
    orch = FakeOrchestrator(store)

    out = await steer_or_proceed(
        orch,
        session_id="s1",
        text=text,
        user_msg_id="u1",
    )

    assert out == SteerOutcome("protocol_active", "t_v1", 1)
    assert store.append_calls == []
    assert orch.cancelled == []


async def test_active_v1_with_attachments_fails_before_protocol_or_buffer() -> None:
    store = FakeSessionStore(active="t_v1", active_protocol_version=1)
    orch = FakeOrchestrator(store)

    out = await steer_or_proceed(
        orch,
        session_id="s1",
        text="follow up",
        user_msg_id="u1",
        has_attachments=True,
    )

    assert out == SteerOutcome("attachments_unsupported", "t_v1")
    assert store.append_calls == []
    assert orch.cancelled == []


async def test_expected_turn_id_is_passed_to_atomic_append() -> None:
    store = FakeSessionStore(active="t_active", append_result=True)
    orch = FakeOrchestrator(store)
    out = await steer_or_proceed(
        orch,
        session_id="s1",
        text="follow up",
        user_msg_id="u1",
        expected_turn_id="t_active",
    )
    assert out == SteerOutcome("buffered", "t_active")
    assert store.append_calls == [
        {
            "session_id": "s1",
            "text": "follow up",
            "expected_turn_id": "t_active",
        }
    ]


async def test_expected_turn_mismatch_completes_without_buffering_or_fresh_turn() -> None:
    store = FakeSessionStore(active="t_new", append_result=True)
    orch = FakeOrchestrator(store)
    out = await steer_or_proceed(
        orch,
        session_id="s1",
        text="stale follow up",
        user_msg_id="u1",
        expected_turn_id="t_old",
    )
    assert out == SteerOutcome("completed", "t_new")
    assert store.append_calls == []


async def test_expected_turn_missing_active_completes_without_fresh_turn() -> None:
    store = FakeSessionStore(active=None, append_result=True)
    orch = FakeOrchestrator(store)
    out = await steer_or_proceed(
        orch,
        session_id="s1",
        text="stale follow up",
        user_msg_id="u1",
        expected_turn_id="t_old",
    )
    assert out == SteerOutcome("completed", "t_old")
    assert store.append_calls == []


async def test_append_lost_race_falls_through() -> None:
    store = FakeSessionStore(active="t_active", append_result=False)
    orch = FakeOrchestrator(store)
    out = await steer_or_proceed(orch, session_id="s1", text="follow up", user_msg_id="u1")
    assert out.kind == "proceed"  # fall through to a fresh turn
    assert store.append_calls == [
        {
            "session_id": "s1",
            "text": "follow up",
            "expected_turn_id": "t_active",
        }
    ]


async def test_legacy_read_then_v1_swap_cannot_poison_new_turn_buffer() -> None:
    store = FakeSessionStore(
        active="t_old",
        append_result=True,
        active_on_append="t_v1",
    )
    orch = FakeOrchestrator(store)

    out = await steer_or_proceed(
        orch,
        session_id="s1",
        text="belongs to old turn",
        user_msg_id="u1",
    )

    assert out == SteerOutcome("proceed", "t_old")
    assert store.append_calls[0]["expected_turn_id"] == "t_old"
    assert store.buffer == []


async def test_expected_turn_append_lost_race_completes_instead_of_fresh_turn() -> None:
    store = FakeSessionStore(active="t_active", append_result=False)
    orch = FakeOrchestrator(store)
    out = await steer_or_proceed(
        orch,
        session_id="s1",
        text="follow up",
        user_msg_id="u1",
        expected_turn_id="t_active",
    )
    assert out == SteerOutcome("completed", "t_active")
    assert store.append_calls == [
        {
            "session_id": "s1",
            "text": "follow up",
            "expected_turn_id": "t_active",
        }
    ]


async def test_empty_text_with_active_turn_falls_through_without_appending() -> None:
    store = FakeSessionStore(active="t_active")
    orch = FakeOrchestrator(store)
    out = await steer_or_proceed(orch, session_id="s1", text="", user_msg_id="u1")
    assert out.kind == "proceed"
    assert store.append_calls == []  # empty msg = nothing to buffer
    assert orch.cancelled == []


@pytest.mark.parametrize("kw", ["停", "停下", "取消", "算了", "终止", "别看了"])
async def test_former_meta_keyword_is_ordinary_steer_text(kw: str) -> None:
    store = FakeSessionStore(active="t_active", append_result=True)
    orch = FakeOrchestrator(store)
    out = await steer_or_proceed(orch, session_id="s1", text=kw, user_msg_id="u1")
    assert out == SteerOutcome("buffered", "t_active")
    assert store.append_calls == [
        {
            "session_id": "s1",
            "text": kw,
            "expected_turn_id": "t_active",
        }
    ]
    assert orch.cancelled == []  # never cancels


# ─── spawn_turn ──────────────────────────────────────────────────────────────


class FakeDispatchStore:
    def __init__(
        self,
        *,
        claim_results: tuple[str, ...] = ("claimed",),
        raise_record: bool = False,
    ) -> None:
        self.claim_results = list(claim_results)
        self.raise_record = raise_record
        self.claim_calls: list[dict] = []
        self.ensure_calls: list[dict] = []
        self.record_calls: list[dict] = []

    async def require_legacy_chat_execution(self, **_kwargs: object) -> None:
        # This dispatch harness models only legacy text sources.
        pass

    async def claim_chat_spawn_dispatch_for_worker(self, **kwargs) -> str:
        self.claim_calls.append(kwargs)
        return self.claim_results.pop(0)

    async def ensure_session(self, **kwargs) -> None:
        self.ensure_calls.append(kwargs)

    async def record_turn(self, **kwargs) -> None:
        self.record_calls.append(kwargs)
        if self.raise_record:
            raise RuntimeError("record boom")

    async def heartbeat_chat_spawn_dispatch(self, **_kwargs) -> str:
        return "renewed"


class HealthyPersistBus:
    _persist_before_live = True

    def raise_if_failed(self) -> None:
        return None


class FakeSpawnRegistry:
    def __init__(self) -> None:
        self.bus = HealthyPersistBus()
        self.drain_calls: list[str] = []
        self.close_calls: list[tuple[str, object | None]] = []

    async def projection_checkpoint(self, _turn_id: str):
        return self.bus, 0

    async def drain_turn_persistence(self, turn_id: str) -> None:
        self.drain_calls.append(turn_id)

    async def close(self, turn_id: str, *, expected=None) -> None:
        self.close_calls.append((turn_id, expected))


class FakeSpawnOrchestrator:
    def __init__(self, store, registry=None) -> None:
        self.session_store = store
        self.event_registry = registry or FakeSpawnRegistry()
        self.dispatch_owner_id = "worker-a"
        self._cancel_events = {}


async def spawn_turn(orchestrator, turn_req, **kwargs):
    if getattr(turn_req, "terminal_runtime", None) is None:
        kwargs.setdefault(
            "admitted_legacy_bus",
            orchestrator.event_registry.bus,
        )
    return await _real_spawn_turn(orchestrator, turn_req, **kwargs)


class FakeResumeStore:
    def __init__(
        self,
        *,
        transition: bool = True,
        status: str = "clarification_pending",
    ) -> None:
        self.transition = transition
        self.status = status
        self.generation = 1
        self.advance_calls: list[str] = []
        self.rollback_calls: list[dict] = []
        self.claim_calls: list[dict] = []
        self.terminal_calls: list[dict] = []
        self.remote_generation: int | None = None
        self.lease_state: str | None = None
        self.lease_owner: str | None = None

    async def advance_turn_execution_generation(self, *, turn_id: str) -> int | None:
        self.advance_calls.append(turn_id)
        if not self.transition:
            return None
        self.generation += 1
        self.status = "running"
        self.lease_state = "claimed"
        return self.generation

    async def get_turn(self, _turn_id: str):
        return type(
            "Turn",
            (),
            {
                "execution_generation": self.generation,
                "status": self.status,
            },
        )()

    async def get_active_resume_dispatch_generation(self, *, turn_id: str):
        return self.remote_generation

    async def rollback_turn_resume_generation(self, **kwargs) -> bool:
        self.rollback_calls.append(kwargs)
        owner_id = kwargs["owner_id"]
        matches = (owner_id is None and self.lease_state == "claimed" and self.lease_owner is None) or (
            owner_id is not None and self.lease_state == "spawned" and self.lease_owner == owner_id
        )
        if matches:
            self.lease_state = "terminal"
            self.lease_owner = None
            self.status = "clarification_pending"
        return matches

    async def claim_chat_spawn_dispatch_for_worker(self, **kwargs) -> str:
        self.claim_calls.append(kwargs)
        if self.lease_state != "claimed":
            return "rejected"
        self.lease_state = "spawned"
        self.lease_owner = kwargs["owner_id"]
        return "claimed"

    async def heartbeat_chat_spawn_dispatch(self, **_kwargs) -> str:
        return "renewed"

    async def mark_chat_spawn_dispatch_terminal(self, **kwargs) -> bool:
        self.terminal_calls.append(kwargs)
        return True


class FakeTurnReq:
    def __init__(self, session_id: str = "s1", turn_id: str = "t1") -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.workspace_id = "ws1"
        self.user_id = "u1"
        self.text = "hello"


async def test_admit_legacy_chat_execution_uses_injected_registry() -> None:
    events: list[tuple] = []
    durable_bus = object()

    class Registry:
        async def get_or_create(
            self,
            turn_id: str,
            *,
            initial_seq: int = 0,
            exclusive_producer: bool = False,
            persist_before_live: bool = False,
        ):
            assert exclusive_producer is True
            assert persist_before_live is True
            events.append(("get_or_create", turn_id, initial_seq))
            return durable_bus

        async def close(self, turn_id: str, *, expected=None) -> None:
            events.append(("close", turn_id, expected))

    orchestrator = SimpleNamespace(event_registry=Registry())
    request = FakeTurnReq(turn_id="t_admitted")

    assert (
        await admit_legacy_chat_execution(
            orchestrator,
            request,
            initial_seq=9,
        )
        is durable_bus
    )
    assert events == [("get_or_create", "t_admitted", 9)]


async def test_admit_legacy_chat_execution_rejects_terminal_runtime() -> None:
    request = FakeTurnReq(turn_id="t_terminal")
    request.terminal_runtime = object()

    with pytest.raises(
        RuntimeError,
        match="terminal_runtime_cannot_use_legacy_chat_adapter",
    ):
        await admit_legacy_chat_execution(
            SimpleNamespace(event_registry=object()),
            request,
        )


async def test_concurrent_legacy_admission_has_one_bus_owner() -> None:
    from bowhead_core.events import EventBusRegistry, OrchestratorEvent

    async def accept_event(_event: OrchestratorEvent) -> bool:
        return True

    registry = EventBusRegistry(default_log_sink=accept_event)
    orchestrator = SimpleNamespace(event_registry=registry)
    request = FakeTurnReq(turn_id="t_exclusive_admission")

    outcomes = await asyncio.gather(
        admit_legacy_chat_execution(orchestrator, request),
        admit_legacy_chat_execution(orchestrator, request),
        return_exceptions=True,
    )

    winners = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], RuntimeError)
    assert str(losers[0]) == "turn producer already admitted"

    winner = winners[0]
    assert await winner.emit(
        OrchestratorEvent(
            event_type="turn_started",
            turn_id=request.turn_id,
            payload={},
        )
    )
    projected, _floor = await registry.projection_checkpoint(request.turn_id)
    assert projected is winner
    await registry.close(request.turn_id, expected=winner)


async def test_concurrent_cross_registry_spawn_runs_one_worker(
    tmp_path,
    monkeypatch,
) -> None:
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.session_store import SessionStore

    async def accept_event(_event) -> bool:
        return True

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    await store.ensure_session(
        session_id="s_spawn_race",
        workspace_id="ws1",
        user_id="u1",
    )
    await store.record_turn(
        turn_id="t_spawn_race",
        session_id="s_spawn_race",
        user_text="hello",
    )
    registries = [
        EventBusRegistry(default_log_sink=accept_event),
        EventBusRegistry(default_log_sink=accept_event),
    ]
    orchestrators = [
        SimpleNamespace(
            event_registry=registry,
            session_store=store,
            dispatch_owner_id=f"gateway-{index}",
            _cancel_events={},
        )
        for index, registry in enumerate(registries)
    ]
    request = FakeTurnReq("s_spawn_race", "t_spawn_race")
    buses = [await admit_legacy_chat_execution(orchestrator, request) for orchestrator in orchestrators]
    worker_started = asyncio.Event()
    release_worker = asyncio.Event()
    worker_calls = 0

    async def one_worker(*_args, **_kwargs) -> None:
        nonlocal worker_calls
        worker_calls += 1
        worker_started.set()
        await release_worker.wait()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", one_worker)
    outcomes = await asyncio.gather(
        *(
            _real_spawn_turn(
                orchestrator,
                request,
                admitted_legacy_bus=bus,
            )
            for orchestrator, bus in zip(orchestrators, buses, strict=True)
        ),
        return_exceptions=True,
    )
    tasks = [outcome for outcome in outcomes if isinstance(outcome, asyncio.Task)]
    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(tasks) == 1, outcomes
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "turn_dispatch_not_claimable"
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    assert worker_calls == 1
    with sqlite3.connect(store.db_path) as connection:
        state, owner = connection.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (request.turn_id,),
        ).fetchone()
    assert state == "spawned"
    assert owner in {"gateway-0", "gateway-1"}
    turn = await store.get_turn(request.turn_id)
    assert turn is not None and turn.status == "running"
    assert (
        await store.heartbeat_chat_spawn_dispatch(
            turn_id=request.turn_id,
            owner_id=owner,
        )
        == "renewed"
    )

    release_worker.set()
    await tasks[0]
    for registry, bus in zip(registries, buses, strict=True):
        await registry.close(request.turn_id, expected=bus)


@pytest.fixture
def captured_runner(monkeypatch):
    """Patch routes.chat._run_turn_and_cleanup with a recorder so spawn_turn's lazy import
    resolves to it; returns the dict the recorder fills + the started Event."""
    rec: dict = {}
    started = asyncio.Event()

    async def fake_runner(
        orch,
        req,
        *,
        user_store=None,
        team_id=None,
        metrics_log=None,
        entitlements=None,
        admitted_legacy_bus=None,
        cleanup_grace_s=5.0,
    ):
        rec["orch"] = orch
        rec["req"] = req
        rec["kwargs"] = {
            "user_store": user_store,
            "team_id": team_id,
            "metrics_log": metrics_log,
            "entitlements": entitlements,
            "cleanup_grace_s": cleanup_grace_s,
        }
        started.set()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", fake_runner)
    return rec, started


async def test_spawn_turn_claims_before_spawn_and_threads_args(captured_runner) -> None:
    rec, started = captured_runner
    store = FakeDispatchStore()
    orch = FakeSpawnOrchestrator(store)
    req = FakeTurnReq("s_abc", "t_xyz")

    task = await spawn_turn(
        orch,
        req,
        user_store="US",
        team_id="o1",
        metrics_log="ML",
        entitlements="EN",
    )

    assert store.claim_calls == [
        {"turn_id": "t_xyz", "owner_id": "worker-a"},
    ]
    assert store.ensure_calls == []
    assert store.record_calls == []
    # registered in the inflight set, default name prefix applied
    assert task in chat_mod._inflight_tasks
    assert task.get_name() == "turn_run:t_xyz"

    await started.wait()
    await task
    await asyncio.sleep(0)  # let the done-callback fire

    # worker received the orchestrator/request + every kwarg threaded through
    assert rec["orch"] is orch
    assert rec["req"] is req
    assert rec["kwargs"] == {
        "user_store": "US",
        "team_id": "o1",
        "metrics_log": "ML",
        "entitlements": "EN",
        "cleanup_grace_s": 5.0,
    }
    # done-callback removed it from the inflight set
    assert task not in chat_mod._inflight_tasks


async def test_spawn_turn_claims_exact_mcp_reference_runtime(
    captured_runner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import bowhead_core.reference_mcp_execution as mcp_execution
    from bowhead_core.access_control import AccessControlStore
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceScope
    from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex
    from cryptography.fernet import Fernet

    class ReferenceDispatchStore(FakeDispatchStore):
        def __init__(self, db_path) -> None:
            super().__init__()
            self._db_path = db_path
            self.terminal_calls: list[dict] = []

        async def get_turn(self, _turn_id: str):
            return SimpleNamespace(status="completed")

        async def mark_chat_spawn_dispatch_terminal(self, **kwargs) -> bool:
            self.terminal_calls.append(kwargs)
            return True

    claim_calls: list[dict] = []
    validated: list[tuple[ReferencePrimaryRuntime, object]] = []

    async def claim_mcp(**kwargs):
        claim_calls.append(kwargs)
        return SimpleNamespace(outcome="claimed")

    async def validate_runtime(self, request) -> None:
        validated.append((self, request))

    monkeypatch.setattr(
        mcp_execution,
        "claim_reference_mcp_primary_for_worker",
        claim_mcp,
    )
    monkeypatch.setattr(ReferencePrimaryRuntime, "validate", validate_runtime)
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())

    db_path = tmp_path / "core.db"
    index = ReferenceMcpMetadataIndex(db_path=db_path, ready=True)
    access = AccessControlStore(db_path)
    store = ReferenceDispatchStore(db_path)
    orchestrator = FakeSpawnOrchestrator(store)
    request = FakeTurnReq("s_mcp", "t_mcp")
    request.reference_scope = ReferenceScope(
        team_id="team1",
        user_id="u1",
        workspace_id="ws1",
        session_id="s_mcp",
        policy_epoch=7,
    )

    rec, started = captured_runner
    task = await spawn_turn(
        orchestrator,
        request,
        reference_mcp_index=index,
        reference_mcp_access=access,
    )
    await started.wait()
    await task
    for _attempt in range(100):
        if store.terminal_calls:
            break
        await asyncio.sleep(0.01)

    assert len(claim_calls) == 1
    claim = claim_calls[0]
    assert claim["index"] is index
    assert claim["access"] is access
    assert claim["scope"].model_dump() == {
        "team_id": "team1",
        "user_id": "u1",
        "workspace_id": "ws1",
        "session_id": "s_mcp",
    }
    assert claim["turn_id"] == "t_mcp"
    assert claim["owner_id"] == "worker-a"
    assert claim["expected_generation"] == 1
    assert validated == [(request.reference_runtime, request)]
    assert rec["req"] is request
    assert request.reference_runtime.mcp_index is index
    assert request.reference_runtime.mcp_access is access
    assert request.reference_runtime.skill_sources is None
    assert request.reference_runtime.allow_bi_workspace_continuation is False
    assert store.claim_calls == []
    assert store.terminal_calls == [
        {"turn_id": "t_mcp", "owner_id": "worker-a", "failed": False},
    ]


async def test_worker_cancelled_before_first_step_retires_exact_generation(
    captured_runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rec, started = captured_runner
    store = FakeDispatchStore()
    registry = FakeSpawnRegistry()
    orchestrator = FakeSpawnOrchestrator(store, registry)
    request = FakeTurnReq("s1", "t_cancel_before_first_step")
    original_create_task = asyncio.create_task

    def cancel_worker_immediately(coro, *, name=None, context=None):
        task = original_create_task(coro, name=name, context=context)
        if name == f"turn_run:{request.turn_id}":
            task.cancel("durable_user_cancel")
        return task

    monkeypatch.setattr(asyncio, "create_task", cancel_worker_immediately)
    task = await spawn_turn(orchestrator, request)

    with pytest.raises(asyncio.CancelledError):
        await task
    for _attempt in range(100):
        if registry.close_calls:
            break
        await asyncio.sleep(0.01)

    assert not started.is_set()
    assert request.turn_id not in orchestrator._cancel_events
    assert registry.close_calls == [(request.turn_id, registry.bus)]
    assert task not in chat_mod._inflight_tasks


async def test_cross_gateway_rejected_spawn_retires_only_loser_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = asyncio.Event()
    worker_stopped = asyncio.Event()

    async def blocking_runner(*_args, **_kwargs) -> None:
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.set()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", blocking_runner)
    store = FakeDispatchStore(claim_results=("claimed", "rejected"))
    winner_registry = FakeSpawnRegistry()
    loser_registry = FakeSpawnRegistry()
    winner_orchestrator = FakeSpawnOrchestrator(store, winner_registry)
    loser_orchestrator = FakeSpawnOrchestrator(store, loser_registry)
    request = FakeTurnReq("s1", "t_duplicate_spawn")

    winner = await spawn_turn(winner_orchestrator, request)
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    winner_fence = winner_orchestrator._cancel_events[request.turn_id]

    with pytest.raises(
        ts_mod.TurnDispatchAlreadyOwnedError,
        match="turn_dispatch_not_claimable",
    ):
        await spawn_turn(loser_orchestrator, request)

    assert winner_orchestrator._cancel_events[request.turn_id] is winner_fence
    assert winner_registry.close_calls == []
    assert request.turn_id not in loser_orchestrator._cancel_events
    assert loser_registry.close_calls == [
        (request.turn_id, loser_registry.bus),
    ]
    winner_fence.set()
    await asyncio.wait_for(worker_stopped.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await winner
    for _attempt in range(100):
        if request.turn_id not in winner_orchestrator._cancel_events:
            break
        await asyncio.sleep(0.01)
    assert request.turn_id not in winner_orchestrator._cancel_events
    assert winner_registry.close_calls == [
        (request.turn_id, winner_registry.bus),
    ]


async def test_cancelled_terminal_worker_waiter_does_not_abort_existing_worker() -> None:
    entered = asyncio.Event()

    class Runtime:
        scope = TerminalScope(
            team_id="team1",
            session_id="s1",
            turn_id="t_duplicate_waiter",
            generation=2,
        )
        bus = SimpleNamespace(receipt=None)
        retired = False
        abort_calls = 0

        async def spawn_worker_once(self, _spawn):
            entered.set()
            await asyncio.Event().wait()

        async def abort_worker_start_failed(self) -> None:
            self.abort_calls += 1

    runtime = Runtime()
    request = FakeTurnReq("s1", "t_duplicate_waiter")
    request.team_id = "team1"
    request.execution_generation = 1
    request.terminal_runtime = runtime
    waiter = asyncio.create_task(spawn_turn(FakeSpawnOrchestrator(FakeDispatchStore()), request))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert request.execution_generation == 2
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert runtime.abort_calls == 0


async def test_terminal_worker_task_creation_failure_aborts_exactly_once(
    monkeypatch,
) -> None:
    class Runtime:
        scope = TerminalScope(
            team_id="team1",
            session_id="s1",
            turn_id="t_create_failure",
            generation=1,
        )
        bus = SimpleNamespace(receipt=None)
        retired = False
        abort_calls = 0

        async def spawn_worker_once(self, spawn):
            return spawn(), True

        async def abort_worker_start_failed(self) -> None:
            self.abort_calls += 1

    runtime = Runtime()
    request = FakeTurnReq("s1", "t_create_failure")
    request.team_id = "team1"
    request.execution_generation = 1
    request.terminal_runtime = runtime
    original_create_task = asyncio.create_task

    def fail_worker_task(coro, *, name=None, context=None):
        if name == "turn_run:t_create_failure":
            raise RuntimeError("task creation failed")
        return original_create_task(coro, name=name, context=context)

    monkeypatch.setattr(asyncio, "create_task", fail_worker_task)

    with pytest.raises(RuntimeError, match="task creation failed"):
        await spawn_turn(
            FakeSpawnOrchestrator(FakeDispatchStore()),
            request,
        )
    assert runtime.abort_calls == 1


async def test_legacy_worker_task_creation_failure_keeps_original_when_handoff_fails(
    monkeypatch,
    captured_runner,
) -> None:
    store = FakeDispatchStore()
    orchestrator = FakeSpawnOrchestrator(store)
    request = FakeTurnReq("s1", "t_legacy_create_failure")
    original_create_task = asyncio.create_task

    def fail_worker_task(coro, *, name=None, context=None):
        if name == "turn_run:t_legacy_create_failure":
            raise RuntimeError("task creation failed")
        return original_create_task(coro, name=name, context=context)

    async def fail_recovery_handoff(*_args, **_kwargs):
        raise RuntimeError("recovery handoff failed")

    monkeypatch.setattr(asyncio, "create_task", fail_worker_task)
    monkeypatch.setattr(
        ts_mod,
        "handoff_legacy_spawn_failure_to_recovery",
        fail_recovery_handoff,
    )

    with pytest.raises(RuntimeError, match="task creation failed") as caught:
        await spawn_turn(orchestrator, request)

    assert any("recovery handoff failed" in note for note in getattr(caught.value, "__notes__", ()))
    assert ts_mod.legacy_spawn_failure_handoff_attempted(caught.value)
    assert "t_legacy_create_failure" not in orchestrator._cancel_events
    assert orchestrator.event_registry.close_calls == [
        ("t_legacy_create_failure", orchestrator.event_registry.bus),
    ]


async def test_legacy_preworker_durability_abort_is_contained(
    captured_runner,
) -> None:
    _rec, started = captured_runner
    store = FakeDispatchStore()
    orchestrator = FakeSpawnOrchestrator(store)
    request = FakeTurnReq("s1", "t_preworker_durability_abort")

    async def reject_notice() -> None:
        raise TerminalDurabilityAbort("notice persistence failed")

    with pytest.raises(
        RuntimeError,
        match="legacy_chat_durability_recovery_handoff",
    ) as failure:
        await spawn_turn(
            orchestrator,
            request,
            before_schedule=reject_notice,
        )

    assert isinstance(failure.value.__cause__, TerminalDurabilityAbort)
    assert not started.is_set()
    assert orchestrator._cancel_events == {}
    assert orchestrator.event_registry.close_calls == [
        (request.turn_id, orchestrator.event_registry.bus),
    ]


async def test_preworker_remote_cancel_after_callback_never_starts_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = asyncio.Event()
    recovery_calls: list[str] = []

    async def runner(*_args, **_kwargs) -> None:
        worker_started.set()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", runner)

    class DispatchStore(FakeDispatchStore):
        remote_cancel = False

        async def heartbeat_chat_spawn_dispatch(self, **_kwargs) -> str:
            return "cancel_requested" if self.remote_cancel else "renewed"

        async def mark_chat_spawn_dispatch_drain_pending(self, **kwargs) -> bool:
            recovery_calls.append(kwargs["turn_id"])
            return True

        async def recover_expired_chat_spawn_dispatches(self) -> list[str]:
            return list(recovery_calls)

    store = DispatchStore()
    orchestrator = FakeSpawnOrchestrator(store)
    request = FakeTurnReq("s1", "t_remote_cancel_before_worker")

    async def persist_resume_notice() -> None:
        await asyncio.sleep(0)
        store.remote_cancel = True

    with pytest.raises(
        ts_mod.TurnDispatchCancelledError,
        match="turn_cancelled_before_dispatch",
    ):
        await spawn_turn(
            orchestrator,
            request,
            before_schedule=persist_resume_notice,
        )

    assert not worker_started.is_set()
    assert recovery_calls == [request.turn_id]
    assert orchestrator._cancel_events == {}
    assert orchestrator.event_registry.close_calls == [
        (request.turn_id, orchestrator.event_registry.bus),
    ]


async def test_preworker_remote_cancel_after_claim_commit_never_starts_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = asyncio.Event()
    recovery_calls: list[str] = []

    async def runner(*_args, **_kwargs) -> None:
        worker_started.set()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", runner)

    class DispatchStore(FakeDispatchStore):
        remote_cancel = False

        async def claim_chat_spawn_dispatch_for_worker(self, **kwargs) -> str:
            claim = await super().claim_chat_spawn_dispatch_for_worker(**kwargs)
            # Model another Gateway committing Stop after this claim's DB
            # transaction, before the awaiting caller resumes.
            self.remote_cancel = True
            return claim

        async def heartbeat_chat_spawn_dispatch(self, **_kwargs) -> str:
            return "cancel_requested" if self.remote_cancel else "renewed"

        async def mark_chat_spawn_dispatch_drain_pending(self, **kwargs) -> bool:
            recovery_calls.append(kwargs["turn_id"])
            return True

        async def recover_expired_chat_spawn_dispatches(self) -> list[str]:
            return list(recovery_calls)

    store = DispatchStore()
    orchestrator = FakeSpawnOrchestrator(store)
    request = FakeTurnReq("s1", "t_remote_cancel_after_claim")

    with pytest.raises(
        ts_mod.TurnDispatchCancelledError,
        match="turn_cancelled_before_dispatch",
    ):
        await spawn_turn(orchestrator, request)

    assert not worker_started.is_set()
    assert recovery_calls == [request.turn_id]
    assert orchestrator._cancel_events == {}
    assert orchestrator.event_registry.close_calls == [
        (request.turn_id, orchestrator.event_registry.bus),
    ]


async def test_preworker_owner_loss_after_callback_never_starts_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = asyncio.Event()
    recovery_completed = asyncio.Event()

    async def runner(*_args, **_kwargs) -> None:
        worker_started.set()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", runner)

    class DispatchStore(FakeDispatchStore):
        async def heartbeat_chat_spawn_dispatch(self, **_kwargs) -> str:
            return "lost"

    store = DispatchStore()
    orchestrator = FakeSpawnOrchestrator(store)
    request = FakeTurnReq("s1", "t_owner_lost_before_worker")

    async def persist_resume_notice() -> None:
        await asyncio.sleep(0)

    async def recover_owner_loss() -> None:
        recovery_completed.set()

    with pytest.raises(
        ts_mod.TurnDispatchAlreadyOwnedError,
        match="turn_dispatch_ownership_lost_before_worker",
    ):
        await spawn_turn(
            orchestrator,
            request,
            before_schedule=persist_resume_notice,
            preworker_failure_recovery=recover_owner_loss,
        )

    assert recovery_completed.is_set()
    assert not worker_started.is_set()
    assert orchestrator._cancel_events == {}
    assert orchestrator.event_registry.close_calls == [
        (request.turn_id, orchestrator.event_registry.bus),
    ]


async def test_preworker_handoff_survives_repeated_caller_cancellation() -> None:
    recovery_started = asyncio.Event()
    allow_recovery = asyncio.Event()
    recovery_completed = asyncio.Event()
    recovery_cancelled = False
    cleanup_order: list[str] = []

    class OrderedRegistry(FakeSpawnRegistry):
        async def close(self, turn_id: str, *, expected=None) -> None:
            assert recovery_completed.is_set()
            cleanup_order.append("bus_retire")
            await super().close(turn_id, expected=expected)

    store = FakeDispatchStore()
    orchestrator = FakeSpawnOrchestrator(store, OrderedRegistry())
    request = FakeTurnReq("s1", "t_double_cancel_handoff")

    async def reject_before_schedule() -> None:
        raise RuntimeError("preworker callback failed")

    async def recover() -> None:
        nonlocal recovery_cancelled
        recovery_started.set()
        try:
            await allow_recovery.wait()
        except asyncio.CancelledError:
            recovery_cancelled = True
            raise
        recovery_completed.set()
        cleanup_order.append("store_handoff")

    caller = asyncio.create_task(
        spawn_turn(
            orchestrator,
            request,
            before_schedule=reject_before_schedule,
            preworker_failure_recovery=recover,
        ),
    )
    await asyncio.wait_for(recovery_started.wait(), timeout=1)
    caller.cancel("first shutdown signal")
    await asyncio.sleep(0)
    caller.cancel("second shutdown signal")
    await asyncio.sleep(0)
    assert not recovery_cancelled

    allow_recovery.set()
    with pytest.raises(RuntimeError, match="preworker callback failed"):
        await asyncio.wait_for(caller, timeout=1)
    assert recovery_completed.is_set()
    assert not recovery_cancelled
    assert cleanup_order == ["store_handoff", "bus_retire"]


async def test_preworker_stop_handoff_survives_repeated_caller_cancellation(
    monkeypatch,
) -> None:
    handoff_started = asyncio.Event()
    allow_handoff = asyncio.Event()
    handoff_completed = asyncio.Event()
    handoff_cancelled = False
    worker_started = False

    class StopStore(FakeDispatchStore):
        async def heartbeat_chat_spawn_dispatch(self, **_kwargs) -> str:
            return "cancel_requested"

        async def mark_chat_spawn_dispatch_drain_pending(
            self,
            **_kwargs,
        ) -> bool:
            nonlocal handoff_cancelled
            handoff_started.set()
            try:
                await allow_handoff.wait()
            except asyncio.CancelledError:
                handoff_cancelled = True
                raise
            handoff_completed.set()
            return True

        async def recover_expired_chat_spawn_dispatches(self) -> list[str]:
            assert handoff_completed.is_set()
            return ["t_double_cancel_stop"]

    async def forbidden_worker(*_args, **_kwargs) -> None:
        nonlocal worker_started
        worker_started = True

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", forbidden_worker)
    registry = FakeSpawnRegistry()
    caller = asyncio.create_task(
        spawn_turn(
            FakeSpawnOrchestrator(StopStore(), registry),
            FakeTurnReq("s1", "t_double_cancel_stop"),
        ),
    )
    await asyncio.wait_for(handoff_started.wait(), timeout=1)
    caller.cancel("first shutdown signal")
    await asyncio.sleep(0)
    caller.cancel("second shutdown signal")
    await asyncio.sleep(0)
    assert not handoff_cancelled
    assert registry.close_calls == []

    allow_handoff.set()
    with pytest.raises(
        ts_mod.TurnDispatchCancelledError,
        match="turn_cancelled_before_dispatch",
    ):
        await asyncio.wait_for(caller, timeout=1)
    assert handoff_completed.is_set()
    assert not handoff_cancelled
    assert not worker_started
    assert registry.close_calls == [
        ("t_double_cancel_stop", registry.bus),
    ]


async def test_spawn_turn_projection_failure_removes_new_cancel_fence(
    captured_runner,
) -> None:
    class ProjectionFailRegistry(FakeSpawnRegistry):
        async def projection_checkpoint(self, _turn_id: str):
            raise RuntimeError("projection unavailable")

    store = FakeDispatchStore()
    registry = ProjectionFailRegistry()
    orchestrator = FakeSpawnOrchestrator(store, registry)

    with pytest.raises(RuntimeError, match="projection unavailable"):
        await spawn_turn(
            orchestrator,
            FakeTurnReq("s1", "t_projection_failure"),
        )

    assert "t_projection_failure" not in orchestrator._cancel_events
    assert store.claim_calls == []
    assert registry.close_calls == [
        ("t_projection_failure", registry.bus),
    ]


async def test_spawn_turn_legacy_session_without_store_fails_closed(
    captured_runner,
) -> None:
    registry = FakeSpawnRegistry()
    orchestrator = SimpleNamespace(
        event_registry=registry,
        dispatch_owner_id="worker-a",
        _cancel_events={},
    )

    with pytest.raises(
        RuntimeError,
        match="legacy_chat_dispatch_store_unavailable",
    ):
        await spawn_turn(
            orchestrator,
            FakeTurnReq("s1", "t_store_missing"),
        )

    assert orchestrator._cancel_events == {}
    assert registry.close_calls == [
        ("t_store_missing", registry.bus),
    ]


async def test_cancel_before_dispatch_keeps_canonical_error_when_recovery_handoff_fails(
    captured_runner,
) -> None:
    class CancelledClaimStore(FakeDispatchStore):
        orchestrator: FakeSpawnOrchestrator

        async def claim_chat_spawn_dispatch_for_worker(self, **kwargs) -> str:
            self.claim_calls.append(kwargs)
            self.orchestrator._cancel_events[kwargs["turn_id"]].set()
            return "claimed"

        async def mark_chat_spawn_dispatch_drain_pending(self, **_kwargs) -> bool:
            raise RuntimeError("recovery marker unavailable")

    store = CancelledClaimStore()
    registry = FakeSpawnRegistry()
    orchestrator = FakeSpawnOrchestrator(store, registry)
    store.orchestrator = orchestrator

    with pytest.raises(
        RuntimeError,
        match="turn_cancelled_before_dispatch",
    ) as caught:
        await spawn_turn(
            orchestrator,
            FakeTurnReq("s1", "t_cancel_marker_failure"),
        )

    assert isinstance(caught.value, ts_mod.TurnDispatchCancelledError)
    assert any("recovery marker unavailable" in note for note in getattr(caught.value, "__notes__", ()))
    assert "t_cancel_marker_failure" not in orchestrator._cancel_events
    assert registry.close_calls == [
        ("t_cancel_marker_failure", registry.bus),
    ]


async def test_real_cancel_after_claim_recovers_before_worker_schedule(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    log_store = TurnLogStore(core_path)
    await log_store.initialize()
    session_id = "s_cancel_after_claim"
    turn_id = "t_cancel_after_claim"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws1",
        user_id="u1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="stop immediately",
    )
    registry = EventBusRegistry(
        default_log_sink=OrderedTurnLogSink(log_store),
    )
    orchestrator = SimpleNamespace(
        event_registry=registry,
        session_store=store,
        dispatch_owner_id="gateway-owner",
        _cancel_events={},
    )
    request = FakeTurnReq(session_id, turn_id)
    admitted_bus = await admit_legacy_chat_execution(orchestrator, request)
    real_claim = store.claim_chat_spawn_dispatch_for_worker

    async def claim_then_stop(**kwargs) -> str:
        outcome = await real_claim(**kwargs)
        if outcome == "claimed":
            assert (
                await store.request_cancel_turn(
                    team_id="team1",
                    user_id="u1",
                    cancel_request_id=("018f0cc3-e4a2-4b21-a109-ca11c1a10001"),
                    workspace_id="ws1",
                    turn_id=turn_id,
                    expected_execution_generation=1,
                )
                == "cancelled"
            )
        return outcome

    worker_started = False

    async def forbidden_worker(*_args, **_kwargs) -> None:
        nonlocal worker_started
        worker_started = True

    monkeypatch.setattr(
        store,
        "claim_chat_spawn_dispatch_for_worker",
        claim_then_stop,
    )
    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", forbidden_worker)

    with pytest.raises(
        ts_mod.TurnDispatchCancelledError,
        match="turn_cancelled_before_dispatch",
    ):
        await _real_spawn_turn(
            orchestrator,
            request,
            admitted_legacy_bus=admitted_bus,
        )

    assert not worker_started
    assert orchestrator._cancel_events == {}
    events = await log_store.list_events(turn_id)
    assert [event.event_type for event in events] == [
        "turn_cancelled",
        "done",
    ]
    assert events[-1].payload["status"] == "cancelled"
    assert await store.get_chat_spawn_dispatch_projection_state(turn_id=turn_id) == "settled"


async def test_real_preworker_revalidation_failure_recovers_without_worker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    log_store = TurnLogStore(core_path)
    await log_store.initialize()
    session_id = "s_preworker_revalidation_failure"
    turn_id = "t_preworker_revalidation_failure"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws1",
        user_id="u1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="recover before worker",
    )
    registry = EventBusRegistry(
        default_log_sink=OrderedTurnLogSink(log_store),
    )
    orchestrator = SimpleNamespace(
        event_registry=registry,
        session_store=store,
        dispatch_owner_id="gateway-owner",
        _cancel_events={},
    )
    request = FakeTurnReq(session_id, turn_id)
    admitted_bus = await admit_legacy_chat_execution(orchestrator, request)
    worker_started = False

    async def unavailable_heartbeat(**_kwargs) -> str:
        raise RuntimeError("preworker revalidation unavailable")

    async def forbidden_worker(*_args, **_kwargs) -> None:
        nonlocal worker_started
        worker_started = True

    monkeypatch.setattr(
        store,
        "heartbeat_chat_spawn_dispatch",
        unavailable_heartbeat,
    )
    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", forbidden_worker)

    with pytest.raises(
        RuntimeError,
        match="preworker revalidation unavailable",
    ):
        await _real_spawn_turn(
            orchestrator,
            request,
            admitted_legacy_bus=admitted_bus,
        )

    assert not worker_started
    assert orchestrator._cancel_events == {}
    turn = await store.get_turn(turn_id)
    assert turn is not None
    assert turn.status == "failed"
    assert turn.error_kind == "dispatch_start_failed"
    events = await log_store.list_events(turn_id)
    assert [event.event_type for event in events] == ["turn_failed", "done"]
    assert events[-1].payload["status"] == "failed"
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id=turn_id,
        )
        == "settled"
    )
    producer, _floor = await registry.projection_checkpoint(turn_id)
    assert producer is None


async def test_spawn_turn_custom_name_prefix(captured_runner) -> None:
    _, started = captured_runner
    task = await spawn_turn(
        FakeSpawnOrchestrator(FakeDispatchStore()),
        FakeTurnReq("s1", "t1"),
        name_prefix="turn_run_feishu",
    )
    assert task.get_name() == "turn_run_feishu:t1"
    await started.wait()
    await task


async def test_spawn_turn_metrics_log_defaults_none(captured_runner) -> None:
    rec, started = captured_runner
    # Feishu omits metrics_log → worker must receive None.
    task = await spawn_turn(
        FakeSpawnOrchestrator(FakeDispatchStore()),
        FakeTurnReq("s1", "t1"),
        team_id="o1",
    )
    await started.wait()
    await task
    assert rec["kwargs"]["metrics_log"] is None
    assert rec["kwargs"]["user_store"] is None


async def test_spawn_turn_untracked_persists_before_worker(captured_runner) -> None:
    _, started = captured_runner
    store = FakeDispatchStore(claim_results=("untracked", "claimed"))
    task = await spawn_turn(
        FakeSpawnOrchestrator(store),
        FakeTurnReq("s1", "t1"),
    )

    assert store.ensure_calls == [
        {"session_id": "s1", "workspace_id": "ws1", "user_id": "u1"},
    ]
    assert store.record_calls == [
        {"turn_id": "t1", "session_id": "s1", "user_text": "hello"},
    ]
    assert store.claim_calls == [
        {"turn_id": "t1", "owner_id": "worker-a"},
        {"turn_id": "t1", "owner_id": "worker-a"},
    ]
    await started.wait()
    await task


async def test_spawn_turn_untracked_record_failure_blocks_worker(
    captured_runner,
) -> None:
    rec, _started = captured_runner
    store = FakeDispatchStore(
        claim_results=("untracked",),
        raise_record=True,
    )

    with pytest.raises(RuntimeError, match="record boom"):
        await spawn_turn(
            FakeSpawnOrchestrator(store),
            FakeTurnReq("s1", "t1"),
        )

    assert rec == {}


async def test_spawn_turn_heartbeat_owner_loss_leaves_dispatch_for_recovery(
    monkeypatch,
) -> None:
    worker_started = asyncio.Event()
    worker_stopped = asyncio.Event()
    terminal_calls: list[dict] = []
    turn_probe_calls = 0

    async def blocking_runner(*_args, **_kwargs):
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.set()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", blocking_runner)

    class DispatchStore(FakeDispatchStore):
        heartbeat_calls = 0

        async def claim_chat_spawn_dispatch_for_worker(self, **_kwargs):
            return "claimed"

        async def heartbeat_chat_spawn_dispatch(self, **_kwargs):
            self.heartbeat_calls += 1
            return "renewed" if self.heartbeat_calls == 1 else "lost"

        async def get_turn(self, _turn_id: str):
            nonlocal turn_probe_calls
            turn_probe_calls += 1
            return type("Turn", (), {"status": "cancelled"})()

        async def mark_chat_spawn_dispatch_terminal(self, **kwargs):
            terminal_calls.append(kwargs)
            return True

    store = DispatchStore()
    orch = FakeSpawnOrchestrator(store)
    orch.dispatch_owner_id = "worker-a"
    orch._cancel_events = {}

    task = await spawn_turn(orch, FakeTurnReq("s1", "t_owner_lost"))
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    await asyncio.wait_for(worker_stopped.wait(), timeout=3)

    assert task.cancelled()
    for _attempt in range(100):
        if "t_owner_lost" not in orch._cancel_events:
            break
        await asyncio.sleep(0.01)
    assert "t_owner_lost" not in orch._cancel_events
    assert orch.event_registry.close_calls == [
        ("t_owner_lost", orch.event_registry.bus),
    ]
    assert turn_probe_calls == 0
    assert terminal_calls == []


async def test_spawn_turn_transient_heartbeat_exception_does_not_cancel_worker(
    monkeypatch,
) -> None:
    worker_started = asyncio.Event()
    allow_worker_finish = asyncio.Event()
    heartbeat_retried = asyncio.Event()
    settlement_heartbeat = asyncio.Event()
    persist_before_live_required = asyncio.Event()
    tail_drain_started = asyncio.Event()
    allow_tail_drain = asyncio.Event()
    dispatch_terminal = asyncio.Event()

    async def blocking_runner(*_args, **_kwargs):
        assert persist_before_live_required.is_set()
        worker_started.set()
        await allow_worker_finish.wait()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", blocking_runner)

    class _TailRegistry:
        bus = HealthyPersistBus()

        async def projection_checkpoint(self, turn_id: str):
            assert turn_id == "t_transient_heartbeat"
            persist_before_live_required.set()
            return self.bus, 0

        async def drain_turn_persistence(self, turn_id: str) -> None:
            assert turn_id == "t_transient_heartbeat"
            tail_drain_started.set()
            await allow_tail_drain.wait()

        async def close(self, turn_id: str, *, expected=None) -> None:
            assert turn_id == "t_transient_heartbeat"
            assert expected is self.bus

    class DispatchStore(FakeDispatchStore):
        calls = 0

        async def claim_chat_spawn_dispatch_for_worker(self, **_kwargs):
            return "claimed"

        async def heartbeat_chat_spawn_dispatch(self, **_kwargs):
            if _kwargs.get("settling"):
                settlement_heartbeat.set()
                return "renewed"
            self.calls += 1
            if self.calls == 1:
                return "renewed"
            if self.calls == 2:
                raise RuntimeError("database temporarily unavailable")
            heartbeat_retried.set()
            return "renewed"

        async def get_turn(self, _turn_id: str):
            return type("Turn", (), {"status": "completed"})()

        async def mark_chat_spawn_dispatch_terminal(self, **kwargs):
            assert kwargs["failed"] is False
            dispatch_terminal.set()
            return True

    store = DispatchStore()
    orch = FakeSpawnOrchestrator(store)
    orch.event_registry = _TailRegistry()
    orch.dispatch_owner_id = "worker-a"
    orch._cancel_events = {}

    task = await spawn_turn(orch, FakeTurnReq("s1", "t_transient_heartbeat"))
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    await asyncio.wait_for(heartbeat_retried.wait(), timeout=3)
    assert not task.done()

    allow_worker_finish.set()
    await asyncio.wait_for(task, timeout=1)
    await asyncio.wait_for(tail_drain_started.wait(), timeout=2)
    await asyncio.wait_for(settlement_heartbeat.wait(), timeout=2)
    assert not dispatch_terminal.is_set()
    allow_tail_drain.set()
    await asyncio.wait_for(dispatch_terminal.wait(), timeout=1)


async def test_spawn_turn_owner_loss_never_settles_cas_without_wire_terminal(
    monkeypatch,
) -> None:
    canonical_committed = asyncio.Event()
    worker_stopped = asyncio.Event()
    recovery_started = asyncio.Event()
    allow_recovery = asyncio.Event()
    terminal_calls: list[dict] = []
    recovery_calls = 0
    turn_probe_calls = 0

    async def cas_then_block(*_args, **_kwargs):
        canonical_committed.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.set()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", cas_then_block)
    monkeypatch.setattr(ts_mod, "_CHAT_DISPATCH_LEASE_SECONDS", 0.0)

    class DispatchStore(FakeDispatchStore):
        heartbeat_calls = 0

        async def claim_chat_spawn_dispatch_for_worker(self, **_kwargs):
            return "claimed"

        async def heartbeat_chat_spawn_dispatch(self, **_kwargs):
            self.heartbeat_calls += 1
            if self.heartbeat_calls == 1:
                return "renewed"
            raise RuntimeError("database unavailable through lease deadline")

        async def recover_expired_chat_spawn_dispatches(self):
            nonlocal recovery_calls
            recovery_calls += 1
            recovery_started.set()
            await allow_recovery.wait()
            raise RuntimeError("recovery transaction also unavailable")

        async def get_turn(self, _turn_id: str):
            nonlocal turn_probe_calls
            turn_probe_calls += 1
            return type("Turn", (), {"status": "cancelled"})()

        async def mark_chat_spawn_dispatch_terminal(self, **kwargs):
            terminal_calls.append(kwargs)
            return True

    store = DispatchStore()
    orchestrator = FakeSpawnOrchestrator(store)
    orchestrator.dispatch_owner_id = "worker-a"
    orchestrator._cancel_events = {}

    task = await spawn_turn(
        orchestrator,
        FakeTurnReq("s1", "t_owner_loss_after_cas"),
    )
    await asyncio.wait_for(canonical_committed.wait(), timeout=1)
    await asyncio.wait_for(recovery_started.wait(), timeout=3)
    # Expiry recovery may block on the Store, but the untrusted worker must
    # already be stopped before recovery begins.
    await asyncio.wait_for(worker_stopped.wait(), timeout=3)
    assert task.cancelled()
    allow_recovery.set()
    assert recovery_calls == 1
    assert turn_probe_calls == 0
    assert terminal_calls == []


async def test_spawn_turn_cancelled_monitor_leaves_running_worker_for_lease_recovery(
    monkeypatch,
) -> None:
    worker_started = asyncio.Event()
    allow_worker_finish = asyncio.Event()
    terminal_calls: list[dict] = []

    async def blocking_runner(*_args, **_kwargs):
        worker_started.set()
        await allow_worker_finish.wait()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", blocking_runner)

    class DispatchStore(FakeDispatchStore):
        async def claim_chat_spawn_dispatch_for_worker(self, **_kwargs):
            return "claimed"

        async def heartbeat_chat_spawn_dispatch(self, **_kwargs):
            return "renewed"

        async def mark_chat_spawn_dispatch_terminal(self, **kwargs):
            terminal_calls.append(kwargs)
            return True

    store = DispatchStore()
    orch = FakeSpawnOrchestrator(store)
    orch.dispatch_owner_id = "worker-a"
    orch._cancel_events = {}

    task = await spawn_turn(orch, FakeTurnReq("s1", "t_shutdown"))
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    monitor = next(pending for pending in tuple(chat_mod._inflight_tasks) if pending.get_name() == "turn_dispatch_lease:t_shutdown")
    monitor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await monitor

    assert not task.done()
    assert terminal_calls == []

    allow_worker_finish.set()
    await asyncio.wait_for(task, timeout=1)


async def test_spawn_turn_durable_cancel_request_stops_remote_worker(
    monkeypatch,
) -> None:
    worker_started = asyncio.Event()
    worker_stopped = asyncio.Event()
    dispatch_terminal = asyncio.Event()
    get_turn_started = asyncio.Event()
    allow_get_turn = asyncio.Event()
    terminal_calls: list[dict] = []

    async def blocking_runner(*_args, **_kwargs):
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.set()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", blocking_runner)

    class DispatchStore(FakeDispatchStore):
        heartbeat_calls = 0

        async def claim_chat_spawn_dispatch_for_worker(self, **_kwargs):
            return "claimed"

        async def heartbeat_chat_spawn_dispatch(self, **_kwargs):
            self.heartbeat_calls += 1
            return "renewed" if self.heartbeat_calls == 1 else "cancel_requested"

        async def get_turn(self, _turn_id: str):
            get_turn_started.set()
            await allow_get_turn.wait()
            return type("Turn", (), {"status": "cancelled"})()

        async def mark_chat_spawn_dispatch_terminal(self, **kwargs):
            terminal_calls.append(kwargs)
            dispatch_terminal.set()
            return True

    store = DispatchStore()
    orch = FakeSpawnOrchestrator(store)
    orch.dispatch_owner_id = "worker-a"
    orch._cancel_events = {}

    task = await spawn_turn(orch, FakeTurnReq("s1", "t_remote_cancel"))
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    await asyncio.wait_for(worker_stopped.wait(), timeout=3)
    await asyncio.wait_for(get_turn_started.wait(), timeout=1)
    # The canonical status read is settlement work, never a precondition for
    # stopping a worker after the durable cancel heartbeat.
    assert task.cancelled()
    allow_get_turn.set()
    await asyncio.wait_for(dispatch_terminal.wait(), timeout=1)

    for _attempt in range(100):
        if "t_remote_cancel" not in orch._cancel_events:
            break
        await asyncio.sleep(0.01)
    assert "t_remote_cancel" not in orch._cancel_events
    assert terminal_calls == [
        {
            "turn_id": "t_remote_cancel",
            "owner_id": "worker-a",
            "failed": False,
        },
    ]


@pytest.mark.parametrize("fanout_flag", ["1", "0"])
async def test_real_local_stop_monitor_recovers_missing_wire_immediately(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    fanout_flag: str,
) -> None:
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", fanout_flag)
    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    log_store = TurnLogStore(core_path)
    await log_store.initialize()
    session_id = "s_real_stop_monitor"
    turn_id = "t_real_stop_monitor"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws1",
        user_id="u1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="stop before the worker emits its terminal tail",
    )
    registry = EventBusRegistry(
        default_log_sink=OrderedTurnLogSink(log_store),
    )
    orchestrator = SimpleNamespace(
        event_registry=registry,
        session_store=store,
        dispatch_owner_id="gateway-owner",
        _cancel_events={},
    )
    request = FakeTurnReq(session_id, turn_id)
    admitted_bus = await admit_legacy_chat_execution(orchestrator, request)
    live_subscription = admitted_bus.subscribe().__aiter__()
    live_waiter = asyncio.create_task(live_subscription.__anext__())
    await asyncio.sleep(0)
    worker_started = asyncio.Event()
    worker_stopped = asyncio.Event()

    async def blocking_worker(*_args, **_kwargs) -> None:
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.set()

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", blocking_worker)
    worker = await _real_spawn_turn(
        orchestrator,
        request,
        admitted_legacy_bus=admitted_bus,
    )
    await asyncio.wait_for(worker_started.wait(), timeout=1)

    assert (
        await store.request_cancel_turn(
            team_id="team1",
            user_id="u1",
            cancel_request_id="018f0cc3-e4a2-4b21-a109-stopmonitor01",
            workspace_id="ws1",
            turn_id=turn_id,
            expected_execution_generation=1,
        )
        == "cancelled"
    )
    stop_signalled_at = asyncio.get_running_loop().time()
    orchestrator._cancel_events[turn_id].set()
    await asyncio.wait_for(worker_stopped.wait(), timeout=0.5)
    assert asyncio.get_running_loop().time() - stop_signalled_at < 0.5
    with pytest.raises(asyncio.CancelledError):
        await worker

    monitor_name = f"turn_dispatch_lease:{turn_id}"
    for _attempt in range(200):
        if not any(task.get_name() == monitor_name for task in chat_mod._inflight_tasks):
            break
        await asyncio.sleep(0.01)
    assert not any(task.get_name() == monitor_name for task in chat_mod._inflight_tasks)
    canonical = await store.get_turn(turn_id)
    assert canonical is not None and canonical.status == "cancelled"
    recovered = await log_store.list_events(turn_id)
    assert [event.event_type for event in recovered] == [
        "turn_cancelled",
        "done",
    ]
    assert recovered[-1].payload["status"] == "cancelled"
    assert (
        await store.get_chat_spawn_dispatch_projection_state(
            turn_id=turn_id,
        )
        == "settled"
    )
    with sqlite3.connect(core_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("failed",)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(live_waiter, timeout=0.5)
    producer, _floor = await registry.projection_checkpoint(turn_id)
    assert producer is None
    assert turn_id not in orchestrator._cancel_events


async def test_monitor_and_turn_runner_signal_cancel_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.turn_runner import TurnRunner

    inner_started = asyncio.Event()
    finish_started = asyncio.Event()
    allow_finish = asyncio.Event()
    dispatch_terminal = asyncio.Event()
    finish_calls = 0

    class DispatchStore(FakeDispatchStore):
        heartbeat_calls = 0

        async def claim_chat_spawn_dispatch_for_worker(self, **_kwargs):
            return "claimed"

        async def heartbeat_chat_spawn_dispatch(self, **kwargs):
            if kwargs.get("settling"):
                return "renewed"
            self.heartbeat_calls += 1
            if self.heartbeat_calls == 1:
                return "renewed"
            raise AssertionError("local Stop must not wait for heartbeat")

        async def get_turn(self, _turn_id: str):
            return SimpleNamespace(
                status="cancelled",
                execution_generation=1,
            )

        async def mark_chat_spawn_dispatch_terminal(self, **_kwargs):
            dispatch_terminal.set()
            return True

    store = DispatchStore()
    orchestrator = FakeSpawnOrchestrator(store)
    orchestrator.clarification_store = None
    orchestrator.fact_store = None
    orchestrator.router = None
    orchestrator._terminal_cancel_events = {}
    orchestrator._executing_plans = {}
    runner = TurnRunner(orchestrator)

    async def blocking_inner(_request, _cancel_event):
        inner_started.set()
        await asyncio.Event().wait()

    async def slow_finish(_request):
        nonlocal finish_calls
        finish_calls += 1
        finish_started.set()
        await allow_finish.wait()
        return "cancelled-once"

    runner._run_inner = blocking_inner  # type: ignore[method-assign]
    runner._finish_cancelled = slow_finish  # type: ignore[method-assign]

    async def run_real_boundary(_orchestrator, req, **_kwargs):
        return await runner.run(req)

    monkeypatch.setattr(
        chat_mod,
        "_run_turn_and_cleanup",
        run_real_boundary,
    )
    turn_id = "t_single_cancel_signal"
    worker = await spawn_turn(
        orchestrator,
        FakeTurnReq("s1", turn_id),
    )
    await asyncio.wait_for(inner_started.wait(), timeout=2)

    orchestrator._cancel_events[turn_id].set()
    await asyncio.wait_for(finish_started.wait(), timeout=0.5)
    await asyncio.sleep(0)
    allow_finish.set()

    assert await asyncio.wait_for(worker, timeout=0.5) == "cancelled-once"
    await asyncio.wait_for(dispatch_terminal.wait(), timeout=0.5)
    assert finish_calls == 1
    assert not worker.cancelled()


@pytest.mark.parametrize("canonical_status", ["completed", "running"])
async def test_spawn_turn_task_error_settles_only_canonical_terminal(
    monkeypatch,
    canonical_status: str,
) -> None:
    state_probed = asyncio.Event()
    terminalized = asyncio.Event()
    terminal_calls: list[dict] = []

    async def failing_runner(*_args, **_kwargs):
        raise RuntimeError("worker wrapper failed")

    monkeypatch.setattr(chat_mod, "_run_turn_and_cleanup", failing_runner)

    class DispatchStore(FakeDispatchStore):
        async def claim_chat_spawn_dispatch_for_worker(self, **_kwargs):
            return "claimed"

        async def get_turn(self, _turn_id: str):
            state_probed.set()
            return type("Turn", (), {"status": canonical_status})()

        async def mark_chat_spawn_dispatch_terminal(self, **kwargs):
            terminal_calls.append(kwargs)
            terminalized.set()
            return True

    orch = FakeSpawnOrchestrator(DispatchStore())
    task = await spawn_turn(
        orch,
        FakeTurnReq("s1", f"t_task_error_{canonical_status}"),
    )
    with pytest.raises(RuntimeError, match="worker wrapper failed"):
        await task
    await asyncio.wait_for(state_probed.wait(), timeout=1)

    if canonical_status == "completed":
        await asyncio.wait_for(terminalized.wait(), timeout=1)
        assert terminal_calls == [
            {
                "turn_id": "t_task_error_completed",
                "owner_id": "worker-a",
                "failed": False,
            },
        ]
    else:
        await asyncio.sleep(0)
        assert terminal_calls == []


async def test_spawn_turn_wrapped_durability_abort_keeps_completed_dispatch_active(
    monkeypatch,
) -> None:
    terminal_calls: list[dict] = []

    async def wrapped_durability_failure(*_args, **_kwargs):
        try:
            raise TerminalDurabilityAbort("done persistence failed")
        except TerminalDurabilityAbort as exc:
            raise RuntimeError(
                "legacy_chat_durability_recovery_handoff",
            ) from exc

    monkeypatch.setattr(
        chat_mod,
        "_run_turn_and_cleanup",
        wrapped_durability_failure,
    )

    class DispatchStore(FakeDispatchStore):
        async def claim_chat_spawn_dispatch_for_worker(self, **_kwargs):
            return "claimed"

        async def get_turn(self, _turn_id: str):
            return type("Turn", (), {"status": "completed"})()

        async def mark_chat_spawn_dispatch_terminal(self, **kwargs):
            terminal_calls.append(kwargs)
            return True

    turn_id = "t_wrapped_durability_abort"
    worker = await spawn_turn(
        FakeSpawnOrchestrator(DispatchStore()),
        FakeTurnReq("s1", turn_id),
    )
    with pytest.raises(
        RuntimeError,
        match="legacy_chat_durability_recovery_handoff",
    ) as caught:
        await worker
    assert isinstance(caught.value.__cause__, TerminalDurabilityAbort)

    monitor_name = f"turn_dispatch_lease:{turn_id}"
    for _attempt in range(100):
        if not any(task.get_name() == monitor_name for task in chat_mod._inflight_tasks):
            break
        await asyncio.sleep(0)
    assert not any(task.get_name() == monitor_name for task in chat_mod._inflight_tasks)
    assert terminal_calls == []


async def test_spawn_turn_terminal_log_failure_waits_for_expiry_recovery(
    tmp_path,
    monkeypatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from bowhead_core.events import EventBusRegistry, OrchestratorEvent
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    log_store = TurnLogStore(core_path)
    await log_store.initialize()
    turn_id = "t_terminal_log_failure"
    session_id = "s_terminal_log_failure"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws1",
        user_id="u1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="complete before the terminal log write",
    )

    class _RejectDoneLogStore:
        async def record(self, **kwargs):
            if kwargs["event_type"] == "done":
                return None
            return await log_store.record(**kwargs)

    ordered_sink = OrderedTurnLogSink(_RejectDoneLogStore())  # type: ignore[arg-type]
    registry = EventBusRegistry(default_log_sink=ordered_sink)
    orchestrator = SimpleNamespace(
        event_registry=registry,
        session_store=store,
        dispatch_owner_id="gateway-owner",
        _cancel_events={},
    )
    request = FakeTurnReq(session_id, turn_id)
    admitted_bus = await admit_legacy_chat_execution(orchestrator, request)

    async def complete_then_reject_done(
        _orchestrator,
        req,
        *,
        admitted_legacy_bus,
        **_kwargs,
    ) -> None:
        assert await store.mark_completed(
            turn_id=req.turn_id,
            intent=None,
            final_answer="durable Store answer",
            tokens_input=7,
            tokens_output=11,
            cost_usd=0.25,
        )
        await admitted_legacy_bus.emit(
            OrchestratorEvent(
                event_type="done",
                turn_id=req.turn_id,
                payload={"turn_id": req.turn_id, "status": "completed"},
            )
        )

    monkeypatch.setattr(
        chat_mod,
        "_run_turn_and_cleanup",
        complete_then_reject_done,
    )
    worker = await _real_spawn_turn(
        orchestrator,
        request,
        admitted_legacy_bus=admitted_bus,
    )
    with pytest.raises(
        RuntimeError,
        match="legacy_chat_durability_recovery_handoff",
    ) as worker_failure:
        await worker
    assert isinstance(worker_failure.value.__cause__, TerminalDurabilityAbort)

    monitor_name = f"turn_dispatch_lease:{turn_id}"
    for _attempt in range(100):
        if not any(task.get_name() == monitor_name for task in chat_mod._inflight_tasks):
            break
        await asyncio.sleep(0.01)
    assert not any(task.get_name() == monitor_name for task in chat_mod._inflight_tasks)
    with sqlite3.connect(core_path) as connection:
        assert connection.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("spawned", "gateway-owner")
        connection.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            (
                (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
                turn_id,
            ),
        )
        connection.commit()

    before_recovery = await store.get_turn(turn_id)
    assert before_recovery is not None
    assert await store.recover_expired_chat_spawn_dispatches() == [turn_id]
    after_recovery = await store.get_turn(turn_id)
    assert after_recovery == before_recovery
    assert after_recovery.status == "completed"
    assert after_recovery.final_answer == "durable Store answer"
    events = await log_store.list_events(turn_id)
    assert [event.event_type for event in events] == ["final_answer", "done"]
    assert events[0].payload["text"] == "durable Store answer"
    assert events[0].payload["card_id"] == turn_id
    assert events[1].payload["status"] == "completed"
    assert await store.recover_expired_chat_spawn_dispatches() == []
    events_after_retry = await log_store.list_events(turn_id)
    assert [event.event_type for event in events_after_retry].count("done") == 1
    assert [event.event_type for event in events_after_retry].count("final_answer") == 1
    assert await store.get_chat_spawn_dispatch_projection_state(turn_id=turn_id) == "settled"
    await registry.close(turn_id, expected=admitted_bus)


# ─── spawn_resume_turn / resume_already_in_flight ────────────────────────────


async def test_spawn_failure_handoff_is_idempotent_after_recovery(tmp_path) -> None:
    from bowhead_core.session_store import SessionStore

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    session_id = "s_double_spawn_failure_handoff"
    turn_id = "t_double_spawn_failure_handoff"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws1",
        user_id="u1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="fail before worker start",
    )

    first = await ts_mod.handoff_legacy_spawn_failure_to_recovery(
        store,
        turn_id=turn_id,
        session_id=session_id,
        owner_id="gateway-owner",
        error="turn dispatch failed before worker start",
        error_kind="dispatch_start_failed",
    )
    assert first == "recovered"
    failed = await store.get_turn(turn_id)
    assert failed is not None and failed.status == "failed"
    assert await store.get_active_turn(session_id) is None
    with sqlite3.connect(core_path) as connection:
        assert connection.execute(
            "SELECT state FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("failed",)

    second = await ts_mod.handoff_legacy_spawn_failure_to_recovery(
        store,
        turn_id=turn_id,
        session_id=session_id,
        owner_id="gateway-owner",
        error="duplicate outer handoff",
        error_kind="dispatch_start_failed",
    )
    assert second == "untracked"
    assert await store.get_active_turn(session_id) is None


async def _build_real_paused_resume_store(tmp_path, *, suffix: str, bounded: bool = False):
    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import TurnLogStore

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    session_id = f"s_resume_preworker_{suffix}"
    turn_id = f"t_resume_preworker_{suffix}"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws1",
        user_id="u1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="parent",
        user_source=ChatTurnUserSource(
            turn_id=turn_id,
            session_id=session_id,
            user_id="u1",
            workspace_id="ws1",
            user_text="parent",
            attachment_ids=(),
            attachment_refs=(),
            parts=None,
        )
        if bounded
        else None,
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="pause-owner",
        )
        == "claimed"
    )
    if bounded:
        from bowhead_core.clarification_store import ClarificationStore

        cards = ClarificationStore(core_path)
        card = await cards.put(
            turn_id=turn_id,
            session_id=session_id,
            user_id="u1",
            workspace_id="ws1",
            kind="ambiguous_turn",
            token="",
            rationale="Which period?",
            candidates=[],
            resume_context={"user_text": "parent", "user_id": "u1", "workspace_id": "ws1", "session_id": session_id, "team_id": None},
            bounded_question={"generation": 1, "owner_id": "pause-owner", "questions": [], "fallback": "Which period?"},
        )
        await cards.resolve(card.request_id, chosen_value="2026")
    assert await store.mark_clarification_pending(
        turn_id=turn_id,
        intent="clarify",
    )
    assert (
        await store.materialize_steer_follow_up(
            session_id=session_id,
            current_turn_id=turn_id,
            next_turn_id=f"unused_{turn_id}",
        )
        == []
    )
    log_store = TurnLogStore(core_path)
    await log_store.initialize()
    assert await log_store.record(
        turn_id=turn_id,
        event_type="done",
        payload={
            "turn_id": turn_id,
            "status": "clarification_pending",
            "execution_generation": 1,
            "seq": 1,
            "evt_seq": 1,
        },
    )
    assert await store.mark_chat_spawn_dispatch_terminal(
        turn_id=turn_id,
        owner_id="pause-owner",
    )
    assert await store.get_active_turn(session_id) is None
    return store, session_id, turn_id


async def _assert_real_resume_preworker_failure_is_retryable(
    store,
    *,
    session_id: str,
    turn_id: str,
    clarification_id: str | None = None,
) -> None:
    turn = await store.get_turn(turn_id)
    assert turn is not None
    assert turn.status == "clarification_pending"
    assert await store.get_active_turn(session_id) is None
    with sqlite3.connect(store.db_path) as connection:
        dispatch = connection.execute(
            "SELECT state,lease_kind,lease_execution_generation,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
    assert dispatch == ("terminal", "resume", 2, None)
    assert await store.get_chat_spawn_dispatch_projection_state(turn_id=turn_id) == "settled"

    attempts = await asyncio.gather(
        store.advance_turn_execution_generation(turn_id=turn_id, clarification_id=clarification_id),
        store.advance_turn_execution_generation(turn_id=turn_id, clarification_id=clarification_id),
    )
    assert attempts.count(3) == attempts.count(None) == 1
    advanced = await store.get_turn(turn_id)
    assert advanced is not None
    assert advanced.status == "running"
    with sqlite3.connect(store.db_path) as connection:
        next_dispatch = connection.execute(
            "SELECT state,lease_kind,lease_execution_generation,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
    assert next_dispatch == ("claimed", "resume", 3, None)
    assert await store.rollback_turn_resume_generation(
        turn_id=turn_id,
        expected_execution_generation=3,
        owner_id=None,
    )


@pytest.mark.parametrize("bounded", [False, True])
async def test_real_resume_notice_rejection_restores_paused_retry_state(
    tmp_path,
    monkeypatch,
    bounded,
) -> None:
    from bowhead_core.events import EventBusRegistry

    store, session_id, turn_id = await _build_real_paused_resume_store(
        tmp_path,
        suffix="notice_rejected",
        bounded=bounded,
    )
    request = FakeTurnReq(session_id, turn_id)
    clarification_id = None
    if bounded:
        with sqlite3.connect(store.db_path) as connection:
            clarification_id = connection.execute("SELECT request_id FROM clarification_requests WHERE turn_id=?", (turn_id,)).fetchone()[0]
        request.resume_clarification_id = clarification_id
        request.team_id = None

    async def reject_resume_notice(_event) -> bool:
        return False

    registry = EventBusRegistry(default_log_sink=reject_resume_notice)
    orchestrator = SimpleNamespace(
        event_registry=registry,
        session_store=store,
        dispatch_owner_id="gateway-default",
        _cancel_events={},
    )

    with pytest.raises(
        RuntimeError,
        match="legacy_chat_durability_recovery_handoff",
    ) as rejected:
        await ts_mod.spawn_resume_turn(
            orchestrator,
            request,
            initial_seq=0,
            resume_notice={"request_id": "cr_notice_rejected"},
        )
    assert isinstance(rejected.value.__cause__, TerminalDurabilityAbort)
    await _assert_real_resume_preworker_failure_is_retryable(
        store,
        session_id=session_id,
        turn_id=turn_id,
        clarification_id=clarification_id,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "claimed"),
        ("state", "failed"),
        ("owner_id", "old-worker"),
        ("lease_execution_generation", 1),
        ("lease_kind", "turn"),
        ("session_id", "s_other"),
        ("cancel_requested_at", "2026-10-03T00:00:00+00:00"),
    ],
)
async def test_bounded_resume_retry_requires_exact_settled_preworker_dispatch(tmp_path, monkeypatch, field, value):
    store, _, turn_id = await _build_real_paused_resume_store(tmp_path, suffix="bounded_retry_fence", bounded=True)
    with sqlite3.connect(store.db_path) as connection:
        card_id = connection.execute("SELECT request_id FROM clarification_requests WHERE turn_id=?", (turn_id,)).fetchone()[0]
    assert await store.advance_turn_execution_generation(turn_id=turn_id, clarification_id=card_id) == 2
    assert await store.rollback_turn_resume_generation(turn_id=turn_id, expected_execution_generation=2, owner_id=None)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(f"UPDATE chat_turn_spawn_dispatch SET {field}=? WHERE turn_id=?", (value, turn_id))
    assert await store.advance_turn_execution_generation(turn_id=turn_id, clarification_id=card_id) is None
    turn = await store.get_turn(turn_id)
    assert turn.status == "clarification_pending" and turn.execution_generation == 2


async def test_real_resume_monitor_start_failure_restores_paused_retry_state(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore

    store, session_id, turn_id = await _build_real_paused_resume_store(
        tmp_path,
        suffix="monitor_start_failed",
    )

    log_store = TurnLogStore(store.db_path)
    await log_store.initialize()
    registry = EventBusRegistry(
        default_log_sink=OrderedTurnLogSink(log_store),
    )
    orchestrator = SimpleNamespace(
        event_registry=registry,
        session_store=store,
        dispatch_owner_id="gateway-default",
        _cancel_events={},
    )
    create_task = asyncio.create_task

    def fail_monitor_start(coro, *, name=None, context=None):
        if name == f"turn_dispatch_lease:{turn_id}":
            raise RuntimeError("monitor create_task failed")
        if context is None:
            return create_task(coro, name=name)
        return create_task(coro, name=name, context=context)

    monkeypatch.setattr(ts_mod.asyncio, "create_task", fail_monitor_start)

    with pytest.raises(RuntimeError, match="monitor create_task failed"):
        await ts_mod.spawn_resume_turn(
            orchestrator,
            FakeTurnReq(session_id, turn_id),
            initial_seq=await log_store.max_seq(turn_id),
            resume_notice={"request_id": "cr_monitor_start_failed"},
        )
    events = await log_store.list_events(turn_id)
    assert [event.event_type for event in events] == [
        "done",
        "turn.resumed",
        "done",
    ]
    assert events[0].payload["execution_generation"] == 1
    assert events[1].payload["execution_generation"] == 2
    assert events[2].payload["execution_generation"] == 2
    assert events[2].payload["status"] == "clarification_pending"
    assert [event.payload["evt_seq"] for event in events] == [1, 2, 3]
    replay = chat_mod._replay_rows_for_stream(events)
    assert [event.event_type for event in replay] == ["turn.resumed", "done"]
    assert replay[-1].payload["execution_generation"] == 2
    await _assert_real_resume_preworker_failure_is_retryable(
        store,
        session_id=session_id,
        turn_id=turn_id,
    )


async def test_real_store_idle_pause_can_spawn_and_rollback_resume(
    tmp_path,
    captured_runner,
) -> None:
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.session_store import SessionStore
    from bowhead_core.turn_log_store import TurnLogStore

    async def accept_event(_event) -> bool:
        return True

    core_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    store = SessionStore(core_path)
    await store.initialize()
    session_id = "s_real_idle_resume"
    turn_id = "t_real_idle_resume"
    await store.ensure_session(
        session_id=session_id,
        workspace_id="ws1",
        user_id="u1",
    )
    await store.record_turn(
        turn_id=turn_id,
        session_id=session_id,
        user_text="parent",
    )
    assert (
        await store.claim_chat_spawn_dispatch_for_worker(
            turn_id=turn_id,
            owner_id="pause-owner",
        )
        == "claimed"
    )
    assert await store.mark_clarification_pending(
        turn_id=turn_id,
        intent="clarify",
    )
    assert (
        await store.materialize_steer_follow_up(
            session_id=session_id,
            current_turn_id=turn_id,
            next_turn_id="unused-child",
        )
        == []
    )
    log_store = TurnLogStore(core_path)
    await log_store.initialize()
    assert await log_store.record(
        turn_id=turn_id,
        event_type="done",
        payload={
            "turn_id": turn_id,
            "status": "clarification_pending",
            "execution_generation": 1,
            "seq": 1,
            "evt_seq": 1,
        },
    )
    assert await store.mark_chat_spawn_dispatch_terminal(
        turn_id=turn_id,
        owner_id="pause-owner",
    )
    assert await store.get_active_turn(session_id) is None

    registry = EventBusRegistry(default_log_sink=accept_event)
    orchestrator = SimpleNamespace(
        event_registry=registry,
        session_store=store,
        dispatch_owner_id="gateway-default",
        _cancel_events={},
    )
    spawned = await ts_mod.spawn_resume_turn(
        orchestrator,
        FakeTurnReq(session_id, turn_id),
        initial_seq=0,
    )
    assert spawned.execution_generation == 2
    assert spawned.task is not None
    await spawned.task
    for _attempt in range(10):
        await asyncio.sleep(0)
        if not any(task.get_name() == f"turn_dispatch_lease:{turn_id}" for task in chat_mod._inflight_tasks):
            break

    assert await store.get_active_turn(session_id) == turn_id
    with sqlite3.connect(store.db_path) as connection:
        state, owner_id = connection.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
    assert state == "spawned"
    assert owner_id is not None
    assert await store.rollback_turn_resume_generation(
        turn_id=turn_id,
        expected_execution_generation=2,
        owner_id=owner_id,
    )
    assert await store.get_active_turn(session_id) is None
    await registry.close(turn_id)


class FakeRegistry:
    """Records the bus generation-guard calls in order (close → get_or_create → on_bus_ready)."""

    def __init__(self) -> None:
        self.events: list = []
        self.bus = HealthyPersistBus()

    async def close(self, turn_id: str, *, expected=None) -> None:
        self.events.append(("close", turn_id))

    async def get_or_create(
        self,
        turn_id: str,
        *,
        initial_seq: int = 0,
        exclusive_producer: bool = False,
        persist_before_live: bool = False,
    ):
        assert exclusive_producer is True
        assert persist_before_live is True
        self.events.append(("get_or_create", turn_id, initial_seq))
        return self.bus

    async def projection_checkpoint(self, _turn_id: str):
        return self.bus, 0

    async def drain_turn_persistence(self, _turn_id: str) -> None:
        return None


async def test_spawn_resume_turn_bus_guard_then_spawn(monkeypatch, captured_runner) -> None:
    rec, started = captured_runner
    reg = FakeRegistry()

    def on_ready() -> None:
        reg.events.append(("on_bus_ready",))

    req = FakeTurnReq("s1", "t_resume")
    store = FakeResumeStore()
    spawned = await ts_mod.spawn_resume_turn(
        FakeSpawnOrchestrator(store, reg),
        req,
        user_store="US",
        team_id="o1",
        entitlements="EN",
        initial_seq=7,
        on_bus_ready=on_ready,
    )

    # generation guard order: durable generation → fresh durable bus → hook → spawn.
    assert store.advance_calls == ["t_resume"]
    assert reg.events == [
        ("close", "t_resume"),
        ("get_or_create", "t_resume", 7),
        ("on_bus_ready",),
    ]
    task = spawned.task
    assert spawned.execution_generation == 2
    assert task.get_name() == "turn_resume:t_resume"
    assert task in chat_mod._inflight_tasks

    await started.wait()
    await task
    await asyncio.sleep(0)  # let the done-callback fire

    # resume threads user_store/team_id/entitlements but NOT metrics_log (omitted → None default)
    assert rec["kwargs"] == {
        "user_store": "US",
        "team_id": "o1",
        "metrics_log": None,
        "entitlements": "EN",
        "cleanup_grace_s": 5.0,
    }
    assert task not in chat_mod._inflight_tasks


async def test_spawn_resume_turn_defaults_seq_zero_no_hook(monkeypatch, captured_runner) -> None:
    _, started = captured_runner
    reg = FakeRegistry()
    # Feishu path: no initial_seq (0), no on_bus_ready is fine too (web passes None).
    spawned = await ts_mod.spawn_resume_turn(
        FakeSpawnOrchestrator(FakeResumeStore(), reg),
        FakeTurnReq("s1", "t0"),
        initial_seq=0,
    )
    task = spawned.task
    assert reg.events == [
        ("close", "t0"),
        ("get_or_create", "t0", 0),
    ]
    await started.wait()
    await task


async def test_spawn_resume_turn_rejects_non_pending_before_bus_replacement(
    monkeypatch,
    captured_runner,
) -> None:
    reg = FakeRegistry()
    store = FakeResumeStore(transition=False)

    with pytest.raises(ts_mod.ResumeTurnConflictError):
        await ts_mod.spawn_resume_turn(
            FakeSpawnOrchestrator(store, reg),
            FakeTurnReq("s1", "t_terminal"),
            initial_seq=0,
        )

    assert store.advance_calls == ["t_terminal"]
    assert reg.events == []


async def test_spawn_resume_turn_missing_registry_does_not_advance_generation(
    captured_runner,
) -> None:
    store = FakeResumeStore()
    orchestrator = SimpleNamespace(session_store=store)

    with pytest.raises(
        RuntimeError,
        match="legacy_chat_event_registry_unavailable",
    ):
        await ts_mod.spawn_resume_turn(
            orchestrator,
            FakeTurnReq("s1", "t_resume_registry_missing"),
            initial_seq=0,
        )

    assert store.advance_calls == []
    assert store.rollback_calls == []


async def test_spawn_resume_turn_attaches_to_remote_generation_winner(
    monkeypatch,
    captured_runner,
) -> None:
    reg = FakeRegistry()
    store = FakeResumeStore(transition=False, status="running")
    store.generation = 2
    store.remote_generation = 2

    spawned = await ts_mod.spawn_resume_turn(
        FakeSpawnOrchestrator(store, reg),
        FakeTurnReq("s1", "t_remote_resume"),
        initial_seq=9,
    )

    assert spawned.task is None
    assert spawned.execution_generation == 2
    assert spawned.attached_remote is True
    assert reg.events == []


async def test_spawn_resume_turn_does_not_spawn_when_resume_notice_is_not_durable(
    monkeypatch,
    captured_runner,
) -> None:
    class _RejectingBus:
        _persist_before_live = True

        async def emit(self, _event) -> bool:
            return False

    reg = FakeRegistry()
    reg.bus = _RejectingBus()
    store = FakeResumeStore()

    with pytest.raises(RuntimeError, match="durably accepted"):
        await ts_mod.spawn_resume_turn(
            FakeSpawnOrchestrator(store, reg),
            FakeTurnReq("s1", "t_notice_rejected"),
            initial_seq=0,
            resume_notice={"request_id": "cr_1", "chosen_values": ["A"]},
        )

    assert store.advance_calls == ["t_notice_rejected"]
    assert store.rollback_calls == [
        {
            "turn_id": "t_notice_rejected",
            "expected_execution_generation": 2,
            "owner_id": store.claim_calls[0]["owner_id"],
        }
    ]
    assert reg.events[-1] == ("close", "t_notice_rejected")
    assert not any(task.get_name() == "turn_resume:t_notice_rejected" for task in chat_mod._inflight_tasks)


async def test_spawn_resume_turn_closes_bus_even_when_failure_persist_raises(
    monkeypatch,
    captured_runner,
) -> None:
    class _RejectingBus:
        _persist_before_live = True

        async def emit(self, _event) -> bool:
            return False

    class _FailingStore(FakeResumeStore):
        async def rollback_turn_resume_generation(self, **kwargs) -> bool:
            self.rollback_calls.append(kwargs)
            raise RuntimeError("db unavailable")

    reg = FakeRegistry()
    reg.bus = _RejectingBus()

    with pytest.raises(RuntimeError, match="durably accepted"):
        await ts_mod.spawn_resume_turn(
            FakeSpawnOrchestrator(_FailingStore(), reg),
            FakeTurnReq("s1", "t_notice_db_failure"),
            initial_seq=0,
            resume_notice={"request_id": "cr_2", "chosen_values": ["B"]},
        )

    assert reg.events[-1] == ("close", "t_notice_db_failure")


async def test_spawn_resume_turn_hook_failure_is_retryable_and_closes_bus(
    monkeypatch,
    captured_runner,
) -> None:
    reg = FakeRegistry()
    store = FakeResumeStore()

    def _failing_hook() -> None:
        raise RuntimeError("mirror failed")

    with pytest.raises(RuntimeError, match="mirror failed"):
        await ts_mod.spawn_resume_turn(
            FakeSpawnOrchestrator(store, reg),
            FakeTurnReq("s1", "t_hook_failure"),
            initial_seq=0,
            on_bus_ready=_failing_hook,
        )

    assert len(store.rollback_calls) == 2
    assert store.rollback_calls[0]["owner_id"] is not None
    assert store.rollback_calls[1]["owner_id"] is None
    assert reg.events[-1] == ("close", "t_hook_failure")


async def test_spawn_resume_cleanup_survives_repeated_cancellation() -> None:
    rollback_started = asyncio.Event()
    allow_rollback = asyncio.Event()
    rollback_completed = asyncio.Event()
    rollback_cancelled = False

    class BlockingRollbackStore(FakeResumeStore):
        async def rollback_turn_resume_generation(self, **kwargs) -> bool:
            nonlocal rollback_cancelled
            self.rollback_calls.append(kwargs)
            rollback_started.set()
            try:
                await allow_rollback.wait()
            except asyncio.CancelledError:
                rollback_cancelled = True
                raise
            self.lease_state = "terminal"
            self.lease_owner = None
            self.status = "clarification_pending"
            rollback_completed.set()
            return True

    class ExactRegistry:
        def __init__(self) -> None:
            self.old_bus = HealthyPersistBus()
            self.new_bus = HealthyPersistBus()
            self.current = self.old_bus
            self.closed: list[object] = []

        async def projection_checkpoint(self, _turn_id: str):
            return self.current, 0

        async def close(self, _turn_id: str, *, expected=None) -> None:
            assert expected is self.current
            self.closed.append(expected)
            self.current = None

        async def get_or_create(self, _turn_id: str, **kwargs):
            assert kwargs["exclusive_producer"] is True
            assert kwargs["persist_before_live"] is True
            self.current = self.new_bus
            return self.new_bus

    registry = ExactRegistry()
    store = BlockingRollbackStore()

    def fail_after_bus_ready() -> None:
        raise RuntimeError("mirror failed after resume admission")

    caller = asyncio.create_task(
        ts_mod.spawn_resume_turn(
            FakeSpawnOrchestrator(store, registry),
            FakeTurnReq("s1", "t_resume_double_cancel"),
            initial_seq=0,
            on_bus_ready=fail_after_bus_ready,
        ),
    )
    await asyncio.wait_for(rollback_started.wait(), timeout=1)
    caller.cancel("first shutdown signal")
    await asyncio.sleep(0)
    caller.cancel("second shutdown signal")
    await asyncio.sleep(0)
    assert not rollback_cancelled
    assert registry.closed == [registry.old_bus]

    allow_rollback.set()
    with pytest.raises(
        RuntimeError,
        match="mirror failed after resume admission",
    ):
        await asyncio.wait_for(caller, timeout=1)
    assert rollback_completed.is_set()
    assert not rollback_cancelled
    assert registry.closed == [registry.old_bus, registry.new_bus]


async def test_spawn_resume_turn_bus_admission_failure_is_retryable(
    monkeypatch,
    captured_runner,
) -> None:
    class _UpgradeFailRegistry(FakeRegistry):
        async def get_or_create(self, turn_id: str, **_kwargs):
            self.events.append(("get_or_create", turn_id, 0))
            raise RuntimeError("upgrade failed")

    reg = _UpgradeFailRegistry()
    store = FakeResumeStore()

    with pytest.raises(RuntimeError, match="upgrade failed"):
        await ts_mod.spawn_resume_turn(
            FakeSpawnOrchestrator(store, reg),
            FakeTurnReq("s1", "t_upgrade_failure"),
            initial_seq=0,
        )

    assert len(store.rollback_calls) == 2
    assert store.rollback_calls[0]["owner_id"] is not None
    assert store.rollback_calls[1]["owner_id"] is None
    assert reg.events == [
        ("close", "t_upgrade_failure"),
        ("get_or_create", "t_upgrade_failure", 0),
    ]


async def test_spawn_resume_turn_preworker_failure_can_retry(
    monkeypatch,
    captured_runner,
) -> None:
    class _RejectingBus:
        _persist_before_live = True

        async def emit(self, _event) -> bool:
            return False

    class _AcceptingBus:
        _persist_before_live = True

        async def emit(self, _event) -> bool:
            return True

    reg = FakeRegistry()
    reg.bus = _RejectingBus()
    store = FakeResumeStore()
    orchestrator = FakeSpawnOrchestrator(store, reg)

    with pytest.raises(RuntimeError, match="durably accepted"):
        await ts_mod.spawn_resume_turn(
            orchestrator,
            FakeTurnReq("s1", "t_retryable_resume"),
            initial_seq=0,
            resume_notice={"request_id": "cr_retry"},
        )
    assert store.status == "clarification_pending"

    reg.bus = _AcceptingBus()
    spawned = await ts_mod.spawn_resume_turn(
        orchestrator,
        FakeTurnReq("s1", "t_retryable_resume"),
        initial_seq=1,
        resume_notice={"request_id": "cr_retry"},
    )
    assert spawned.execution_generation == 3
    assert spawned.task is not None
    await spawned.task


async def test_resume_already_in_flight_detects_live_then_clears() -> None:
    turn_id = "t_dedup"
    ev = asyncio.Event()

    async def _hold() -> None:
        await ev.wait()

    task = asyncio.create_task(_hold(), name=f"turn_resume:{turn_id}")
    chat_mod._inflight_tasks.add(task)
    try:
        assert ts_mod.resume_already_in_flight(turn_id) is True
        assert ts_mod.resume_already_in_flight("other_turn") is False  # different turn_id
    finally:
        ev.set()
        await task
        chat_mod._inflight_tasks.discard(task)
    # a completed task no longer counts as in-flight
    assert ts_mod.resume_already_in_flight(turn_id) is False


# ─── build_resume_turn_request (FB5 — neutral resume reconstruction) ──────────


@dataclass
class _FakeClarReq:
    turn_id: str
    resume_context: object
    token: str = ""
    chosen_value: str | None = None
    chosen_values: list | None = None
    workspace_id: str = ""
    user_id: str = ""
    kind: str = ""
    candidates: tuple = ()


def test_build_resume_token_uses_glossary_no_merge() -> None:
    req = _FakeClarReq(
        turn_id="t1",
        token="GMV",
        chosen_value="毛利",
        resume_context={"user_text": "看一下 GMV", "session_id": "s1", "team_id": "o1", "workspace_id": "ws1"},
    )
    tr = build_resume_turn_request(req, datarc_client="DC", datarc_unavailable=False, vector_client="VC")
    assert tr is not None
    assert tr.text == "看一下 GMV"  # token present → no append-merge
    assert tr.resume_glossary == {"GMV": "毛利"}
    assert tr.turn_id == "t1" and tr.resume_turn_id == "t1"
    assert tr.session_id == "s1" and tr.workspace_id == "ws1" and tr.team_id == "o1"
    assert tr.datarc_client == "DC" and tr.vector_client == "VC"


def test_build_resume_empty_token_appends_all_answers() -> None:
    req = _FakeClarReq(
        turn_id="t1",
        token="",
        chosen_values=["北京", "今年至今"],
        resume_context={"user_text": "帮我生成一个分析ppt", "session_id": "s1"},
    )
    tr = build_resume_turn_request(req)
    assert tr is not None
    # B1: original intent + ALL answers appended (not the bare first pick)
    assert "帮我生成一个分析ppt" in tr.text and "北京" in tr.text and "今年至今" in tr.text
    assert tr.resume_glossary is None


def test_build_resume_whole_skip_never_leaks_sentinel() -> None:
    req = _FakeClarReq(
        turn_id="t1",
        kind="options",
        chosen_value="__bowhead_clarification_skip__",
        resume_context={"user_text": "分析销售", "session_id": "s1"},
    )

    tr = build_resume_turn_request(req)

    assert tr is not None
    assert "用户跳过澄清" in tr.text
    assert "__bowhead_clarification_skip__" not in tr.text


def test_build_resume_per_question_skip_uses_human_semantics() -> None:
    req = _FakeClarReq(
        turn_id="t1",
        kind="options",
        chosen_value="时间范围: __bowhead_question_skip__",
        chosen_values=["时间范围: __bowhead_question_skip__"],
        resume_context={
            "user_text": "分析销售",
            "session_id": "s1",
            "clarification_answers": [
                {
                    "question": "时间范围",
                    "source": "skip",
                    "skipped": True,
                }
            ],
        },
    )

    tr = build_resume_turn_request(req)

    assert tr is not None
    assert "时间范围: 用户跳过该问题" in tr.text
    assert "__bowhead_question_skip__" not in tr.text


def test_build_resume_tool_context_skip_keeps_catalog_and_switch_instruction() -> None:
    req = _FakeClarReq(
        turn_id="t1",
        kind="tool_context_select",
        chosen_value="__bowhead_clarification_skip__",
        candidates=(SimpleNamespace(label="销售域", desc="销售数据"),),
        resume_context={"user_text": "分析销售", "session_id": "s1"},
    )

    tr = build_resume_turn_request(req)

    assert tr is not None
    assert "tool_switch_context" in tr.text
    assert "销售域（销售数据）" in tr.text
    assert "__bowhead_clarification_skip__" not in tr.text


def test_build_resume_none_when_not_resumable() -> None:
    assert build_resume_turn_request(_FakeClarReq(turn_id="t1", resume_context=None)) is None
    # missing session_id
    assert build_resume_turn_request(_FakeClarReq(turn_id="t1", resume_context={"user_text": "x"})) is None
    # missing user_text
    assert build_resume_turn_request(_FakeClarReq(turn_id="t1", resume_context={"session_id": "s1"})) is None


@pytest.mark.parametrize("version", [3, "2", True, None, -1, 2.0])
def test_build_resume_rejects_unsupported_snapshot_version(version) -> None:
    request = _FakeClarReq(turn_id="t_version", resume_context={"user_text": "Continue", "session_id": "s_version", "__schema_version": version})
    assert build_resume_turn_request(request) is None


def test_document_checkpoint_cannot_fall_back_to_an_ordinary_resume_request() -> None:
    import json
    from pathlib import Path

    snapshot = json.loads((Path(__file__).resolve().parents[3] / "contracts/chat-documents/planning-resume-v3.json").read_text())
    request = _FakeClarReq(turn_id=snapshot["turn_id"], resume_context=snapshot)
    assert build_resume_turn_request(request) is None


# ─────────── Batch 7-F: clarification resume inherits this turn's language ───


def test_resume_inherits_the_turn_language_from_the_snapshot() -> None:
    """恢复的是**同一个 turn**，就该继续用那一轮定下的语言。

    修前这里硬写 `LanguageContext.default()` ⇒ 英文用户走一次 HITL 澄清，恢复的那一轮
    整轮掉回中文（README §6 裂缝 1）。飞书与 terminal 审批执行都走这个构造器。
    """
    from bowhead_core.language_context import RESUME_LANGUAGE_KEY

    req = _FakeClarReq(
        turn_id="t1",
        resume_context={
            "user_text": "Show me revenue by province",
            "session_id": "s1",
            RESUME_LANGUAGE_KEY: {
                "ui_language": "zh-CN",
                "output_language": "en-US",
                "data_language": "zh-CN",
                "week_start_day": "monday",
            },
        },
    )
    tr = build_resume_turn_request(req)
    assert tr is not None
    assert tr.language_context.output_language == "en-US"
    assert tr.language_context.ui_language == "zh-CN"


def test_resume_without_a_language_snapshot_uses_the_product_default() -> None:
    """A legacy snapshot without the key falls back to LanguageContext.default() (English)."""
    from bowhead_core.language_context import LanguageContext

    req = _FakeClarReq(turn_id="t1", resume_context={"user_text": "看一下营收", "session_id": "s1"})
    tr = build_resume_turn_request(req)
    assert tr is not None
    assert tr.language_context == LanguageContext.default()
    assert tr.language_context.output_language == "en-US"
