from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.session_lifecycle import (
    SESSION_LIFECYCLE_ERROR_CODES,
    SessionLifecycleError,
)
from bowhead_gateway.session_lifecycle import (
    SESSION_LIFECYCLE_PUBLIC_CODES,
    project_session_lifecycle_error,
    revive_session_for_im,
)


@pytest.mark.parametrize(
    ("internal_code", "status_code", "public_code"),
    [
        ("session_active_turn", 409, "session_delete_active_turn"),
        ("session_lifecycle_conflict", 409, "session_transitioning"),
        ("session_lifecycle_commit_conflict", 409, "session_transitioning"),
        ("session_lifecycle_precondition_changed", 409, "session_transitioning"),
        ("session_visibility_epoch_changed", 409, "session_transitioning"),
        ("session_revive_blocked", 409, "terminal_dependency_recovering"),
        ("session_generation_not_fenced", 409, "terminal_dependency_recovering"),
        ("terminal_protocol_future_version", 409, "terminal_protocol_future_version"),
        ("terminal_protocol_malformed", 500, "terminal_protocol_malformed"),
        ("session_lifecycle_operation_missing", 500, "terminal_protocol_malformed"),
        ("session_visibility_epoch_missing", 500, "terminal_protocol_malformed"),
        ("session_owner_mismatch", 409, "terminal_protocol_admission_rejected"),
        ("session_not_found", 409, "terminal_protocol_admission_rejected"),
        ("linked_canvas_not_found", 409, "terminal_protocol_admission_rejected"),
        ("canvas_lifecycle_store_missing", 503, "terminal_protocol_writer_not_ready"),
        ("unknown_internal_value", 500, "terminal_protocol_malformed"),
    ],
)
def test_lifecycle_projection_is_finite(
    internal_code: str,
    status_code: int,
    public_code: str,
) -> None:
    projected = project_session_lifecycle_error(SessionLifecycleError(internal_code))

    assert projected.status_code == status_code
    assert projected.code == public_code
    assert projected.code in SESSION_LIFECYCLE_PUBLIC_CODES


def test_every_core_lifecycle_code_has_an_explicit_projection() -> None:
    projected = {code: project_session_lifecycle_error(SessionLifecycleError(code)) for code in SESSION_LIFECYCLE_ERROR_CODES}

    assert projected.keys() == SESSION_LIFECYCLE_ERROR_CODES
    assert all(item.code in SESSION_LIFECYCLE_PUBLIC_CODES for item in projected.values())


@pytest.mark.asyncio
async def test_im_lifecycle_error_returns_controlled_result_without_legacy_fallback() -> None:
    class Coordinator:
        async def revive(self, **_kwargs):
            raise SessionLifecycleError("terminal_protocol_future_version")

    class LegacyStore:
        calls = 0

        async def revive_session(self, *_args, **_kwargs):
            self.calls += 1
            return True

    legacy = LegacyStore()
    app = SimpleNamespace(state=SimpleNamespace(session_lifecycle_coordinator=Coordinator()))
    orchestrator = SimpleNamespace(session_store=legacy)

    result = await revive_session_for_im(
        app,
        orchestrator,
        session_id="s_1",
        user_id="u_1",
        request_id="teams-message:m_1",
    )

    assert not result.changed
    assert result.error_code == "terminal_protocol_future_version"
    assert legacy.calls == 0


@pytest.mark.asyncio
async def test_im_missing_coordinator_requires_explicit_legacy_mode() -> None:
    class LegacyStore:
        calls = 0

        async def revive_session(self, *_args, **_kwargs):
            self.calls += 1
            return True

    legacy = LegacyStore()
    app = SimpleNamespace(state=SimpleNamespace())
    orchestrator = SimpleNamespace(session_store=legacy)

    blocked = await revive_session_for_im(
        app,
        orchestrator,
        session_id="s_1",
        user_id="u_1",
        request_id="feishu-message:m_1",
    )
    app.state.legacy_session_lifecycle_allowed = True
    allowed = await revive_session_for_im(
        app,
        orchestrator,
        session_id="s_1",
        user_id="u_1",
        request_id="feishu-message:m_2",
    )

    assert blocked.error_code == "terminal_protocol_writer_not_ready"
    assert not blocked.changed
    assert allowed.error_code is None
    assert allowed.changed
    assert legacy.calls == 1


@pytest.mark.asyncio
async def test_im_explicit_legacy_mode_still_requires_session_store() -> None:
    app = SimpleNamespace(state=SimpleNamespace(legacy_session_lifecycle_allowed=True))
    orchestrator = SimpleNamespace(session_store=None)

    result = await revive_session_for_im(
        app,
        orchestrator,
        session_id="s_1",
        user_id="u_1",
        request_id="teams-message:m_1",
    )

    assert not result.changed
    assert result.error_code == "terminal_protocol_writer_not_ready"
