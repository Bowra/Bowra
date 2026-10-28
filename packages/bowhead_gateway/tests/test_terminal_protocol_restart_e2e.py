"""Real Gateway lifecycle restart coverage for Terminal Protocol v1."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import bowhead_gateway.terminal_protocol_rollout as terminal_rollout
import httpx2
import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core._db import open_shared_ctx
from bowhead_core.terminal_protocol import (
    AdmissionContext,
    TerminalAdmissionRequest,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _enable_dormant_terminal_writer_for_protocol_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        terminal_rollout,
        "_FRESH_TERMINAL_V1_ADMISSION_RELEASED",
        True,
    )


@pytest.fixture(autouse=True)
def _lifespan_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # lifespan derives artifact-fetch key from BOWHEAD_JWT_SECRET, rejecting startup without it; no host env/.env dependency.
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-terminal-restart-jwt-secret-32bytes")


async def _deploy_startup_cutover(db_path) -> None:
    """复刻 deploy/start-app.sh 在 uvicorn 之前做的一次性 Agent 市场 cutover。

    /readyz 自 4906143b9 起把 ``agent_marketplace`` 计入就绪判定，而 cutover 只由启动脚本
    （``bowhead admin ensure-agent-marketplace-cutover``）完成、lifespan 不做；不补这一步
    真实 gateway 恒为 503 not_ready。
    """
    from bowhead_core.agent_assignment_store import AgentAssignmentStore

    await AgentAssignmentStore(db_path).ensure_marketplace_cutover(source_build="test")


@pytest.mark.integration
async def test_retired_writer_ignores_stale_env_but_keeps_recovery_lifecycle(
    tmp_path,
    monkeypatch,
) -> None:
    """A legacy env value cannot revive fresh admission after an upgrade."""
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{db_path}") == tuple(TARGET_FILES), "迁移未跑到 head"
    await _deploy_startup_cutover(db_path)
    monkeypatch.setenv(
        "BOWHEAD_DB_URL",
        f"sqlite+aiosqlite:///{db_path}",
    )
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setenv("BOWHEAD_ASSEMBLY_ROUTER", "mock")
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "1")

    from bowhead_gateway.main import create_app

    monkeypatch.setattr(
        terminal_rollout,
        "_FRESH_TERMINAL_V1_ADMISSION_RELEASED",
        False,
    )

    app = create_app()
    async with app.router.lifespan_context(app):
        assert app.state.terminal_writer_service.fresh_admission_ready is False
        assert app.state.terminal_runtime_factory is not None
        assert app.state.terminal_recovery_processor is not None
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://gateway",
        ) as client:
            ready = await client.get("/readyz")

        assert ready.status_code == 200, ready.text
        checks = ready.json()["checks"]
        assert checks["durable_chat_execution_config"] == "ready"
        assert checks["terminal_writer"] == "inactive"
        assert checks["terminal_recovery"] == "ready"


@pytest.mark.integration
@pytest.mark.parametrize("with_draft_target", [False, True])
async def test_gateway_restart_converges_kill_after_open_without_runner(
    tmp_path,
    monkeypatch,
    with_draft_target,
) -> None:
    """Two real lifespans share one DB; boot two must recover boot one's gap."""

    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{db_path}") == tuple(TARGET_FILES), "迁移未跑到 head"
    await _deploy_startup_cutover(db_path)
    monkeypatch.setenv(
        "BOWHEAD_DB_URL",
        f"sqlite+aiosqlite:///{db_path}",
    )
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setenv("BOWHEAD_ASSEMBLY_ROUTER", "mock")
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "rollout")
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1_PCT", "100")

    from bowhead_core.terminal_protocol_store import AdmissionDisposition, TerminalProtocolStore
    from bowhead_gateway.main import create_app

    first = create_app()
    async with first.router.lifespan_context(first):
        assert first.state.terminal_writer_service.fresh_admission_ready is True
        transport = httpx2.ASGITransport(app=first)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://gateway",
        ) as client:
            ready = await client.get("/readyz")
        assert ready.status_code == 200, ready.text
        assert ready.json()["checks"]["durable_chat_execution_config"] == ("ready")
        assert ready.json()["checks"]["terminal_writer"] == "ready"
        assert ready.json()["checks"]["terminal_recovery"] == "ready"
        # Keep the event loop schedulable while default-on startup maintenance
        # briefly owns SQLite's single-writer lock. A synchronous sqlite3 write
        # here can block the loop that must let the maintenance task commit.
        async with open_shared_ctx(str(db_path)) as connection:
            await connection.execute(
                """
                INSERT INTO teams(id,name,status,created_at)
                VALUES ('team_restart','Restart Team','active','t')
                """
            )
            await connection.execute(
                """
                INSERT INTO workspaces(
                  id,team_id,name,status,created_at,updated_at,revision
                ) VALUES (
                  'ws_restart','team_restart','Restart Space','active',
                  't','t',1
                )
                """
            )
            await connection.execute(
                """
                INSERT INTO sessions(
                  id,workspace_id,user_id,title,started_at,last_active_at,
                  status,cost_usd,tokens_total,intents
                ) VALUES (
                  's_restart','ws_restart','u_restart','Restart','t','t',
                  'active',0,0,'[]'
                )
                """
            )
            await connection.commit()

        factory = first.state.terminal_runtime_factory
        writer = first.state.terminal_writer_service.writer_snapshot()
        auth_digest = await factory.prepare_web_auth_fence(
            team_id="team_restart",
            session_id="s_restart",
            user_id="u_restart",
        )
        crash_clock = datetime.now(tz=UTC) - timedelta(minutes=5)
        seeding_store = TerminalProtocolStore(
            str(db_path),
            _clock=lambda: crash_clock,
        )
        admitted = await seeding_store.admit_or_steer_v1(
            TerminalAdmissionRequest(
                context=AdmissionContext(
                    team_id="team_restart",
                    session_id="s_restart",
                    request_id="req_restart",
                    surface="web",
                    cohort="all;config=terminal-v1",
                    actor="u_restart",
                    writer_instance_id=writer.instance_id,
                    writer_lease_epoch=writer.lease_epoch,
                    writer_admission_epoch=writer.admission_epoch,
                    rollout_config_generation=(writer.rollout_config_generation),
                    auth_fence_vector_digest=auth_digest,
                ),
                turn_id="t_restart",
                user_text="restart me",
                execution_intent={
                    "attachments": [],
                    "planning_mode": None,
                    "text": "restart me",
                    "workspace_id": "ws_restart",
                    **({"agent_draft_target": {"kind": "agent_draft", "id": "sad_restart", "version": 3}} if with_draft_target else {}),
                },
                presentation_locale="zh-CN",
                message_catalog_version=1,
            )
        )
        assert admitted.disposition is AdmissionDisposition.ADMITTED
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                """
                SELECT state,claim_epoch,execution_started_at
                FROM turn_generations WHERE turn_id='t_restart'
                """
            ).fetchone() == ("opened", 0, None)

    second = create_app()
    async with second.router.lifespan_context(second):
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                """
                SELECT state,execution_started_at,terminal_receipt_id
                FROM turn_generations WHERE turn_id='t_restart'
                """
            ).fetchone()[0:2] == ("closed", None)
            assert connection.execute(
                """
                SELECT status,reason_code
                FROM turn_terminal_receipts
                WHERE turn_id='t_restart' AND generation=1
                """
            ).fetchone() == (
                "aborted",
                "terminal_projection_not_committed",
            )
            intent = json.loads(connection.execute("SELECT execution_intent_json FROM turn_generations WHERE turn_id='t_restart'").fetchone()[0])
            assert intent.get("agent_draft_target") == ({"kind": "agent_draft", "id": "sad_restart", "version": 3} if with_draft_target else None)
        assert await second.state.terminal_protocol_store.count_recovery_inventory() == 0
        assert second.state.terminal_runtime_registry._workers == {}


@pytest.mark.integration
async def test_second_gateway_stays_unready_until_writer_lease_is_released(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    # ``bowhead migrate --target core`` always resolves the canonical
    # ``bowhead.db`` filename within the configured database directory.
    db_path = tmp_path / TARGET_FILES["core"]
    assert upgrade(f"sqlite+aiosqlite:///{db_path}") == tuple(TARGET_FILES), "迁移未跑到 head"
    await _deploy_startup_cutover(db_path)
    monkeypatch.setenv(
        "BOWHEAD_DB_URL",
        f"sqlite+aiosqlite:///{db_path}",
    )
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "subprocess")
    monkeypatch.setenv("BOWHEAD_ASSEMBLY_ROUTER", "mock")
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "rollout")
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1_PCT", "100")

    from bowhead_gateway.main import create_app

    first = create_app()
    second = create_app()
    first_context = first.router.lifespan_context(first)
    second_context = second.router.lifespan_context(second)
    first_open = False
    second_open = False
    try:
        await first_context.__aenter__()
        first_open = True
        assert first.state.terminal_writer_service.fresh_admission_ready is True
        await second_context.__aenter__()
        second_open = True
        assert second.state.terminal_writer_service.fresh_admission_ready is False

        transport = httpx2.ASGITransport(app=second)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://gateway",
        ) as client:
            blocked = await client.get("/readyz")
        assert blocked.status_code == 503
        assert blocked.json()["checks"]["terminal_writer"] == "unavailable"

        await first_context.__aexit__(None, None, None)
        first_open = False
        for _ in range(70):
            if second.state.terminal_writer_service.fresh_admission_ready:
                break
            await asyncio.sleep(0.1)
        assert second.state.terminal_writer_service.fresh_admission_ready is True

        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://gateway",
        ) as client:
            ready = await client.get("/readyz")
        assert ready.status_code == 200, ready.text
        assert ready.json()["checks"]["terminal_writer"] == "ready"
    finally:
        if second_open:
            await second_context.__aexit__(None, None, None)
        if first_open:
            await first_context.__aexit__(None, None, None)
