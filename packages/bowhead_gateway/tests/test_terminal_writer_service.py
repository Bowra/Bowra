from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.terminal_generation_bus import GenerationEventBusRegistry
from bowhead_core.terminal_protocol import (
    AdmissionContext,
    GenerationClaimContext,
    RecoveryWriterFence,
    TerminalAdmissionRequest,
    TerminalProtocolMalformedError,
    TerminalWriterLease,
)
from bowhead_core.terminal_protocol_store import (
    AdmissionDisposition,
    GenerationClaimDisposition,
    TerminalProtocolStore,
)
from bowhead_core.terminal_runtime_factory import (
    TerminalRuntimeFactory,
    TerminalRuntimeRegistry,
    terminal_auth_resolve_context_values,
)
from bowhead_gateway.terminal_writer_service import TerminalWriterLeaseService


def _expires() -> str:
    return (datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat()


def _expired() -> str:
    return (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()


class _Store:
    def __init__(self) -> None:
        self.epoch = 0
        self.fail_acquire = False
        self.fail_heartbeat = False
        self.expire_heartbeat = False
        self.fail_drain = False
        self.drains = 0
        self.releases = 0
        self.recovery_releases = 0

    async def acquire_terminal_writer_lease(self, **kwargs) -> TerminalWriterLease:
        if self.fail_acquire:
            raise RuntimeError("acquire failed")
        self.epoch += 1
        return TerminalWriterLease(
            owner_instance_id=kwargs["owner_instance_id"],
            writer_lease_epoch=self.epoch,
            writer_admission_epoch=self.epoch,
            rollout_config_generation=kwargs["rollout_config_generation"],
            lease_expires_at=_expires(),
            admission_state="open",
        )

    async def acquire_recovery_writer_fence(self, **kwargs) -> RecoveryWriterFence:
        self.epoch += 1
        return RecoveryWriterFence(
            owner_instance_id=kwargs["owner_instance_id"],
            writer_lease_epoch=self.epoch,
            writer_admission_epoch=self.epoch,
            rollout_config_generation=kwargs["rollout_config_generation"],
            lease_expires_at=_expires(),
        )

    async def heartbeat_terminal_writer_lease(
        self,
        lease: TerminalWriterLease,
    ) -> TerminalWriterLease:
        if self.fail_heartbeat:
            raise RuntimeError("lease lost")
        if self.expire_heartbeat:
            return replace(lease, lease_expires_at=_expired())
        return replace(lease, lease_expires_at=_expires())

    async def heartbeat_recovery_writer_fence(
        self,
        fence: RecoveryWriterFence,
    ) -> RecoveryWriterFence:
        if self.fail_heartbeat:
            raise RuntimeError("fence lost")
        return replace(fence, lease_expires_at=_expires())

    async def drain_terminal_writer_lease(
        self,
        lease: TerminalWriterLease,
    ) -> TerminalWriterLease:
        if self.fail_drain:
            raise RuntimeError("drain failed")
        self.drains += 1
        return replace(
            lease,
            writer_admission_epoch=lease.writer_admission_epoch + 1,
            lease_expires_at=_expires(),
            admission_state="draining",
        )

    async def release_terminal_writer_lease(self, _lease: TerminalWriterLease) -> bool:
        self.releases += 1
        return True

    async def release_recovery_writer_fence(self, _fence: RecoveryWriterFence) -> bool:
        self.recovery_releases += 1
        return True


def _service(
    store: _Store,
    *,
    fresh: bool = True,
    active_runtime_count: Callable[[], int] | None = None,
    heartbeat_interval_seconds: float = 10.0,
) -> TerminalWriterLeaseService:
    return TerminalWriterLeaseService(
        store=store,
        owner_instance_id="gateway_1",
        rollout_config_generation="terminal-v1",
        fresh_admission_enabled=fresh,
        active_runtime_count=active_runtime_count,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )


@pytest.mark.anyio
async def test_writer_requires_startup_recovery_before_fresh_admission() -> None:
    store = _Store()
    service = _service(store)

    assert await service.start() is True
    assert service.ready is True
    assert service.fresh_admission_ready is False
    assert service.mark_recovery_ready() is True
    assert service.fresh_admission_ready is True

    service.mark_recovery_pending()
    assert service.fresh_admission_ready is False
    await service.close(worker_timeout_seconds=0)


@pytest.mark.anyio
async def test_writer_acquisition_error_stays_closed_and_can_retry() -> None:
    store = _Store()
    store.fail_acquire = True
    service = _service(store)

    with pytest.raises(RuntimeError, match="acquire failed"):
        await service.start()
    assert service.ready is False
    assert service.fresh_admission_ready is False

    store.fail_acquire = False
    assert await service.start() is True
    assert service.fresh_admission_ready is False
    assert service.mark_recovery_ready() is True
    await service.close(worker_timeout_seconds=0)


@pytest.mark.anyio
async def test_begin_drain_is_idempotent_and_prevents_reacquisition() -> None:
    store = _Store()
    service = _service(store)
    assert await service.start() is True
    assert service.mark_recovery_ready() is True

    await service.begin_drain()
    await service.begin_drain()

    assert service.draining is True
    assert service.fresh_admission_ready is False
    assert isinstance(service.fence, TerminalWriterLease)
    assert service.fence.admission_state == "draining"
    assert store.drains == 1
    assert await service.start() is False
    assert await service.ensure_writer_ready() is True

    await service.close(worker_timeout_seconds=0)
    assert await service.ensure_writer_ready() is False
    assert store.releases == 1


@pytest.mark.anyio
async def test_heartbeat_loss_revokes_ready_until_new_lease_is_recovered() -> None:
    store = _Store()
    service = _service(store, heartbeat_interval_seconds=0.01)
    assert await service.start() is True
    assert service.mark_recovery_ready() is True

    store.fail_heartbeat = True
    await asyncio.sleep(0.03)
    assert service.ready is False
    assert service.fresh_admission_ready is False

    store.fail_heartbeat = False
    assert await service.start() is True
    assert service.ready is True
    assert service.fresh_admission_ready is False
    assert service.mark_recovery_ready() is True
    await service.close(worker_timeout_seconds=0)


@pytest.mark.anyio
async def test_expired_heartbeat_snapshot_revokes_ready_and_is_reacquired() -> None:
    store = _Store()
    service = _service(store, heartbeat_interval_seconds=0.01)
    assert await service.start() is True
    assert service.mark_recovery_ready() is True

    store.expire_heartbeat = True
    await asyncio.sleep(0.03)
    assert service.ready is False
    assert service.fresh_admission_ready is False
    with pytest.raises(RuntimeError, match="terminal_protocol_writer_not_ready"):
        service.writer_snapshot()

    store.expire_heartbeat = False
    assert await service.start() is True
    assert service.fresh_admission_ready is False
    assert service.mark_recovery_ready() is True
    await service.close(worker_timeout_seconds=0)


@pytest.mark.anyio
async def test_drain_failure_closes_process_gate_and_prevents_reacquisition() -> None:
    store = _Store()
    service = _service(store)
    assert await service.start() is True
    assert service.mark_recovery_ready() is True
    store.fail_drain = True

    with pytest.raises(RuntimeError, match="drain failed"):
        await service.begin_drain()

    assert service.draining is True
    assert service.fresh_admission_ready is False
    assert await service.start() is False
    store.fail_drain = False
    await service.close(worker_timeout_seconds=0)


@pytest.mark.anyio
async def test_close_waits_for_observed_worker_without_cancelling_it() -> None:
    store = _Store()
    service = _service(store)
    assert await service.start() is True
    assert service.mark_recovery_ready() is True
    release_worker = asyncio.Event()

    async def worker() -> object:
        await release_worker.wait()
        return None

    task = asyncio.create_task(worker())
    service.observe_worker_task(task)
    closing = asyncio.create_task(service.close(worker_timeout_seconds=0.5))
    await asyncio.sleep(0.02)
    assert closing.done() is False

    release_worker.set()
    await closing
    assert task.done() is True
    assert task.cancelled() is False
    assert store.drains == 1
    assert store.releases == 1


@pytest.mark.anyio
async def test_close_also_waits_for_worker_observed_during_drain() -> None:
    store = _Store()
    service = _service(store)
    assert await service.start() is True
    first_release = asyncio.Event()
    late_release = asyncio.Event()

    async def worker(release: asyncio.Event) -> object:
        await release.wait()
        return None

    first = asyncio.create_task(worker(first_release))
    service.observe_worker_task(first)
    closing = asyncio.create_task(service.close(worker_timeout_seconds=0.5))
    await asyncio.sleep(0.02)
    late = asyncio.create_task(worker(late_release))
    service.observe_worker_task(late)
    first_release.set()
    await asyncio.sleep(0.02)
    assert closing.done() is False

    late_release.set()
    await closing
    assert first.cancelled() is False
    assert late.cancelled() is False
    assert store.releases == 1


@pytest.mark.anyio
async def test_close_timeout_releases_lease_without_cancelling_worker() -> None:
    store = _Store()
    service = _service(store)
    assert await service.start() is True
    blocker = asyncio.Event()

    async def worker() -> object:
        await blocker.wait()
        return None

    task = asyncio.create_task(worker())
    service.observe_worker_task(task)

    await service.close(worker_timeout_seconds=0.01)

    assert task.done() is False
    assert store.releases == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_disabled_writer_releases_idle_recovery_fence() -> None:
    store = _Store()
    service = _service(store, fresh=False)

    assert await service.start() is True
    assert service.ready is True
    assert service.mark_recovery_ready() is False
    assert service.fresh_admission_ready is False
    assert await service.release_recovery_if_idle() is True
    assert service.fence is None
    assert store.recovery_releases == 1


@pytest.mark.anyio
async def test_recovery_writer_waits_for_attached_runtime_before_release() -> None:
    store = _Store()
    runtime_count = 1
    service = _service(
        store,
        fresh=False,
        active_runtime_count=lambda: runtime_count,
    )
    assert await service.start() is True

    assert await service.release_recovery_if_idle() is False
    assert service.fence is not None

    runtime_count = 0
    assert await service.release_recovery_if_idle() is True
    assert service.fence is None


@pytest.mark.integration
@pytest.mark.anyio
async def test_disabled_writer_real_store_steer_fences_recovery_epoch(
    tmp_path,
) -> None:
    db_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{db_path}") == tuple(TARGET_FILES)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active','t')",
        )
        connection.execute(
            """
            INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at,revision)
            VALUES ('ws_1','team_1','Space','active','t','t',1)
            """,
        )
        connection.execute(
            """
            INSERT INTO sessions(
              id,workspace_id,user_id,title,started_at,last_active_at,status,
              cost_usd,tokens_total,intents
            ) VALUES ('s_1','ws_1','u_1','Session','t','t','active',0,0,'[]')
            """,
        )
        connection.commit()

    store = TerminalProtocolStore(str(db_path))
    fresh_lease = await store.acquire_terminal_writer_lease(
        owner_instance_id="fresh_gateway",
        rollout_config_generation="terminal-v1",
    )
    assert fresh_lease is not None
    auth = await store.ensure_auth_scope_epochs(
        terminal_auth_resolve_context_values(
            team_id="team_1",
            session_id="s_1",
            user_id="u_1",
            resource_ref="t_1",
        )
    )
    admitted = await store.admit_or_steer_v1(
        TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id="team_1",
                session_id="s_1",
                request_id="start_1",
                surface="web",
                cohort="all;config=terminal-v1",
                actor="u_1",
                writer_instance_id=fresh_lease.owner_instance_id,
                writer_lease_epoch=fresh_lease.writer_lease_epoch,
                writer_admission_epoch=fresh_lease.writer_admission_epoch,
                rollout_config_generation=(fresh_lease.rollout_config_generation),
                auth_fence_vector_digest=auth.auth_fence_vector_digest,
            ),
            turn_id="t_1",
            user_text="hello",
            execution_intent={"text": "hello"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
            allow_steer=False,
        )
    )
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    assert admitted.scope is not None
    assert await store.release_terminal_writer_lease(fresh_lease) is True

    service = TerminalWriterLeaseService(
        store=store,
        owner_instance_id="recovery_gateway",
        rollout_config_generation="terminal-v1",
        fresh_admission_enabled=False,
        heartbeat_interval_seconds=60,
    )
    assert await service.start() is True
    recovery_fence = service.fence
    assert isinstance(recovery_fence, RecoveryWriterFence)
    claimed = await store.claim_generation(
        GenerationClaimContext(
            scope=admitted.scope,
            expected_generation_row_version=1,
            expected_session_claim_epoch=0,
            writer_lease_epoch=recovery_fence.writer_lease_epoch,
            owner_instance_id=recovery_fence.owner_instance_id,
            require_open_writer=False,
        )
    )
    assert claimed.disposition is GenerationClaimDisposition.CLAIMED

    recovery: list[tuple[object, str]] = []

    async def trigger_recovery(scope, reason: str) -> None:
        recovery.append((scope, reason))

    factory = TerminalRuntimeFactory(
        store,
        GenerationEventBusRegistry(),
        TerminalRuntimeRegistry(),
        service.writer_snapshot(),
        writer_snapshot_provider=service.writer_snapshot,
        writer_ready=service.ensure_writer_ready,
        recovery_trigger=trigger_recovery,
    )
    steer = {
        "team_id": "team_1",
        "session_id": "s_1",
        "user_id": "u_1",
        "expected_turn_id": "t_1",
        "expected_execution_generation": 1,
        "request_id": "act_steer_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "user_msg_id": "u_r_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "user_text": "continue",
        "workspace_id": "ws_1",
        "auth_fence_vector_digest": auth.auth_fence_vector_digest,
        "presentation_locale": "zh-CN",
    }
    try:
        first, second = await asyncio.gather(
            factory.steer_web_turn(**steer),
            factory.steer_web_turn(**steer),
        )
        assert {first.disposition, second.disposition} == {
            AdmissionDisposition.STEERED,
            AdmissionDisposition.STEER_REPLAY,
        }
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT steer_buffer_revision FROM sessions WHERE id='s_1'",
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM turn_event_proposals WHERE event_type='emoji_ack'",
            ).fetchone() == (1,)

        assert await service.release_recovery_if_idle() is True
        stale_epoch_steer = {
            **steer,
            "request_id": "act_steer_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "user_msg_id": "u_r_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        }
        with pytest.raises(
            TerminalProtocolMalformedError,
            match="ownership fence changed",
        ):
            await factory.steer_web_turn(**stale_epoch_steer)
        assert recovery == [
            (admitted.scope, "steer_admission_unconfirmed"),
        ]
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT steer_buffer_revision FROM sessions WHERE id='s_1'",
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM turn_event_proposals WHERE event_type='emoji_ack'",
            ).fetchone() == (1,)
    finally:
        await service.close(worker_timeout_seconds=0)
