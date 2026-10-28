"""Gateway 基础路由测试。"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import bowhead_gateway.terminal_protocol_rollout as terminal_rollout
import pytest
from bowhead_core.subagent_compatibility import (
    ApplicationCompatibilityDecision,
)
from bowhead_gateway.main import create_app
from bowhead_gateway.subagent_compatibility import (
    GatewayCompatibilityState,
)
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    class _ReadySchema:
        @staticmethod
        async def chat_submission_protocol_schema_ready() -> bool:
            return True

        @staticmethod
        async def chat_submission_fingerprint_keyring_aligned(**_kwargs) -> bool:
            return True

        async def admit_or_steer_v1(self, *_args, **_kwargs):
            raise AssertionError("readiness must not mutate Terminal state")

        async def read_execution_accepted_highwater(self, *_args, **_kwargs):
            raise AssertionError("readiness must not read a Turn")

        async def read_web_chat_action(self, *_args, **_kwargs):
            raise AssertionError("readiness must not read an action")

    class _ReadyFactory:
        async def admit_web_turn(self, *_args, **_kwargs):
            raise AssertionError("readiness must not admit a Turn")

        async def steer_web_turn(self, *_args, **_kwargs):
            raise AssertionError("readiness must not steer")

        async def wake_admitted_web_turn(self, *_args, **_kwargs):
            raise AssertionError("readiness must not wake a Turn")

    class _ReadyGenerationBuses:
        async def get_or_create(self, *_args, **_kwargs):
            raise AssertionError("readiness must not create a bus")

    class _ReadyRecovery:
        async def run_bounded(self, *_args, **_kwargs):
            raise AssertionError("readiness must not run recovery")

    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"h" * 32).decode().rstrip("="),
    )
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS", raising=False)
    monkeypatch.delenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL",
        raising=False,
    )
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "off")
    app = create_app()
    terminal_store = _ReadySchema()
    app.state.session_store = terminal_store
    app.state.team_model_boundary_ready = True
    app.state.terminal_protocol_store = terminal_store
    app.state.terminal_writer_service = SimpleNamespace(
        fresh_admission_ready=False,
        startup_recovery_ready=False,
    )
    app.state.terminal_runtime_factory = _ReadyFactory()
    app.state.terminal_generation_bus_registry = _ReadyGenerationBuses()
    app.state.terminal_recovery_processor = _ReadyRecovery()
    app.state.terminal_recovery_sweep = SimpleNamespace(errors=())
    app.state.agent_assignment_store = SimpleNamespace(marketplace_ready=_marketplace_ready)
    app.state.agent_marketplace_schema_ready = True
    return TestClient(app)


async def _marketplace_ready() -> bool:
    return True


@pytest.mark.unit
def test_health_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert isinstance(body["timestamp"], int)


@pytest.mark.unit
def test_readyz(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert "checks" in resp.json()
    assert resp.json()["checks"]["agent_marketplace"] == "ready"


@pytest.mark.unit
def test_readyz_requires_marketplace_schema_and_cutover(
    client: TestClient,
) -> None:
    class AssignmentStore:
        def __init__(self, cutover: bool) -> None:
            self.cutover = cutover

        async def marketplace_ready(self) -> bool:
            return self.cutover

    client.app.state.agent_assignment_store = AssignmentStore(False)
    client.app.state.agent_marketplace_schema_ready = True
    blocked = client.get("/readyz")
    assert blocked.status_code == 503
    assert blocked.json()["checks"]["agent_marketplace"] == "unavailable"

    client.app.state.agent_assignment_store = AssignmentStore(True)
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"]["agent_marketplace"] == "ready"

    client.app.state.agent_marketplace_schema_ready = False
    schema_blocked = client.get("/readyz")
    assert schema_blocked.status_code == 503
    assert schema_blocked.json()["checks"]["agent_marketplace"] == "unavailable"


@pytest.mark.unit
def test_readyz_requires_enabled_terminal_writer_substrate(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        terminal_rollout,
        "_FRESH_TERMINAL_V1_ADMISSION_RELEASED",
        True,
    )
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "rollout")
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1_PCT", "100")

    blocked = client.get("/readyz")
    assert blocked.status_code == 503
    blocked_checks = blocked.json()["checks"]
    assert blocked_checks["durable_chat_execution_config"] == "ready"
    assert blocked_checks["terminal_writer"] == "unavailable"
    assert blocked_checks["terminal_recovery"] == "unavailable"
    assert blocked_checks["terminal_server_bundle"] == "unavailable"

    client.app.state.terminal_writer_service = SimpleNamespace(
        fresh_admission_ready=True,
        startup_recovery_ready=True,
    )
    ready = client.get("/readyz")
    assert ready.status_code == 200
    checks = ready.json()["checks"]
    assert checks["durable_chat_execution_config"] == "ready"
    assert checks["terminal_writer"] == "ready"
    assert checks["terminal_recovery"] == "ready"


@pytest.mark.unit
def test_readyz_rejects_explicitly_enabled_writer_when_terminal_schema_is_absent(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        terminal_rollout,
        "_FRESH_TERMINAL_V1_ADMISSION_RELEASED",
        True,
    )
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "required")
    client.app.state.terminal_protocol_store = None

    response = client.get("/readyz")

    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["durable_chat_execution_config"] == "ready"
    assert checks["terminal_writer"] == "unavailable"
    assert checks["terminal_recovery"] == "unavailable"


@pytest.mark.unit
def test_readyz_keeps_kill_switch_available_and_rejects_invalid_config(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        terminal_rollout,
        "_FRESH_TERMINAL_V1_ADMISSION_RELEASED",
        True,
    )
    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "off")

    disabled = client.get("/readyz")
    assert disabled.status_code == 200
    assert disabled.json()["checks"]["durable_chat_execution_config"] == "ready"
    assert disabled.json()["checks"]["terminal_writer"] == "inactive"
    assert disabled.json()["checks"]["terminal_recovery"] == "ready"

    monkeypatch.setenv("FEATURE_DURABLE_CHAT_EXECUTION_V1", "enabled")
    invalid = client.get("/readyz")
    assert invalid.status_code == 503
    assert invalid.json()["checks"]["durable_chat_execution_config"] == "unavailable"
    assert invalid.json()["checks"]["terminal_writer"] == "unavailable"
    assert invalid.json()["checks"]["terminal_recovery"] == "unavailable"


@pytest.mark.unit
def test_readyz_ignores_stale_enabled_env_after_writer_retirement(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEATURE_TURN_TERMINAL_PROTOCOL_V1_WRITER", "1")

    response = client.get("/readyz")

    assert response.status_code == 200
    checks = response.json()["checks"]
    assert checks["durable_chat_execution_config"] == "ready"
    assert checks["terminal_writer"] == "inactive"
    assert checks["terminal_recovery"] == "ready"


@pytest.mark.unit
def test_readyz_requires_complete_terminal_server_bundle_when_writer_is_off(
    client: TestClient,
) -> None:
    client.app.state.terminal_runtime_factory = None

    response = client.get("/readyz")

    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["durable_chat_execution_config"] == "ready"
    assert checks["terminal_writer"] == "inactive"
    assert checks["terminal_server_bundle"] == "unavailable"
    assert checks["terminal_recovery"] == "unavailable"


@pytest.mark.unit
def test_health_and_readyz_surface_subagent_compatibility_without_instance_id(
    client: TestClient,
) -> None:
    decision = ApplicationCompatibilityDecision(
        schema_version=2,
        reader_version="0001",
        writer_version="0001",
        minimum_reader_version="0001",
        minimum_writer_version="0001",
        reader_compatible=True,
        writer_compatible=True,
        compatible=True,
        checked_at="2026-07-28T07:00:00Z",
    )

    class _Heartbeat:
        state = GatewayCompatibilityState(
            status="ready",
            decision=decision,
        )

    client.app.state.subagent_compatibility_heartbeat = _Heartbeat()
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"]["subagent_compatibility"] == "ready"

    _Heartbeat.state = GatewayCompatibilityState(
        status="heartbeat_failed",
        decision=decision,
    )
    not_ready = client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json()["checks"]["subagent_compatibility"] == "unavailable"


@pytest.mark.unit
def test_readyz_rejects_export_runner_without_live_processor(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装配出了 runner 却没有可用 processor = 这台声称能导出但导不出，必须 not_ready。

    没有 runner 的部署（不装 node/soffice/pdftoppm 的镜像）不在此列，见 `health.py` 的注释。
    """

    class _DeadRunner:
        @staticmethod
        def is_available_for(fmt: str, profile: str) -> bool:
            return False

    client.app.state.export_runner = _DeadRunner()

    resp = client.get("/readyz")

    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"
    assert resp.json()["checks"]["canvas_export:pdf/full_report"] == "unavailable"


@pytest.mark.unit
def test_readyz_accepts_enabled_export_with_live_processor(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AvailableRunner:
        @staticmethod
        def is_available_for(fmt: str, profile: str) -> bool:
            # Official image contract guarantees node / pdftoppm / soffice, so all four format/profile combinations should be available.
            return True

    client.app.state.export_runner = _AvailableRunner()

    resp = client.get("/readyz")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert resp.json()["checks"]["canvas_export:pdf/full_report"] == "ready"


@pytest.mark.unit
def test_readyz_ignores_fingerprint_key_when_submission_protocol_is_inactive(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", raising=False)

    response = client.get("/readyz")

    assert response.status_code == 200
    checks = response.json()["checks"]
    assert checks["composer_submission_key"] == "inactive"
    assert checks["composer_flag_matrix"] == "ready"


@pytest.mark.unit
def test_readyz_keeps_compatible_key_substrate_inactive_without_new_setting(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", raising=False)
    monkeypatch.setenv(
        "BOWHEAD_FERNET_KEY",
        base64.urlsafe_b64encode(b"f" * 32).decode(),
    )

    response = client.get("/readyz")

    assert response.status_code == 200
    checks = response.json()["checks"]
    assert checks["composer_submission_key"] == "inactive"
    assert checks["composer_submission_key_epochs"] == "inactive"


@pytest.mark.unit
def test_readyz_does_not_gate_chat_on_inactive_rotation_keyring(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    monkeypatch.delenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", raising=False)
    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS", f'["{previous}"]')
    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_ACCEPT_UNTIL",
        (datetime.now(tz=UTC) + timedelta(days=30)).isoformat(),
    )
    assert client.get("/readyz").status_code == 200

    current = base64.urlsafe_b64encode(b"c" * 32).decode().rstrip("=")
    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_KEY", current)
    monkeypatch.setenv("BOWHEAD_SUBMISSION_FINGERPRINT_PREVIOUS_KEYS", "not-json")
    assert client.get("/readyz").status_code == 200


@pytest.mark.unit
def test_readyz_does_not_require_inactive_composer_schema(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SchemaProbe:
        @staticmethod
        async def chat_submission_protocol_schema_ready() -> bool:
            return False

        @staticmethod
        async def chat_submission_fingerprint_keyring_aligned(**_kwargs) -> bool:
            return True

    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    client.app.state.session_store = _SchemaProbe()
    client.app.state.team_model_boundary_ready = True

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"]["composer_submission_schema"] == "inactive"


@pytest.mark.unit
def test_readyz_accepts_composer_a_key_matrix_and_schema(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SchemaProbe:
        @staticmethod
        async def chat_submission_protocol_schema_ready() -> bool:
            return True

        @staticmethod
        async def chat_submission_fingerprint_keyring_aligned(**_kwargs) -> bool:
            return True

    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    client.app.state.session_store = _SchemaProbe()
    client.app.state.team_model_boundary_ready = True

    response = client.get("/readyz")

    assert response.status_code == 200
    checks = response.json()["checks"]
    assert checks["composer_submission_key"] == "inactive"
    assert checks["composer_flag_matrix"] == "ready"
    assert checks["composer_submission_schema"] == "inactive"
    assert checks["composer_submission_key_epochs"] == "inactive"


@pytest.mark.unit
def test_readyz_does_not_gate_chat_on_inactive_keyring_epochs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SchemaProbe:
        @staticmethod
        async def chat_submission_protocol_schema_ready() -> bool:
            return True

        @staticmethod
        async def chat_submission_fingerprint_keyring_aligned(**_kwargs) -> bool:
            return False

    monkeypatch.setenv(
        "BOWHEAD_SUBMISSION_FINGERPRINT_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    client.app.state.session_store = _SchemaProbe()
    client.app.state.team_model_boundary_ready = True

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"]["composer_submission_key_epochs"] == "inactive"


@pytest.mark.unit
def test_metrics_disabled_by_default(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """#107: /metrics 默认 off，env 未开 → 404."""
    monkeypatch.delenv("BOWHEAD_METRICS_ENDPOINT_ENABLED", raising=False)
    resp = client.get("/metrics")
    assert resp.status_code == 404


@pytest.mark.unit
def test_metrics_enabled_via_env(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """#107: 打开 env 后返回 prometheus 文本."""
    from bowhead_core.metrics import get_registry

    monkeypatch.setenv("BOWHEAD_METRICS_ENDPOINT_ENABLED", "1")
    get_registry().reset()
    get_registry().counter("bowhead_gateway_probe", help="probe").inc(labels={"k": "v"})
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "bowhead_gateway_probe" in body
    assert "# TYPE bowhead_gateway_probe counter" in body
    get_registry().reset()


# ── /health wheelhouse availability ──────────────────────────────────────────


@pytest.mark.unit
def test_health_reports_wheelhouse_availability_without_fingerprint(
    client: TestClient,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_SANDBOX_WHEELHOUSE", str(tmp_path))

    runtime_info = client.get("/health").json()["runtime_info"]

    assert runtime_info["wheelhouse_present"] is True
    assert "wheelhouse_sha" not in runtime_info


# Full-stack coverage in test_chat_integration.py supersedes chat/send unit-level tests
# (chat.send depends on app.state.orchestrator and needs lifespan startup, so does not belong in health unit tests).
