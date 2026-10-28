from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from bowhead_core.language_context import LanguageContext
from bowhead_core.terminal_protocol import TerminalScope
from bowhead_core.terminal_protocol_store import (
    WebChatActionAccessScope,
    WebChatActionFutureVersionError,
    WebChatActionLookup,
    WebChatActionMalformedError,
    canonical_admission_request_digest,
)
from bowhead_gateway.routes import auth as auth_routes
from bowhead_gateway.routes import chat as chat_routes
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

START_SUFFIX = "0123456789abcdef0123456789abcdef"
START_ACTION_ID = f"act_start_{START_SUFFIX}"
STEER_SUFFIX = "fedcba9876543210fedcba9876543210"
STEER_ACTION_ID = f"act_steer_{STEER_SUFFIX}"


class _ActionStore:
    def __init__(self, result: WebChatActionLookup | Exception | None) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def read_web_chat_action(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _UserStore:
    def __init__(
        self,
        *,
        workspace_role: str | None = "member",
        workspace_status: str = "active",
        fail: bool = False,
    ) -> None:
        self.workspace_role = workspace_role
        self.workspace_status = workspace_status
        self.fail = fail

    async def get_user(self, user_id: str):
        if self.fail:
            raise RuntimeError("auth database unavailable")
        return SimpleNamespace(id=user_id, status="active")

    async def get_team_role(self, user_id: str, team_id: str):
        return "member"

    async def get_workspace_team_id(self, workspace_id: str):
        return "team_1"

    async def get_member_role(self, workspace_id: str, user_id: str):
        return self.workspace_role

    async def get_workspace(self, workspace_id: str):
        return {"id": workspace_id, "status": self.workspace_status}

    async def get_team_llm_config_with_version(self, team_id: str):
        return {}, 0


def _lookup(
    *,
    kind: str,
    paradigm_v2: bool | None = False,
    planning_mode: str | None = None,
    language_context: dict[str, object] | None = None,
    resolution: dict[str, object] | None = None,
    with_resolution_snapshot: bool = True,
    agent_draft_target: dict[str, object] | None = None,
) -> WebChatActionLookup:
    request_digest = None
    admitted_resolution = {
        "active_datarc_project": None,
        "member_uid": "",
        "preferred_model": None,
        "preferred_provider": None,
        **(resolution or {}),
    }
    if kind == "start":
        action_id = START_ACTION_ID
        session_id = f"s_{START_SUFFIX}"
        turn_id = f"t_{START_SUFFIX}"
        generation = 1
    else:
        action_id = STEER_ACTION_ID
        session_id = "s_existing"
        turn_id = "t_running"
        generation = 7
    if kind == "start":
        execution_intent: dict[str, object] = {
            "attachment_refs": [],
            "attachments": [],
            "language_context": language_context,
            "parts": None,
            "planning_mode": planning_mode,
            "text": "hello",
            "workspace_id": "ws_1",
            **admitted_resolution,
        }
        if paradigm_v2 is not None:
            execution_intent["paradigm_v2"] = paradigm_v2
        if agent_draft_target is not None:
            execution_intent["agent_draft_target"] = agent_draft_target
        request_digest = canonical_admission_request_digest(
            execution_intent=execution_intent,
            request_id=action_id,
            session_id=session_id,
            team_id="team_1",
            turn_id=turn_id,
            user_text="hello",
        )
    return WebChatActionLookup(
        action_id=action_id,
        action_kind=kind,
        scope=TerminalScope(
            team_id="team_1",
            session_id=session_id,
            turn_id=turn_id,
            generation=generation,
        ),
        workspace_id="ws_1",
        request_digest=request_digest,
        paradigm_v2=(paradigm_v2 if kind == "start" else None),
        planning_mode=(planning_mode if kind == "start" else None),
        language_context=(language_context if kind == "start" else None),
        admitted_resolution=(admitted_resolution if kind == "start" and with_resolution_snapshot else None),
        agent_draft_target=agent_draft_target,
    )


def _access_scope(*, kind: str = "start") -> WebChatActionAccessScope:
    lookup = _lookup(kind=kind)
    return WebChatActionAccessScope(
        team_id=lookup.scope.team_id,
        session_id=lookup.scope.session_id,
        turn_id=lookup.scope.turn_id,
        generation=lookup.scope.generation,
        workspace_id=lookup.workspace_id,
    )


def _app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    action_result: WebChatActionLookup | Exception | None,
    user_store: object | None = None,
    auth_error: HTTPException | None = None,
) -> tuple[FastAPI, _ActionStore]:
    async def _current_user(_request):
        if auth_error is not None:
            raise auth_error
        return SimpleNamespace(id="user_1", team_id="team_1")

    monkeypatch.setattr(auth_routes, "_current_user", _current_user)

    async def _resolve_datarc_identity(*_args, **_kwargs):
        return SimpleNamespace(active_datarc_project=None), ""

    async def _resolve_language_inputs(*_args, **_kwargs):
        return None, {}, None, False

    async def _resolve_model_preference(
        _request,
        *,
        preferred_provider,
        preferred_model,
        **_kwargs,
    ):
        return preferred_provider, preferred_model

    monkeypatch.setattr(
        chat_routes,
        "_resolve_web_datarc_identity",
        _resolve_datarc_identity,
    )
    monkeypatch.setattr(
        chat_routes,
        "_resolve_web_language_inputs",
        _resolve_language_inputs,
    )
    monkeypatch.setattr(
        chat_routes,
        "_resolve_effective_model_preference",
        _resolve_model_preference,
    )
    action_store = _ActionStore(action_result)
    app = FastAPI()
    app.state.terminal_protocol_store = action_store
    app.state.user_store = user_store if user_store is not None else _UserStore()
    app.state.access_control_store = None
    app.include_router(chat_routes.router, prefix="/api/v1/chat")
    return app, action_store


def test_start_action_lookup_returns_canonical_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, store = _app(monkeypatch, action_result=_lookup(kind="start"))

    response = TestClient(app).get(f"/api/v1/chat/actions/{START_ACTION_ID}")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "action_id": START_ACTION_ID,
        "action_kind": "start",
        "outcome": "admitted",
        "session_id": f"s_{START_SUFFIX}",
        "turn_id": f"t_{START_SUFFIX}",
        "execution_generation": 1,
        "terminal_protocol_version": 1,
        "stream_url": f"/api/v1/chat/stream/t_{START_SUFFIX}",
    }
    assert store.calls == [
        {
            "action_id": START_ACTION_ID,
            "action_kind": "start",
            "actor": "user_1",
            "team_id": "team_1",
            "turn_id": f"t_{START_SUFFIX}",
            "user_message_id": None,
            "session_id": None,
            "execution_generation": None,
        }
    ]


def test_steer_action_lookup_requires_and_forwards_exact_marker_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, store = _app(monkeypatch, action_result=_lookup(kind="steer"))

    response = TestClient(app).get(
        f"/api/v1/chat/actions/{STEER_ACTION_ID}",
        params={
            "session_id": "s_existing",
            "expected_turn_id": "t_running",
            "expected_execution_generation": "7",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["outcome"] == "accepted"
    assert store.calls[0] == {
        "action_id": STEER_ACTION_ID,
        "action_kind": "steer",
        "actor": "user_1",
        "team_id": "team_1",
        "turn_id": "t_running",
        "user_message_id": f"u_r_{STEER_SUFFIX}",
        "session_id": "s_existing",
        "execution_generation": 7,
    }


@pytest.mark.parametrize(
    ("path", "params"),
    (
        ("/api/v1/chat/actions/not-an-action", {}),
        (
            f"/api/v1/chat/actions/{START_ACTION_ID}",
            {"session_id": "s_wrong"},
        ),
        (
            f"/api/v1/chat/actions/{STEER_ACTION_ID}",
            {
                "session_id": "s_existing",
                "expected_turn_id": "t_running",
            },
        ),
        (
            f"/api/v1/chat/actions/{STEER_ACTION_ID}",
            {
                "session_id": "s_existing",
                "expected_turn_id": "t_running",
                "expected_execution_generation": "01",
            },
        ),
        (f"/api/v1/chat/actions/{START_ACTION_ID}", {"unknown": "value"}),
        (
            f"/api/v1/chat/actions/{START_ACTION_ID}",
            [("session_id", "one"), ("session_id", "two")],
        ),
    ),
)
def test_action_lookup_rejects_noncanonical_query_without_reading(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    params: dict[str, str] | list[tuple[str, str]],
) -> None:
    app, store = _app(monkeypatch, action_result=None)

    response = TestClient(app).get(path, params=params)

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"]["error"]["code"] == "request_validation_failed"
    assert store.calls == []


def test_action_miss_and_revoked_membership_share_opaque_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    miss_app, _ = _app(monkeypatch, action_result=None)
    miss = TestClient(miss_app).get(f"/api/v1/chat/actions/{START_ACTION_ID}")

    revoked_app, _ = _app(
        monkeypatch,
        action_result=_lookup(kind="start"),
        user_store=_UserStore(workspace_role=None),
    )
    revoked = TestClient(revoked_app).get(f"/api/v1/chat/actions/{START_ACTION_ID}")

    assert miss.status_code == revoked.status_code == 404
    assert miss.json() == revoked.json() == {"detail": {"error": {"code": "chat_action_not_found"}}}
    assert miss.headers["cache-control"] == revoked.headers["cache-control"] == "no-store"


def test_archived_workspace_remains_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _app(
        monkeypatch,
        action_result=_lookup(kind="start"),
        user_store=_UserStore(workspace_status="archived"),
    )

    response = TestClient(app).get(f"/api/v1/chat/actions/{START_ACTION_ID}")

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("action_result", "user_store", "expected_status", "expected_code"),
    (
        (
            WebChatActionFutureVersionError(
                "future",
                access_scope=_access_scope(),
            ),
            _UserStore(),
            409,
            "terminal_protocol_future_version",
        ),
        (
            WebChatActionMalformedError(
                "malformed",
                access_scope=_access_scope(),
            ),
            _UserStore(),
            503,
            "terminal_dependency_recovering",
        ),
        (RuntimeError("store down"), _UserStore(), 503, "terminal_dependency_recovering"),
        (_lookup(kind="start"), _UserStore(fail=True), 503, "authz_lookup_failed"),
    ),
)
def test_action_lookup_projects_dependency_and_protocol_errors(
    monkeypatch: pytest.MonkeyPatch,
    action_result: WebChatActionLookup | Exception,
    user_store: object,
    expected_status: int,
    expected_code: str,
) -> None:
    app, _ = _app(
        monkeypatch,
        action_result=action_result,
        user_store=user_store,
    )

    response = TestClient(app).get(f"/api/v1/chat/actions/{START_ACTION_ID}")

    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"]["error"]["code"] == expected_code


def test_authentication_failure_is_also_non_cacheable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, store = _app(
        monkeypatch,
        action_result=_lookup(kind="start"),
        auth_error=HTTPException(401, detail={"error": {"code": "auth_required"}}),
    )

    response = TestClient(app).get(f"/api/v1/chat/actions/{START_ACTION_ID}")

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert store.calls == []


def test_missing_terminal_action_store_is_non_cacheable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _app(monkeypatch, action_result=None)
    del app.state.terminal_protocol_store

    response = TestClient(app).get(f"/api/v1/chat/actions/{START_ACTION_ID}")

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"]["error"]["code"] == "terminal_dependency_recovering"


def test_future_protocol_is_hidden_after_membership_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _app(
        monkeypatch,
        action_result=WebChatActionFutureVersionError(
            "future",
            access_scope=_access_scope(),
        ),
        user_store=_UserStore(workspace_role=None),
    )

    response = TestClient(app).get(f"/api/v1/chat/actions/{START_ACTION_ID}")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": {"error": {"code": "chat_action_not_found"}}}


@pytest.mark.parametrize(
    "error",
    (
        WebChatActionMalformedError(
            "malformed",
            access_scope=_access_scope(),
        ),
        WebChatActionMalformedError(
            "malformed scope",
            access_scope=None,
        ),
    ),
)
def test_malformed_action_is_hidden_after_membership_revocation_or_invalid_scope(
    monkeypatch: pytest.MonkeyPatch,
    error: WebChatActionMalformedError,
) -> None:
    app, _ = _app(
        monkeypatch,
        action_result=error,
        user_store=_UserStore(workspace_role=None),
    )

    response = TestClient(app).get(f"/api/v1/chat/actions/{START_ACTION_ID}")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": {"error": {"code": "chat_action_not_found"}}}


def test_send_action_replay_precedes_orchestrator_readiness_and_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WakeFactory:
        def __init__(self) -> None:
            self.scopes: list[TerminalScope] = []

        async def wake_admitted_web_turn(self, scope: TerminalScope) -> bool:
            self.scopes.append(scope)
            return True

    observed_metrics: list[str] = []
    datarc_reads: list[bool] = []
    from bowhead_core import metrics

    monkeypatch.setattr(
        metrics,
        "inc",
        lambda name, **_kwargs: observed_metrics.append(name),
    )

    async def _read_only_datarc(*_args, read_only=False, **_kwargs):
        datarc_reads.append(read_only)
        return SimpleNamespace(active_datarc_project=None), ""

    body: dict[str, Any] = {
        "workspace_id": "ws_1",
        "message": "hello",
        "client_info": {
            "action_kind": "start",
            "request_id": START_ACTION_ID,
            "user_msg_id": f"u_s_{START_SUFFIX}",
        },
    }
    hit_app, _ = _app(monkeypatch, action_result=_lookup(kind="start"))
    monkeypatch.setattr(
        chat_routes,
        "_resolve_web_datarc_identity",
        _read_only_datarc,
    )
    wake_factory = WakeFactory()
    hit_app.state.terminal_runtime_factory = wake_factory
    hit = TestClient(hit_app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=body,
    )
    assert hit.status_code == 200, hit.text
    assert hit.json()["turn_id"] == f"t_{START_SUFFIX}"
    assert observed_metrics == []
    # Replay compares against the admitted Datarc snapshot; it makes no Datarc read.
    assert datarc_reads == []

    conflict = TestClient(hit_app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={**body, "message": "changed"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")
    assert observed_metrics == []
    assert wake_factory.scopes == [
        _lookup(kind="start").scope,
        _lookup(kind="start").scope,
    ]


def test_send_legacy_shape_start_replay_wakes_then_requires_get_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class WakeFactory:
        def __init__(self) -> None:
            self.scopes: list[TerminalScope] = []

        async def wake_admitted_web_turn(self, scope: TerminalScope) -> bool:
            self.scopes.append(scope)
            return True

    lookup = _lookup(kind="start", paradigm_v2=None)
    app, _ = _app(monkeypatch, action_result=lookup)
    wake_factory = WakeFactory()
    app.state.terminal_runtime_factory = wake_factory

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "workspace_id": "ws_1",
            "message": "hello",
            "client_info": {
                "action_kind": "start",
                "request_id": START_ACTION_ID,
                "user_msg_id": f"u_s_{START_SUFFIX}",
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")
    assert wake_factory.scopes == [lookup.scope]


def test_send_start_replay_never_treats_missing_paradigm_evidence_as_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even if the replayed envelope would reproduce the stored digest, an action
    # without explicit paradigm_v2 evidence must not be confirmed by POST.
    legacy = _lookup(kind="start", paradigm_v2=None)
    replayable_digest = canonical_admission_request_digest(
        execution_intent={
            "active_datarc_project": None,
            "attachment_refs": [],
            "attachments": [],
            "language_context": None,
            "member_uid": "",
            "paradigm_v2": None,
            "parts": None,
            "planning_mode": None,
            "preferred_model": None,
            "preferred_provider": None,
            "text": "hello",
            "workspace_id": "ws_1",
        },
        request_id=START_ACTION_ID,
        session_id=legacy.scope.session_id,
        team_id="team_1",
        turn_id=legacy.scope.turn_id,
        user_text="hello",
    )
    app, _ = _app(monkeypatch, action_result=replace(legacy, request_digest=replayable_digest))

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=_start_retry_body(),
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")


def test_send_explicit_true_paradigm_start_replay_matches_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _lookup(kind="start", paradigm_v2=True)
    app, _ = _app(monkeypatch, action_result=lookup)

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "workspace_id": "ws_1",
            "message": "hello",
            "client_info": {
                "action_kind": "start",
                "request_id": START_ACTION_ID,
                "user_msg_id": f"u_s_{START_SUFFIX}",
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["turn_id"] == lookup.scope.turn_id


def test_send_start_action_replay_projects_oversized_drift_as_non_definitive_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _app(monkeypatch, action_result=_lookup(kind="start"))
    wake_factory = SimpleNamespace(scopes=[])

    async def wake(scope: TerminalScope) -> bool:
        wake_factory.scopes.append(scope)
        return True

    wake_factory.wake_admitted_web_turn = wake
    app.state.terminal_runtime_factory = wake_factory
    observed_metrics: list[str] = []
    from bowhead_core import metrics

    monkeypatch.setattr(
        metrics,
        "inc",
        lambda name, **_kwargs: observed_metrics.append(name),
    )

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "workspace_id": "ws_1",
            "message": "x" * (chat_routes._MAX_CHAT_MESSAGE_LENGTH + 1),
            "client_info": {
                "action_kind": "start",
                "request_id": START_ACTION_ID,
                "user_msg_id": f"u_s_{START_SUFFIX}",
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")
    assert wake_factory.scopes == [_lookup(kind="start").scope]
    assert observed_metrics == []


def test_fresh_action_preserves_orchestrator_readiness_before_length_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _app(monkeypatch, action_result=None)

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "session_id": "s_existing",
            "workspace_id": "ws_1",
            "message": "x" * (chat_routes._MAX_CHAT_MESSAGE_LENGTH + 1),
            "client_info": {
                "action_kind": "start",
                "request_id": START_ACTION_ID,
                "user_msg_id": f"u_s_{START_SUFFIX}",
            },
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "orchestrator_not_ready"


@pytest.mark.parametrize(
    "message",
    ("hello", "changed", "", "x" * (chat_routes._MAX_CHAT_MESSAGE_LENGTH + 1)),
)
def test_send_steer_action_replay_uses_canonical_lookup_instead_of_acknowledging_a_post(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    app, _ = _app(monkeypatch, action_result=_lookup(kind="steer"))

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "session_id": "s_existing",
            "workspace_id": "ws_1",
            "message": message,
            "client_info": {
                "action_kind": "steer",
                "request_id": STEER_ACTION_ID,
                "user_msg_id": f"u_r_{STEER_SUFFIX}",
                "expected_turn_id": "t_running",
                "expected_execution_generation": "7",
            },
        },
    )

    lookup = TestClient(app).get(
        f"/api/v1/chat/actions/{STEER_ACTION_ID}",
        params={
            "session_id": "s_existing",
            "expected_turn_id": "t_running",
            "expected_execution_generation": "7",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["outcome"] == "accepted"


def test_send_start_action_replay_rejects_newly_effective_auto_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Start action accepted with planning OFF stores planning_mode=off (no automatic plan).
    # Replaying the same request after re-enabling (client could not submit a mode while OFF, hence None)
    # might automatically create a plan, unlike the original turn; conflict and restore canonically via GET.
    app, _ = _app(monkeypatch, action_result=_lookup(kind="start", planning_mode="off"))
    monkeypatch.setattr(chat_routes, "_planning_enabled", lambda: True)
    body: dict[str, Any] = {
        "workspace_id": "ws_1",
        "message": "hello",
        "client_info": {
            "action_kind": "start",
            "request_id": START_ACTION_ID,
            "user_msg_id": f"u_s_{START_SUFFIX}",
        },
    }

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=body,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")


def test_send_start_action_replay_accepts_flag_off_admission_while_flag_stays_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Replay an action accepted while OFF with the flag still OFF: same no-plan intent/digest,
    # return the original turn instead of a false conflict.
    app, _ = _app(monkeypatch, action_result=_lookup(kind="start", planning_mode="off"))
    monkeypatch.setattr(chat_routes, "_planning_enabled", lambda: False)

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "workspace_id": "ws_1",
            "message": "hello",
            "client_info": {
                "action_kind": "start",
                "request_id": START_ACTION_ID,
                "user_msg_id": f"u_s_{START_SUFFIX}",
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["turn_id"] == f"t_{START_SUFFIX}"
    assert response.json()["status"] == "started"


@pytest.mark.parametrize("planning_on", (True, False))
@pytest.mark.parametrize("action_kind", ("start", "steer"))
@pytest.mark.parametrize("existing", (False, True))
def test_terminal_action_never_executes_a_legacy_plan(
    monkeypatch: pytest.MonkeyPatch,
    action_kind: str,
    existing: bool,
    planning_on: bool,
) -> None:
    lookup = _lookup(kind=action_kind) if existing else None
    app, _ = _app(monkeypatch, action_result=lookup)
    monkeypatch.setattr(chat_routes, "_planning_enabled", lambda: planning_on)
    suffix = START_SUFFIX if action_kind == "start" else STEER_SUFFIX
    body: dict[str, Any] = {
        "workspace_id": "ws_1",
        "message": "execute",
        "plan_execution_id": "plan_1",
        "client_info": {
            "action_kind": action_kind,
            "request_id": f"act_{action_kind}_{suffix}",
            "user_msg_id": f"u_{'s' if action_kind == 'start' else 'r'}_{suffix}",
        },
    }
    if action_kind == "steer":
        body.update(
            {
                "session_id": "s_existing",
                "client_info": {
                    **body["client_info"],
                    "expected_turn_id": "t_running",
                    "expected_execution_generation": "7",
                },
            }
        )

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=body,
    )

    if existing:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == "terminal_protocol_admission_rejected"
    elif action_kind == "start":
        # Since 23fd993f1 (durable plan control), new start actions may carry plans; server rejects with planning disabled,
        # preventing stale clients creating actions whose plan fields are silently erased.
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == "client_refresh_required"
    else:
        # Steer never executes plans (approvals use start). New steer actions with plan_execution_id fail validation regardless
        # of planning flag, matching client_submission_id entry's steer validation;
        # previously OFF silently erased fields, while ON admitted them to plan execution.
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["error"]["code"] == "request_validation_failed"


@pytest.mark.parametrize(
    "drift",
    (
        "model",
        "planning",
        "member_override",
    ),
)
def test_send_start_action_replay_rejects_resolved_execution_context_drift(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    app, _ = _app(monkeypatch, action_result=_lookup(kind="start"))
    body = {
        "workspace_id": "ws_1",
        "message": "hello",
        "client_info": {
            "action_kind": "start",
            "request_id": START_ACTION_ID,
            "user_msg_id": f"u_s_{START_SUFFIX}",
        },
    }
    if drift == "model":
        body.update(
            {
                "preferred_provider": "provider_2",
                "preferred_model": "model_2",
            }
        )
    elif drift == "planning":
        monkeypatch.setattr(chat_routes, "_planning_enabled", lambda: True)
        body["planning_mode"] = "off"
    else:
        # A client member_uid override that admission did not record; with
        # workspace enforcement off the override is honored, so it differs.
        body["member_uid"] = "member_2"

        async def _honored_override(*_args, body_member_uid, **_kwargs):
            return SimpleNamespace(active_datarc_project=None), body_member_uid

        monkeypatch.setattr(
            chat_routes,
            "_resolve_web_datarc_identity",
            _honored_override,
        )

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=body,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")


def _start_retry_body(message: str = "hello") -> dict[str, object]:
    return {
        "workspace_id": "ws_1",
        "message": message,
        "client_info": {
            "action_kind": "start",
            "request_id": START_ACTION_ID,
            "user_msg_id": f"u_s_{START_SUFFIX}",
        },
    }


@pytest.mark.parametrize("message", ("hello", "hello again"))
def test_send_start_action_replay_keeps_language_snapshot_across_german_flag_flip(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    # A French-interface user chose German answers; admission ran while German
    # output was enabled, so the persisted snapshot carries de-DE output.
    admitted = LanguageContext(ui_language="fr-FR", output_language="de-DE")
    lookup = _lookup(kind="start", language_context=admitted.to_dict())
    real_resolver = chat_routes._resolve_web_language_inputs
    app, _ = _app(monkeypatch, action_result=lookup)

    class _UserSettings:
        async def list_for_user(self, _user_id: str):
            return {"ui_language": "fr-FR", "output_language": "de-DE"}

    class _WorkspaceSettings:
        async def list_for_workspace(self, _workspace_id: str):
            return {}

    app.state.user_settings_store = _UserSettings()
    app.state.workspace_settings_store = _WorkspaceSettings()
    monkeypatch.setattr(auth_routes, "resolve_user_id_sync", lambda _request: "user_1")
    monkeypatch.setattr(chat_routes, "_resolve_web_language_inputs", real_resolver)
    client = TestClient(app)

    # Sanity: with the flag still on, the live resolver reproduces the snapshot.
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    if message == "hello":
        same_flag = client.post(
            "/api/v1/chat/send",
            headers={"x-bowhead-terminal-protocol-capability": "1"},
            json=_start_retry_body(message),
        )
        assert same_flag.status_code == 200, same_flag.text

    # The retry lands after German output was switched off: re-resolution now
    # yields fr-FR output, but the request itself did not change.
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "0")
    response = client.post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=_start_retry_body(message),
    )

    if message == "hello":
        assert response.status_code == 200, response.text
        assert response.json()["turn_id"] == lookup.scope.turn_id
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")


@pytest.mark.parametrize("message", ("hello", "hello again"))
def test_send_start_action_replay_uses_the_admitted_answer_language_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    # chat-pipeline.md: the answer language is snapshotted at admission. This turn was
    # admitted with en-US answers (an earlier rule, or an English previous answer); the
    # retry re-resolves "hello" (no strict evidence, no previous answer: the Chinese
    # interface per language-policy.md), yet must compare against the snapshot.
    from bowhead_core.language_context import resolve_turn_language

    admitted = LanguageContext(ui_language="zh-CN", output_language="en-US")
    assert resolve_turn_language(accept_language="zh-CN", message_text="hello").output_language == "zh-CN"
    lookup = _lookup(kind="start", language_context=admitted.to_dict())
    real_resolver = chat_routes._resolve_web_language_inputs
    app, _ = _app(monkeypatch, action_result=lookup)

    class _UserSettings:
        async def list_for_user(self, _user_id: str):
            return {}

    class _WorkspaceSettings:
        async def list_for_workspace(self, _workspace_id: str):
            return {}

    app.state.user_settings_store = _UserSettings()
    app.state.workspace_settings_store = _WorkspaceSettings()
    monkeypatch.setattr(auth_routes, "resolve_user_id_sync", lambda _request: "user_1")
    monkeypatch.setattr(chat_routes, "_resolve_web_language_inputs", real_resolver)

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1", "accept-language": "zh-CN"},
        json=_start_retry_body(message),
    )

    if message == "hello":
        assert response.status_code == 200, response.text
        assert response.json()["turn_id"] == lookup.scope.turn_id
    else:
        # Control: a changed request is still a conflict, so the 200 above is not vacuous.
        assert response.status_code == 409, response.text


def test_send_start_action_replay_ignores_changed_language_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted = LanguageContext(ui_language="fr-FR", output_language="fr-FR")
    lookup = _lookup(kind="start", language_context=admitted.to_dict())
    app, _ = _app(monkeypatch, action_result=lookup)

    async def _new_resolution_rules(*_args, **_kwargs):
        # A deploy between admission and retry changed how the language resolves.
        return None, {}, LanguageContext(ui_language="fr-FR", output_language="en-US"), False

    monkeypatch.setattr(chat_routes, "_resolve_web_language_inputs", _new_resolution_rules)

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=_start_retry_body(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["turn_id"] == lookup.scope.turn_id


@pytest.mark.parametrize("message", ("hello", "hello again"))
def test_send_start_action_replay_keeps_paradigm_snapshot_across_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    # Admitted while the space had paradigm v2 enabled.
    lookup = _lookup(kind="start", paradigm_v2=True)
    real_resolver = chat_routes._resolve_web_language_inputs
    app, _ = _app(monkeypatch, action_result=lookup)

    class _UserSettings:
        async def list_for_user(self, _user_id: str):
            return {}

    class _WorkspaceSettings:
        async def list_for_workspace(self, _workspace_id: str):
            return {"paradigm_v2_enabled": "true"}

    app.state.user_settings_store = _UserSettings()
    app.state.workspace_settings_store = _WorkspaceSettings()
    monkeypatch.setattr(auth_routes, "resolve_user_id_sync", lambda _request: "user_1")
    monkeypatch.setattr(chat_routes, "_resolve_web_language_inputs", real_resolver)
    # The global kill switch flips on before the retry arrives.
    monkeypatch.setenv("BOWHEAD_PARADIGM_V2_KILL", "1")

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=_start_retry_body(message),
    )

    if message == "hello":
        assert response.status_code == 200, response.text
        assert response.json()["turn_id"] == lookup.scope.turn_id
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")


@pytest.mark.parametrize("message", ("hello", "hello again"))
def test_send_start_action_replay_reads_snapshot_from_the_real_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    initialize_migrated_core_database,
    message: str,
) -> None:
    # First admission: French interface with German answers, paradigm v2 on.
    admitted_language = LanguageContext(ui_language="fr-FR", output_language="de-DE").to_dict()
    store, turn_id = _admit_in_real_store(
        tmp_path,
        initialize_migrated_core_database,
        resolution={},
        language_context=admitted_language,
        paradigm_v2=True,
        presentation_locale="fr-FR",
    )

    app, _ = _app(monkeypatch, action_result=None)
    app.state.terminal_protocol_store = store

    async def _drifted_language_inputs(*_args, **_kwargs):
        # Whatever current settings would resolve to must not matter on replay.
        return None, {}, LanguageContext(ui_language="fr-FR", output_language="en-US"), False

    monkeypatch.setattr(chat_routes, "_resolve_web_language_inputs", _drifted_language_inputs)
    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=_start_retry_body(message),
    )

    if message == "hello":
        assert response.status_code == 200, response.text
        assert response.json()["turn_id"] == turn_id
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")


@pytest.mark.parametrize(
    ("message", "retry_zone", "expected_status"),
    (
        ("hello", None, 200),
        ("hello", "Asia/Tokyo", 200),
        ("hello", "America/Los_Angeles", 200),
        ("hello again", "America/Los_Angeles", 409),  # control: a changed request still conflicts
    ),
    ids=["retry-without-header", "retry-other-zone", "retry-same-zone", "changed-text"],
)
def test_send_start_replay_with_i18n_off_compares_the_admitted_viewer_zone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    initialize_migrated_core_database,
    message: str,
    retry_zone: str | None,
    expected_status: int,
) -> None:
    """i18n off ⇒ no language snapshot; the first admission's zone rides beside it and replay reuses it."""
    store, turn_id = _admit_in_real_store(
        tmp_path,
        initialize_migrated_core_database,
        resolution={"viewer_time_zone": "America/Los_Angeles"},
        language_context=None,
    )
    app, _ = _app(monkeypatch, action_result=None)  # the default language stub returns no snapshot (i18n off)
    app.state.terminal_protocol_store = store
    headers = {"x-bowhead-terminal-protocol-capability": "1"}
    if retry_zone is not None:
        headers["X-Bowhead-Time-Zone"] = retry_zone
    response = TestClient(app).post("/api/v1/chat/send", headers=headers, json=_start_retry_body(message))

    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        assert response.json()["turn_id"] == turn_id
    else:
        assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")


def test_send_start_action_replay_rejects_partial_datarc_identity_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bowhead_gateway.connector_context_service import ConnectorContextService
    from bowhead_gateway.datarc_context import _resolve_per_user_datarc_workspace

    class _FailingUserStore:
        async def get_user(self, _user_id: str):
            raise RuntimeError("user store unavailable")

    class _Memory:
        def __init__(self) -> None:
            self.set_calls: list[object] = []
            self.clear_calls: list[object] = []

        async def get_tool_context(
            self,
            _user_id: str,
            _tool: str,
            slot: str = "workspace",
            *,
            session_scoped: bool = False,
        ):
            if slot == "account":
                return {"id": "account_l3"}
            if session_scoped:
                return None
            return {"id": "project_1", "name": "旧偏好", "source": "explicit"}

        async def set_tool_context(self, *_args, **_kwargs):
            self.set_calls.append((_args, _kwargs))

        async def clear_tool_context(self, *_args, **_kwargs):
            self.clear_calls.append((_args, _kwargs))

    class _Datarc:
        async def list_account_projects(self, _account_uid: str, *, limit: int = 50):
            return [{"id": "project_1", "name": "当前名称"}]

    memory = _Memory()
    service = ConnectorContextService(
        user_store=_FailingUserStore(),
        user_memory_store=memory,
    )
    state = SimpleNamespace(
        tool_context_service=service,
        user_memory_store=memory,
    )
    read_only_seen: list[bool] = []
    provider_returns: list[tuple[str | None, str, tuple[dict, ...], str]] = []

    async def _partial_datarc_identity(*_args, read_only=False, **_kwargs):
        read_only_seen.append(read_only)
        resolved = await _resolve_per_user_datarc_workspace(
            state,
            _Datarc(),
            "user_1",
            session_id=f"s_{START_SUFFIX}",
            read_only=read_only,
        )
        provider_returns.append(resolved)
        return SimpleNamespace(active_datarc_project=resolved[0]), ""

    app, _ = _app(monkeypatch, action_result=_lookup(kind="start"))
    monkeypatch.setattr(
        chat_routes,
        "_resolve_web_datarc_identity",
        _partial_datarc_identity,
    )
    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "workspace_id": "ws_1",
            "message": "hello",
            # Only a differing client member_uid override reaches the read-only
            # Datarc resolution on replay; the snapshot path makes no call.
            "member_uid": "member_override",
            "client_info": {
                "action_kind": "start",
                "request_id": START_ACTION_ID,
                "user_msg_id": f"u_s_{START_SUFFIX}",
            },
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "terminal_protocol_admission_rejected"
    assert read_only_seen == [True]
    assert provider_returns == []
    assert "user_1" not in service._cache
    assert memory.set_calls == []
    assert memory.clear_calls == []


def test_send_start_action_replay_does_not_depend_on_account_settings_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_resolver = chat_routes._resolve_web_language_inputs
    lookup = _lookup(kind="start")
    app, _ = _app(monkeypatch, action_result=lookup)

    class _FailingUserSettings:
        async def list_for_user(self, _user_id: str):
            raise RuntimeError("settings unavailable")

    class _WorkspaceSettings:
        async def list_for_workspace(self, _workspace_id: str):
            return {}

    app.state.user_settings_store = _FailingUserSettings()
    app.state.workspace_settings_store = _WorkspaceSettings()
    monkeypatch.setattr(auth_routes, "resolve_user_id_sync", lambda _request: "user_1")
    monkeypatch.setattr(
        chat_routes,
        "_resolve_web_language_inputs",
        real_resolver,
    )

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "workspace_id": "ws_1",
            "message": "hello",
            "client_info": {
                "action_kind": "start",
                "request_id": START_ACTION_ID,
                "user_msg_id": f"u_s_{START_SUFFIX}",
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["turn_id"] == lookup.scope.turn_id


def test_send_start_action_replay_fails_closed_when_model_snapshot_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_resolver = chat_routes._resolve_effective_model_preference

    class _FailingModelStore(_UserStore):
        async def get_team_llm_config_with_version(self, team_id: str):
            raise RuntimeError("model settings unavailable")

    app, _ = _app(
        monkeypatch,
        action_result=_lookup(kind="start"),
        user_store=_FailingModelStore(),
    )
    monkeypatch.setattr(
        chat_routes,
        "_resolve_effective_model_preference",
        real_resolver,
    )

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "workspace_id": "ws_1",
            "message": "hello",
            "preferred_provider": "provider_1",
            "preferred_model": "model_1",
            "client_info": {
                "action_kind": "start",
                "request_id": START_ACTION_ID,
                "user_msg_id": f"u_s_{START_SUFFIX}",
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")


def test_send_start_action_replay_preserves_empty_model_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_resolver = chat_routes._resolve_effective_model_preference

    class _FailingModelStore(_UserStore):
        async def get_team_llm_config_with_version(self, team_id: str):
            raise RuntimeError("model settings unavailable")

    lookup = _lookup(kind="start")
    lookup = replace(
        lookup,
        request_digest=canonical_admission_request_digest(
            execution_intent={
                "active_datarc_project": None,
                "attachment_refs": [],
                "attachments": [],
                "language_context": None,
                "member_uid": "",
                "parts": None,
                "paradigm_v2": False,
                "planning_mode": None,
                "preferred_model": "",
                "preferred_provider": "",
                "text": "hello",
                "workspace_id": "ws_1",
            },
            request_id=lookup.action_id,
            session_id=lookup.scope.session_id,
            team_id=lookup.scope.team_id,
            turn_id=lookup.scope.turn_id,
            user_text="hello",
        ),
    )
    app, _ = _app(
        monkeypatch,
        action_result=lookup,
        user_store=_FailingModelStore(),
    )
    monkeypatch.setattr(
        chat_routes,
        "_resolve_effective_model_preference",
        real_resolver,
    )

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            "workspace_id": "ws_1",
            "message": "hello",
            "preferred_provider": "",
            "preferred_model": "",
            "client_info": {
                "action_kind": "start",
                "request_id": START_ACTION_ID,
                "user_msg_id": f"u_s_{START_SUFFIX}",
            },
        },
    )

    assert response.status_code == 200, response.text


def test_send_start_action_replay_compares_typed_refs_without_rerunning_attachment_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_ref = {
        "fileId": "artifact_1",
        "filename": "evidence.csv",
        "mime": "text/csv",
        "size": 42,
    }
    lookup = _lookup(kind="start")
    lookup = replace(
        lookup,
        request_digest=canonical_admission_request_digest(
            execution_intent={
                "active_datarc_project": None,
                "attachment_refs": [attachment_ref],
                "attachments": ["artifact_1"],
                "language_context": None,
                "member_uid": "",
                "parts": None,
                "paradigm_v2": False,
                "planning_mode": None,
                "preferred_model": None,
                "preferred_provider": None,
                "text": "hello",
                "workspace_id": "ws_1",
            },
            request_id=lookup.action_id,
            session_id=lookup.scope.session_id,
            team_id=lookup.scope.team_id,
            turn_id=lookup.scope.turn_id,
            user_text="hello",
        ),
    )
    app, _ = _app(monkeypatch, action_result=lookup)
    body = {
        "workspace_id": "ws_1",
        "message": "hello",
        "attachments": ["artifact_1"],
        "attachment_refs": [attachment_ref],
        "client_info": {
            "action_kind": "start",
            "request_id": START_ACTION_ID,
            "user_msg_id": f"u_s_{START_SUFFIX}",
        },
    }

    exact = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=body,
    )
    changed = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={
            **body,
            "attachment_refs": [
                {
                    **attachment_ref,
                    "filename": "changed.csv",
                }
            ],
        },
    )

    assert exact.status_code == 200, exact.text
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["error"]["code"] == ("terminal_protocol_admission_rejected")


def test_send_start_action_miss_never_reaches_orchestrator_or_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "workspace_id": "ws_1",
        "message": "hello",
        "client_info": {
            "action_kind": "start",
            "request_id": START_ACTION_ID,
            "user_msg_id": f"u_s_{START_SUFFIX}",
        },
    }

    def _unexpected_orchestrator(_request):
        raise AssertionError("action miss reached mutable orchestration")

    monkeypatch.setattr(chat_routes, "_get_orchestrator", _unexpected_orchestrator)
    app, store = _app(monkeypatch, action_result=None)

    response = TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json=body,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "client_refresh_required"
    assert len(store.calls) == 1
    assert store.calls[0]["action_kind"] == "start"


_real_model_resolver = chat_routes._resolve_effective_model_preference
_ADMITTED_MODEL = {"preferred_provider": "provider_1", "preferred_model": "model_1"}
_ADMITTED_DATARC = {"active_datarc_project": "project_1", "member_uid": "member_1"}


class _CountingModelStore(_UserStore):
    """Team model catalog after an admin removed the admitted model."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.catalog_reads = 0

    async def get_team_llm_config_with_version(self, team_id: str):
        self.catalog_reads += 1
        return {}, 0


def _post_start(app: FastAPI, **body_overrides: object):
    return TestClient(app).post(
        "/api/v1/chat/send",
        headers={"x-bowhead-terminal-protocol-capability": "1"},
        json={**_start_retry_body(), **body_overrides},
    )


def _counting_datarc_identity(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Datarc now re-resolves this user to another project and member uid."""

    calls: list[str | None] = []

    async def _re_resolved(*_args, body_member_uid=None, **_kwargs):
        calls.append(body_member_uid)
        return SimpleNamespace(active_datarc_project="project_2"), "member_2"

    monkeypatch.setattr(chat_routes, "_resolve_web_datarc_identity", _re_resolved)
    return calls


def test_send_start_action_replay_keeps_model_snapshot_after_team_catalog_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _lookup(kind="start", resolution=_ADMITTED_MODEL)
    user_store = _CountingModelStore()
    app, _ = _app(monkeypatch, action_result=lookup, user_store=user_store)
    monkeypatch.setattr(chat_routes, "_resolve_effective_model_preference", _real_model_resolver)

    # Sanity: re-resolving now drops the model, so the drift is real.
    async def _fresh_resolution():
        return await _real_model_resolver(
            SimpleNamespace(app=app),
            team_id="team_1",
            preferred_provider="provider_1",
            preferred_model="model_1",
            log=logging.getLogger("test"),
        )

    assert anyio.run(_fresh_resolution) == (None, None)
    user_store.catalog_reads = 0

    response = _post_start(app, **_ADMITTED_MODEL)

    assert response.status_code == 200, response.text
    assert response.json()["turn_id"] == lookup.scope.turn_id
    assert user_store.catalog_reads == 0


@pytest.mark.parametrize(
    "changed",
    (
        {"preferred_provider": "provider_1", "preferred_model": "model_2"},
        {"preferred_provider": None, "preferred_model": None},
        {"message": "hello again", **_ADMITTED_MODEL},
    ),
)
def test_send_start_action_replay_with_model_snapshot_rejects_changed_body(
    monkeypatch: pytest.MonkeyPatch,
    changed: dict[str, object],
) -> None:
    lookup = _lookup(kind="start", resolution=_ADMITTED_MODEL)
    app, _ = _app(monkeypatch, action_result=lookup, user_store=_CountingModelStore())
    monkeypatch.setattr(chat_routes, "_resolve_effective_model_preference", _real_model_resolver)

    response = _post_start(app, **changed)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "terminal_protocol_admission_rejected"


@pytest.mark.parametrize("body_member_uid", (None, "member_1"))
def test_send_start_action_replay_keeps_datarc_snapshot_after_membership_change(
    monkeypatch: pytest.MonkeyPatch,
    body_member_uid: str | None,
) -> None:
    lookup = _lookup(kind="start", resolution=_ADMITTED_DATARC)
    app, _ = _app(monkeypatch, action_result=lookup)
    calls = _counting_datarc_identity(monkeypatch)

    response = _post_start(app, member_uid=body_member_uid)

    assert response.status_code == 200, response.text
    assert response.json()["turn_id"] == lookup.scope.turn_id
    assert calls == []


def test_send_start_action_replay_rejects_changed_member_uid_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _lookup(kind="start", resolution=_ADMITTED_DATARC)
    app, _ = _app(monkeypatch, action_result=lookup)
    calls = _counting_datarc_identity(monkeypatch)

    response = _post_start(app, member_uid="member_override")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "terminal_protocol_admission_rejected"
    assert calls == ["member_override"]


def test_send_start_replay_without_resolution_snapshot_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stored digest would match this body; only the missing model/Datarc
    # snapshot evidence makes POST non-definitive.
    legacy = _lookup(kind="start", with_resolution_snapshot=False)
    app, _ = _app(monkeypatch, action_result=legacy)

    response = _post_start(app)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "terminal_protocol_admission_rejected"
    # Control: the same lookup with the snapshot replays.
    snapshot_app, _ = _app(monkeypatch, action_result=_lookup(kind="start"))
    assert _post_start(snapshot_app).status_code == 200


def _record_authz_then_replay(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from bowhead_gateway import access_context

    events: list[str] = []
    real_build = access_context.build_access_context
    real_replay = chat_routes._replay_existing_web_action

    async def _build(*args, **kwargs):
        events.append("authz")
        return await real_build(*args, **kwargs)

    async def _replay(*args, **kwargs):
        events.append("replay")
        return await real_replay(*args, **kwargs)

    monkeypatch.setattr(access_context, "build_access_context", _build)
    monkeypatch.setattr(chat_routes, "_replay_existing_web_action", _replay)
    return events


def test_send_start_replay_authorizes_before_comparing_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _lookup(kind="start", resolution={**_ADMITTED_MODEL, **_ADMITTED_DATARC})
    app, _ = _app(monkeypatch, action_result=lookup, user_store=_CountingModelStore())
    events = _record_authz_then_replay(monkeypatch)

    response = _post_start(app, **_ADMITTED_MODEL)

    assert response.status_code == 200, response.text
    assert events == ["authz", "replay"]


def test_send_start_replay_still_rejects_a_user_who_lost_space_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same body and a snapshot that would replay; the user was removed from the
    # Space after admission.
    lookup = _lookup(kind="start", resolution={**_ADMITTED_MODEL, **_ADMITTED_DATARC})
    revoked = _CountingModelStore(workspace_role=None)
    app, _ = _app(monkeypatch, action_result=lookup, user_store=revoked)
    events = _record_authz_then_replay(monkeypatch)
    datarc_calls = _counting_datarc_identity(monkeypatch)

    response = _post_start(app, **_ADMITTED_MODEL)

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error"]["code"] == "chat_action_not_found"
    assert events == ["authz"]
    assert revoked.catalog_reads == 0
    assert datarc_calls == []


def _admit_in_real_store(
    tmp_path,
    initialize_migrated_core_database,
    *,
    resolution: dict[str, object],
    language_context: dict[str, object] | None = None,
    paradigm_v2: bool = False,
    presentation_locale: str = "zh-CN",
):
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from bowhead_core.chat_turn_source import ChatTurnUserSource
    from bowhead_core.terminal_protocol import (
        AdmissionContext,
        AuthFenceEpoch,
        AuthFenceScope,
        AuthFenceScopeKind,
        AuthFenceVector,
        TerminalAdmissionRequest,
    )
    from bowhead_core.terminal_protocol_store import AdmissionDisposition, TerminalProtocolStore

    now = datetime(2026, 8, 3, 4, 5, 6, tzinfo=UTC)
    session_id = f"s_{START_SUFFIX}"
    turn_id = f"t_{START_SUFFIX}"
    db_path = tmp_path / "bowhead.db"
    initialize_migrated_core_database(db_path)
    vector = AuthFenceVector.build(
        (
            AuthFenceEpoch(AuthFenceScope(AuthFenceScopeKind.TEAM_MEMBERSHIP, "team_1", "team_1"), 1),
            AuthFenceEpoch(AuthFenceScope(AuthFenceScopeKind.RESOURCE_GRANT, "team_1", "team_1"), 1),
            AuthFenceEpoch(AuthFenceScope(AuthFenceScopeKind.SESSION_VISIBILITY, "team_1", session_id), 1),
            AuthFenceEpoch(AuthFenceScope(AuthFenceScopeKind.USER_ACCOUNT, "__global__", "user_1"), 1),
        )
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active','t')")
        connection.execute(
            """
            INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at,revision)
            VALUES ('ws_1','team_1','Space','active','t','t',1)
            """
        )
        connection.execute(
            """
            INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents)
            VALUES (?,'ws_1','user_1','Session','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z','active',0,0,'[]')
            """,
            (session_id,),
        )
        for entry in vector.entries:
            connection.execute(
                """
                INSERT INTO terminal_auth_scope_epochs(scope_kind,team_id,scope_ref,epoch,row_version,updated_at)
                VALUES (?,?,?,1,1,'t')
                """,
                (entry.scope.scope_kind.value, entry.scope.team_id, entry.scope.scope_ref),
            )
        connection.execute(
            """
            INSERT INTO terminal_protocol_writer_lease(
              lease_name,owner_instance_id,epoch,admission_state,admission_epoch,
              rollout_config_generation,lease_expires_at,heartbeat_at
            ) VALUES ('v1_writer','gateway_1',7,'open',3,'rollout_1',?,?)
            """,
            ((now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"), now.isoformat()),
        )
        connection.commit()

    store = TerminalProtocolStore(str(db_path), _clock=lambda: now)
    admitted = anyio.run(
        store.admit_or_steer_v1,
        TerminalAdmissionRequest(
            context=AdmissionContext(
                team_id="team_1",
                session_id=session_id,
                request_id=START_ACTION_ID,
                surface="web",
                cohort="bucket=1;config=rollout_1",
                actor="user_1",
                writer_instance_id="gateway_1",
                writer_lease_epoch=7,
                writer_admission_epoch=3,
                rollout_config_generation="rollout_1",
                auth_fence_vector_digest=vector.auth_fence_vector_digest,
            ),
            turn_id=turn_id,
            user_text="hello",
            execution_intent={
                "active_datarc_project": None,
                "attachment_refs": [],
                "attachments": [],
                "language_context": language_context,
                "member_uid": "",
                "paradigm_v2": paradigm_v2,
                "parts": None,
                "planning_mode": None,
                "preferred_model": None,
                "preferred_provider": None,
                "text": "hello",
                "workspace_id": "ws_1",
                **resolution,
            },
            presentation_locale=presentation_locale,
            message_catalog_version=1,
            allow_steer=False,
            user_source=ChatTurnUserSource(
                turn_id=turn_id,
                session_id=session_id,
                user_id="user_1",
                workspace_id="ws_1",
                user_text="hello",
                attachment_ids=(),
                attachment_refs=(),
                parts=None,
            ),
        ),
    )
    assert admitted.disposition is AdmissionDisposition.ADMITTED
    return store, turn_id


@pytest.mark.parametrize(
    ("case", "expected_status"),
    (
        ("same_body", 200),
        ("changed_text", 409),
        ("changed_model", 409),
        ("changed_member_override", 409),
        ("lost_space_access", 404),
        ("other_user", None),
    ),
)
def test_send_start_replay_against_real_store_after_model_and_membership_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    initialize_migrated_core_database,
    case: str,
    expected_status: int | None,
) -> None:
    store, turn_id = _admit_in_real_store(
        tmp_path,
        initialize_migrated_core_database,
        resolution={**_ADMITTED_MODEL, **_ADMITTED_DATARC},
    )
    user_store = _CountingModelStore(workspace_role=(None if case == "lost_space_access" else "member"))
    app, _ = _app(monkeypatch, action_result=None, user_store=user_store)
    app.state.terminal_protocol_store = store
    # Between admission and retry the team admin removed the admitted model and
    # Datarc now resolves this user to another project and member uid.
    monkeypatch.setattr(chat_routes, "_resolve_effective_model_preference", _real_model_resolver)
    _counting_datarc_identity(monkeypatch)
    events = _record_authz_then_replay(monkeypatch)
    if case == "other_user":

        async def _other_user(_request):
            return SimpleNamespace(id="user_2", team_id="team_1")

        monkeypatch.setattr(auth_routes, "_current_user", _other_user)

    body: dict[str, object] = dict(_ADMITTED_MODEL)
    if case == "changed_text":
        body["message"] = "hello again"
    elif case == "changed_model":
        body["preferred_model"] = "model_2"
    elif case == "changed_member_override":
        body["member_uid"] = "member_override"
    response = _post_start(app, **body)

    if case == "other_user":
        # The actor-bound store read finds no action for another user, so the
        # original turn is never replayed to them.
        assert response.status_code != 200, response.text
        assert turn_id not in response.text
        assert events == []
        return
    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        assert response.json()["turn_id"] == turn_id
        assert user_store.catalog_reads == 0
    if case == "lost_space_access":
        assert events == ["authz"]
    else:
        assert events == ["authz", "replay"]


def test_draft_action_recovery_keeps_original_target_without_reopening_editor(monkeypatch):
    target = {"kind": "agent_draft", "id": "sad_original", "version": 7}
    app, _ = _app(monkeypatch, action_result=_lookup(kind="start", agent_draft_target=target))
    from bowhead_gateway import agent_draft_send_target as target_admission

    async def reject_new_admission(*_args, **_kwargs):
        pytest.fail("Recovery must not reopen or authorize a draft editor")

    monkeypatch.setattr(target_admission, "admit_agent_draft_target", reject_new_admission)
    client = TestClient(app)
    found = client.get(f"/api/v1/chat/actions/{START_ACTION_ID}")
    assert found.status_code == 200
    assert found.json()["agent_draft_target"] == target
    body = {
        "workspace_id": "ws_1",
        "message": "hello",
        "client_info": {"action_kind": "start", "request_id": START_ACTION_ID, "agent_draft_id": "sad_original", "agent_draft_version": "7"},
    }
    headers = {"x-bowhead-terminal-protocol-capability": "1"}
    replay = client.post("/api/v1/chat/send", json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["agent_draft_target"] == target
    for changed in ({"agent_draft_id": "sad_other"}, {"agent_draft_version": "8"}, {"agent_draft_dirty": "1"}):
        rejected = client.post("/api/v1/chat/send", json={**body, "client_info": {**body["client_info"], **changed}}, headers=headers)
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["error"]["code"] == "terminal_protocol_admission_rejected"
    without_target = {"action_kind": "start", "request_id": START_ACTION_ID}
    rejected = client.post("/api/v1/chat/send", json={**body, "client_info": without_target}, headers=headers)
    assert rejected.status_code == 409


@pytest.mark.parametrize(
    "binding",
    [
        {"agent_draft_id": "sad_a"},
        {"agent_draft_version": "1"},
        {"agent_draft_dirty": "0"},
        {"agent_draft_id": "sad_a", "agent_draft_version": "0"},
        {"agent_draft_id": "sad_a", "agent_draft_version": "1", "agent_draft_dirty": "yes"},
    ],
)
def test_start_rejects_incomplete_or_malformed_draft_target_before_lookup(monkeypatch, binding):
    app, store = _app(monkeypatch, action_result=None)
    response = TestClient(app).post(
        "/api/v1/chat/send",
        json={
            "workspace_id": "ws_1",
            "message": "hello",
            "client_info": {"action_kind": "start", "request_id": START_ACTION_ID, **binding},
        },
    )
    assert response.status_code == 422
    assert store.calls == []


def test_steer_cannot_retarget_an_existing_turn_to_an_agent_draft(monkeypatch):
    app, store = _app(monkeypatch, action_result=_lookup(kind="steer"))
    response = TestClient(app).post(
        "/api/v1/chat/send",
        json={
            "workspace_id": "ws_1",
            "session_id": "s_existing",
            "message": "hello",
            "client_info": {
                "action_kind": "steer",
                "request_id": STEER_ACTION_ID,
                "expected_turn_id": "t_running",
                "expected_execution_generation": "7",
                "agent_draft_id": "sad_other",
                "agent_draft_version": "1",
            },
        },
    )
    assert response.status_code == 422
    assert store.calls == []
