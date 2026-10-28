from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.terminal_protocol import TerminalScope
from bowhead_core.terminal_protocol_store import (
    DecisionAuthorizationDeniedError,
)

from bowhead_gateway import terminal_decision_authorization
from bowhead_gateway import terminal_pending_execution as pending_execution


class _UserStore:
    def __init__(self, *, member_role: str | None = "member") -> None:
        self.member_role = member_role

    async def get_user(self, user_id: str):
        return SimpleNamespace(
            id=user_id,
            status="active",
            email="owner@example.com",
            email_verified=True,
        )

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        assert user_id == "u_1"
        assert team_id == "team_1"
        return "member"

    async def get_workspace_team_id(self, workspace_id: str) -> str:
        assert workspace_id == "ws_1"
        return "team_1"

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        assert workspace_id == "ws_1"
        assert user_id == "u_1"
        return self.member_role

    async def get_workspace(self, workspace_id: str) -> dict[str, str]:
        assert workspace_id == "ws_1"
        return {"id": workspace_id, "status": "active", "team_id": "team_1"}


class _MemberResolver:
    def __init__(self, member_uid: str) -> None:
        self.member_uid = member_uid
        self.calls: list[tuple[str, str, object]] = []

    async def resolve(self, email: str, workspace_id: str, *, datarc):
        self.calls.append((email, workspace_id, datarc))
        return self.member_uid


class _Factory:
    async def prepare_web_auth_fence(self, **kwargs) -> str:
        assert kwargs == {
            "team_id": "team_1",
            "session_id": "s_1",
            "user_id": "u_1",
        }
        return "auth_current"


def _template(*, member_uid: str = "member_1") -> dict[str, object]:
    return {
        "turn_id": "t_child",
        "request_id": "req_child",
        "user_text": "",
        "execution_intent": {
            "active_datarc_project": "project_1",
            "attachments": ["artifact_1"],
            "member_uid": member_uid,
            "planning_mode": "planning",
            "presentation_locale": "en-US",
            "source_authz_snapshot_digest": "auth_parent",
            "source_steer_ids": ["steer_1"],
            "source_steer_request_digests": ["steer_digest_1"],
            "user_id": "u_1",
            "workspace_id": "ws_1",
        },
    }


def _scope() -> TerminalScope:
    return TerminalScope(
        team_id="team_1",
        session_id="s_1",
        turn_id="t_parent",
        generation=1,
    )


def _app(*, member_uid: str = "member_1"):
    return SimpleNamespace(
        state=SimpleNamespace(
            user_store=_UserStore(),
            access_control_store=None,
            member_resolver=_MemberResolver(member_uid),
            terminal_runtime_factory=_Factory(),
        )
    )


@pytest.fixture
def followup_dependencies(monkeypatch):
    authorizations: list[dict[str, str]] = []

    async def authorize_pending_source(_self, **kwargs) -> None:
        authorizations.append(kwargs)

    monkeypatch.setattr(
        terminal_decision_authorization.TerminalDecisionAuthorizer,
        "authorize_pending_source",
        authorize_pending_source,
    )
    datarc_client = object()

    async def resolve_datarc(_app, **kwargs):
        assert kwargs == {
            "team_id": "team_1",
            "workspace_id": "ws_1",
            "user_id": "u_1",
            "session_id": "s_1",
            "pinned_project": "project_1",
        }
        return SimpleNamespace(
            datarc_client=datarc_client,
            datarc_project_for_calls="project_1",
            datarc_unavailable=False,
            vector_client=object(),
            active_datarc_project="project_1",
            active_datarc_workspace_name="Project One",
            accessible_datarc_workspaces=("project_1",),
            datarc_absence_reason="",
        )

    from bowhead_gateway import datarc_context

    monkeypatch.setattr(
        datarc_context,
        "resolve_datarc_turn_context",
        resolve_datarc,
    )
    team_router = object()
    team_kb = object()

    async def resolve_team_runtime(_state, team_id: str):
        assert team_id == "team_1"
        return team_router, team_kb

    monkeypatch.setattr(
        pending_execution,
        "resolve_terminal_team_runtime",
        resolve_team_runtime,
    )
    return authorizations, datarc_client, team_router, team_kb


@pytest.mark.asyncio
async def test_production_followup_resolver_rehydrates_current_context(
    followup_dependencies,
) -> None:
    authorizations, datarc_client, team_router, team_kb = followup_dependencies
    app = _app()

    request = await pending_execution.resolve_terminal_followup_request(
        app,
        _template(),
        _scope(),
    )

    assert authorizations == [
        {
            "team_id": "team_1",
            "session_id": "s_1",
            "workspace_id": "ws_1",
            "requester_ref": "u_1",
        }
    ]
    assert request.session_id == "s_1"
    assert request.turn_id == "t_child"
    assert request.resume_turn_id == "t_parent"
    assert request.text == ""
    assert request.attachments == ["artifact_1"]
    assert request.planning_mode == "planning"
    assert request.member_uid == "member_1"
    assert request.datarc_client is datarc_client
    assert request.team_router is team_router
    assert request.team_kb_retriever is team_kb
    assert request.terminal_auth_fence_vector_digest == "auth_current"
    assert request.language_context is not None
    assert request.language_context.ui_language == "en-US"
    assert app.state.member_resolver.calls == [("owner@example.com", "project_1", datarc_client)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("steer_text", "settings", "expected_output"),
    [
        # chat-pipeline.md: a queued steer resolves like a direct send of the same text.
        ("What was revenue by region last month?", {}, "en-US"),
        ("上个月各区域的营收是多少？", {}, "zh-CN"),
        # A saved concrete answer language still wins over the steer's language.
        ("What was revenue by region last month?", {"output_language": "fr-FR"}, "fr-FR"),
    ],
    ids=["english-steer", "chinese-steer", "saved-preference"],
)
async def test_queued_steer_turn_resolves_the_answer_language_like_a_direct_send(
    followup_dependencies,
    steer_text: str,
    settings: dict[str, str],
    expected_output: str,
) -> None:
    app = _app()

    class _UserSettings:
        async def list_for_user(self, user_id: str) -> dict[str, str]:
            assert user_id == "u_1"
            return settings

    app.state.user_settings_store = _UserSettings()
    template = _template()
    template["user_text"] = steer_text
    template["execution_intent"]["presentation_locale"] = "zh-CN"

    request = await pending_execution.resolve_terminal_followup_request(app, template, _scope())

    assert request.language_context.ui_language == "zh-CN"
    assert request.language_context.output_language == expected_output


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_has_language_snapshot", [True, False], ids=["i18n-on", "i18n-off"])
async def test_queued_steer_turn_keeps_the_parent_viewer_zone(followup_dependencies, parent_has_language_snapshot: bool) -> None:
    """A steer's follow-up Turn states the same "today" as the Turn it continues."""
    from bowhead_core.language_context import LanguageContext
    from bowhead_core.model_clock import request_time_zone_name
    from bowhead_core.terminal_runtime_factory import _add_followup_time_zone

    parent = SimpleNamespace(
        language_context=LanguageContext().with_time_zone("America/Los_Angeles") if parent_has_language_snapshot else None,
        viewer_time_zone="" if parent_has_language_snapshot else "America/Los_Angeles",
    )
    template = _template()
    _add_followup_time_zone(template["execution_intent"], parent)
    assert template["execution_intent"]["time_zone"] == "America/Los_Angeles"

    request = await pending_execution.resolve_terminal_followup_request(_app(), template, _scope())

    assert request_time_zone_name(request) == "America/Los_Angeles"
    assert request.language_context.time_zone == "America/Los_Angeles"  # persisted with the child's snapshots


@pytest.mark.asyncio
async def test_queued_steer_turn_without_a_parent_zone_uses_the_deploy_clock(followup_dependencies) -> None:
    from bowhead_core.model_clock import request_time_zone_name
    from bowhead_core.terminal_runtime_factory import _add_followup_time_zone

    template = _template()
    _add_followup_time_zone(template["execution_intent"], SimpleNamespace(language_context=None, viewer_time_zone=""))
    assert "time_zone" not in template["execution_intent"]

    request = await pending_execution.resolve_terminal_followup_request(_app(), template, _scope())

    assert request_time_zone_name(request) == ""


@pytest.mark.asyncio
async def test_production_followup_resolver_fails_closed_on_member_drift(
    followup_dependencies,
) -> None:
    app = _app(member_uid="member_changed")

    with pytest.raises(
        DecisionAuthorizationDeniedError,
        match="dependency_missing",
    ):
        await pending_execution.resolve_terminal_followup_request(
            app,
            _template(member_uid="member_1"),
            _scope(),
        )


@pytest.mark.asyncio
async def test_production_followup_resolver_rejects_tampered_steer_envelope(
    followup_dependencies,
) -> None:
    template = _template()
    intent = template["execution_intent"]
    assert isinstance(intent, dict)
    intent["source_steer_request_digests"] = []

    with pytest.raises(
        DecisionAuthorizationDeniedError,
        match="dependency_missing",
    ):
        await pending_execution.resolve_terminal_followup_request(
            _app(),
            template,
            _scope(),
        )


@pytest.mark.asyncio
async def test_production_followup_resolver_fails_closed_without_exact_team_runtime(
    monkeypatch,
) -> None:
    async def authorize_pending_source(_self, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        terminal_decision_authorization.TerminalDecisionAuthorizer,
        "authorize_pending_source",
        authorize_pending_source,
    )

    from bowhead_gateway import datarc_context

    async def resolve_datarc(_app, **_kwargs):
        return SimpleNamespace(
            datarc_client=None,
            datarc_project_for_calls="",
            datarc_unavailable=True,
            vector_client=None,
            active_datarc_project=None,
            active_datarc_workspace_name="",
            accessible_datarc_workspaces=(),
            datarc_absence_reason="team_connection_missing",
        )

    monkeypatch.setattr(
        datarc_context,
        "resolve_datarc_turn_context",
        resolve_datarc,
    )
    app = _app(member_uid="")
    template = _template(member_uid="")
    intent = template["execution_intent"]
    assert isinstance(intent, dict)
    intent["active_datarc_project"] = ""

    with pytest.raises(
        DecisionAuthorizationDeniedError,
        match="dependency_missing",
    ):
        await pending_execution.resolve_terminal_followup_request(
            app,
            template,
            _scope(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "expected_output"),
    [
        # chat-pipeline.md: an English question from a zh-UI unset user was admitted with
        # en-US answers; the resumed turn keeps that snapshot instead of the UI language.
        ({"ui_language": "zh-CN", "output_language": "en-US"}, "en-US"),
        # A snapshot that disagrees with the durable turn locale is not trusted.
        ({"ui_language": "fr-FR", "output_language": "en-US"}, "zh-CN"),
        # Older snapshots without a language key use the durable locale.
        (None, "zh-CN"),
        # An ordinary Lead checkpoint seals its own language: none when i18n
        # was off, never a locale fallback.
        (None, None),
    ],
    ids=["admitted-snapshot", "mismatched-snapshot", "no-snapshot", "ordinary-checkpoint"],
)
async def test_pending_clarification_recovery_matches_web_resume_capabilities(
    monkeypatch,
    snapshot: dict[str, str] | None,
    expected_output: str | None,
) -> None:
    switcher = object()
    captured: list[object] = []
    clarification = SimpleNamespace(
        request_id="clarify_1",
        turn_id="t_parent",
        workspace_id="ws_1",
        user_id="u_1",
        status="resolved",
        kind="tool_context_select",
        token="",
        chosen_value="__bowhead_clarification_skip__",
        chosen_values=["__bowhead_clarification_skip__"],
        candidates=[SimpleNamespace(label="销售域", desc="销售数据")],
        resume_context={
            "session_id": "s_1",
            "workspace_id": "ws_1",
            "team_id": "team_1",
            "user_id": "u_1",
            "user_text": "分析销售",
        }
        | ({"language_context": snapshot} if snapshot is not None else {}),
    )

    class Clarifications:
        async def get(self, request_id: str):
            assert request_id == "clarify_1"
            return clarification

    class Factory:
        async def open_decision_runtime(self, _prepared, request):
            captured.append(request)
            return object()

    class Protocol:
        async def read_persisted_turn_presentation_locale(self, **kwargs):
            assert kwargs == {"team_id": "team_1", "turn_id": "t_parent"}
            return "zh-CN"

    app = SimpleNamespace(
        state=SimpleNamespace(
            clarification_store=Clarifications(),
            orchestrator=object(),
            user_store=_UserStore(),
            access_control_store=None,
            entitlements=object(),
        )
    )
    recovery = pending_execution.TerminalPendingExecutionRecovery(
        app=app,
        factory=Factory(),
        protocol_store=Protocol(),
        approval_runner=object(),
        authorizer=object(),
    )
    prepared = SimpleNamespace(
        decision=SimpleNamespace(
            context=SimpleNamespace(
                scope=_scope(),
                source_id="clarify_1",
                principals=SimpleNamespace(requester_ref="u_1"),
            )
        ),
        preflight=SimpleNamespace(authz_snapshot_digest="auth_1"),
    )

    from bowhead_gateway.routes import chat

    from bowhead_gateway import datarc_context, turn_service

    async def resolve_datarc(*_args, **_kwargs):
        return SimpleNamespace(
            datarc_client=None,
            datarc_project_for_calls="",
            datarc_unavailable=False,
            vector_client=None,
            active_datarc_project=None,
            active_datarc_workspace_name="",
            accessible_datarc_workspaces=({"id": "sales", "name": "销售域"},),
            datarc_absence_reason="",
        )

    async def team_runtime(_state, _team_id):
        return object(), object()

    async def spawn(_orchestrator, request, **_kwargs):
        captured.append(request)
        return object()

    monkeypatch.setattr(
        datarc_context,
        "resolve_datarc_turn_context",
        resolve_datarc,
    )
    monkeypatch.setattr(
        pending_execution,
        "resolve_terminal_team_runtime",
        team_runtime,
    )
    monkeypatch.setattr(
        chat,
        "_make_datarc_context_switcher",
        lambda *_args, **_kwargs: switcher,
    )
    monkeypatch.setattr(turn_service, "spawn_turn", spawn)
    monkeypatch.setenv("FEATURE_CONFIG_COPILOT", "1")
    if expected_output is None:
        from dataclasses import replace

        build = turn_service.build_resume_turn_request
        checkpoint = SimpleNamespace(language_context=lambda: None)
        monkeypatch.setattr(
            turn_service,
            "build_resume_turn_request",
            lambda *args, **kwargs: replace(build(*args, **kwargs), ordinary_lead_checkpoint=checkpoint),
        )

    assert await recovery._resume_clarification(prepared) is True
    opened_request, request = captured
    assert opened_request.text == request.text
    assert request.terminal_runtime is not None
    assert "tool_switch_context" in request.text
    assert "销售域（销售数据）" in request.text
    assert "__bowhead_clarification_skip__" not in request.text
    assert request.datarc_context_switcher is switcher
    assert request.config_lite_supported is True
    if expected_output is None:
        assert request.language_context is None
        return
    assert request.language_context.ui_language == "zh-CN"
    assert request.language_context.output_language == expected_output


@pytest.mark.asyncio
async def test_pending_approval_recovery_uses_durable_runner(
    monkeypatch,
) -> None:
    approval = SimpleNamespace(
        id="approval_1",
        status="approved",
        workspace_id="ws_1",
    )

    class Approvals:
        async def get(self, approval_id: str):
            assert approval_id == "approval_1"
            return approval

    class Runner:
        def __init__(self) -> None:
            self.calls = []

        async def spawn(self, **kwargs):
            self.calls.append(kwargs)
            return object()

    runner = Runner()
    app = SimpleNamespace(state=SimpleNamespace(approval_store=Approvals()))
    recovery = pending_execution.TerminalPendingExecutionRecovery(
        app=app,
        factory=object(),
        protocol_store=object(),
        approval_runner=runner,
        authorizer=object(),
    )
    prepared = SimpleNamespace(
        decision=SimpleNamespace(
            context=SimpleNamespace(
                source_id="approval_1",
                scope=_scope(),
                principals=SimpleNamespace(effect_principal_ref="u_1"),
            )
        )
    )
    executor = object()

    async def build_executor(*_args, **_kwargs):
        return executor

    monkeypatch.setattr(
        pending_execution,
        "build_terminal_approval_executor",
        build_executor,
    )

    assert await recovery._execute_approval(prepared) is True
    assert runner.calls == [
        {
            "prepared": prepared,
            "approval": approval,
            "legacy_executor": executor,
        }
    ]
