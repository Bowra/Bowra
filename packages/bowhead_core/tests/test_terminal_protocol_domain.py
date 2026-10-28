from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bowhead_core.terminal_protocol import (
    ActorScope,
    AdmissionContext,
    AuthFenceResolveContext,
    AuthFenceScope,
    AuthFenceScopeKind,
    ConsumerProfile,
    ControlGenerationRequest,
    DecisionAction,
    DecisionContext,
    DecisionPrincipalSet,
    DecisionRecordRequest,
    DecisionSourceKind,
    DecisionState,
    EffectState,
    FinalAnswerMode,
    GenerationState,
    PhaseScope,
    RecordedDecision,
    RootDurability,
    TerminalAdmissionRequest,
    TerminalClassification,
    TerminalFollowupCapability,
    TerminalScope,
    TerminalStatus,
    canonical_decision_payload_digest_for,
    canonical_digest_v1,
    canonical_json_v1,
    classify_terminal,
    decision_identity_digest,
    legacy_status_for,
    legacy_terminal_events,
    load_terminal_protocol_enum_registry,
    load_terminal_protocol_reason_codes,
    recovery_effects_safe,
    stable_decision_action_id,
    stable_decision_id,
    validate_final_answer_mode,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "terminal_protocol_v1_golden.json"


@pytest.mark.unit
def test_decision_auth_context_allows_distinct_requester_and_effect_accounts() -> None:
    context = AuthFenceResolveContext(
        team_id="team_1",
        viewer="u_approver",
        resource_ref="s_1",
        session_id="s_1",
        required_scopes=(
            AuthFenceScope(AuthFenceScopeKind.TEAM_MEMBERSHIP, "team_1", "team_1"),
            AuthFenceScope(AuthFenceScopeKind.RESOURCE_GRANT, "team_1", "team_1"),
            AuthFenceScope(AuthFenceScopeKind.SESSION_VISIBILITY, "team_1", "s_1"),
            AuthFenceScope(AuthFenceScopeKind.USER_ACCOUNT, "__global__", "u_requester"),
            AuthFenceScope(AuthFenceScopeKind.USER_ACCOUNT, "__global__", "u_approver"),
        ),
    )

    assert len(context.required_scopes) == 5


@pytest.mark.unit
def test_recovery_effect_safety_distinguishes_pre_dispatch_from_unknown_outcome() -> None:
    assert recovery_effects_safe(())
    assert recovery_effects_safe((EffectState.INTENT, EffectState.SETTLED))
    assert not recovery_effects_safe((EffectState.DISPATCHED,))
    assert not recovery_effects_safe((EffectState.UNKNOWN,))


@pytest.mark.unit
def test_ac_b1b_005_legacy_root_error_reader_golden() -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    for case in fixture["cases"]:
        actual = classify_terminal(
            case["protocol_version"],
            ConsumerProfile(case["consumer_profile"]),
            case["event"],
        )
        assert actual == TerminalClassification(case["expected"]), case["id"]


@pytest.mark.unit
def test_ac_b1b_019_malformed_or_future_v1_fails_closed() -> None:
    malformed_cases = (
        {
            "event_type": "done",
            "event_id": "evt_1",
            "generation": 1,
            "actor_scope": "root",
            "phase_scope": "in_generation",
            "payload": {"status": "completed"},
        },
        {
            "event_type": "done",
            "event_id": "evt_2",
            "generation": 1,
            "actor_scope": "peer",
            "phase_scope": "in_generation",
            "terminal_receipt_id": "trc_1",
            "payload": {"status": "completed", "receipt_id": "trc_1"},
        },
    )
    for profile in ConsumerProfile:
        for event in malformed_cases:
            assert classify_terminal(1, profile, event) is TerminalClassification.MALFORMED
        assert classify_terminal(2, profile, {"event_type": "done"}) is TerminalClassification.MALFORMED


@pytest.mark.unit
def test_terminal_protocol_enum_registry_matches_python_contract() -> None:
    registry = load_terminal_protocol_enum_registry()
    assert set(registry["terminal_status"]) == set(TerminalStatus)
    assert set(registry["generation_state"]) == set(GenerationState)
    assert set(registry["actor_scope"]) == set(ActorScope)
    assert set(registry["phase_scope"]) == set(PhaseScope)
    assert set(registry["root_durability"]) == set(RootDurability)
    assert set(registry["final_answer_mode"]) == set(FinalAnswerMode)
    assert load_terminal_protocol_reason_codes() == (
        "worker_start_failed",
        "terminal_producer_timeout",
        "terminal_projection_missing",
        "terminal_projection_not_committed",
        "terminal_commit_outcome_unknown",
        "claim_owner_lost",
        "effect_outcome_unknown",
        "terminal_dependency_missing",
        "terminal_protocol_malformed",
        "terminal_protocol_future_version",
        "clarification_dismissed",
        "clarification_resolved",
        "approval_rejected",
        "user_cancelled",
        "decision_expired",
        "access_revoked",
    )


@pytest.mark.unit
def test_terminal_protocol_legacy_profiles_are_not_unified() -> None:
    assert "turn_cancelled" not in legacy_terminal_events(ConsumerProfile.WEB)
    assert "turn_cancelled" in legacy_terminal_events(ConsumerProfile.IM_RENDERER)
    assert "error" not in legacy_terminal_events(ConsumerProfile.TURN_HISTORY)
    assert "error" in legacy_terminal_events(ConsumerProfile.CORE_BUS)


@pytest.mark.unit
def test_terminal_protocol_canonical_json_is_stable_and_rejects_non_finite() -> None:
    value = {
        "z": datetime(2026, 8, 3, 1, 2, 3, 456, tzinfo=UTC),
        "a": [2, 1],
    }
    assert canonical_json_v1(value) == '{"a":[2,1],"z":"2026-08-03T01:02:03.000456Z"}'
    assert canonical_digest_v1(value) == canonical_digest_v1({"a": [2, 1], "z": value["z"]})
    with pytest.raises(ValueError, match="NaN"):
        canonical_json_v1({"bad": float("nan")})
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_json_v1({"bad": datetime(2026, 8, 3)})
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_json_v1({1: "ambiguous"})


@pytest.mark.unit
def test_s1_admission_contract_rejects_non_boolean_steer_policy() -> None:
    context = AdmissionContext(
        team_id="team_1",
        session_id="s_1",
        request_id="req_1",
        surface="web",
        cohort="bucket=1",
        actor="u_1",
        writer_instance_id="gateway_1",
        writer_lease_epoch=1,
        writer_admission_epoch=1,
        rollout_config_generation="rollout_1",
        auth_fence_vector_digest="auth_1",
    )
    with pytest.raises(TypeError, match="allow_steer"):
        TerminalAdmissionRequest(
            context=context,
            turn_id="t_1",
            user_text="hello",
            execution_intent={"text": "hello"},
            presentation_locale="zh-CN",
            message_catalog_version=1,
            allow_steer="yes",  # type: ignore[arg-type]
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "legacy"),
    [
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("aborted", "aborted"),
        ("clarification_pending", "clarification_pending"),
        ("approval_pending", "awaiting_approval"),
    ],
)
def test_ac_b1b_035_terminal_status_field_parity_fixture(status: str, legacy: str) -> None:
    assert legacy_status_for(status) == legacy


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "mode", "event_id", "expected"),
    [
        ("completed", "precommitted", "evt_answer", True),
        ("completed", "in_terminal_batch", "evt_answer", True),
        ("completed", "none", None, False),
        ("failed", "none", None, True),
        ("failed", "precommitted", "evt_partial", True),
        ("cancelled", "none", None, True),
        ("cancelled", "precommitted", "evt_partial", False),
        ("aborted", "none", None, True),
        ("clarification_pending", "none", None, True),
        ("approval_pending", "none", None, True),
    ],
)
def test_terminal_protocol_final_answer_mode_matrix(
    status: str,
    mode: str,
    event_id: str | None,
    expected: bool,
) -> None:
    assert validate_final_answer_mode(status=status, mode=mode, final_answer_event_id=event_id) is expected


def _decision_request(*, auth_digest: str = "auth_1") -> DecisionRecordRequest:
    scope = TerminalScope("team_1", "s_1", "t_1", 1)
    context = DecisionContext(
        scope=scope,
        decision_id=stable_decision_id(
            scope=scope,
            source_kind=DecisionSourceKind.CLARIFICATION,
            source_id="clarify_1",
            action=DecisionAction.RESUME,
        ),
        source_kind=DecisionSourceKind.CLARIFICATION,
        source_id="clarify_1",
        action=DecisionAction.RESUME,
        principals=DecisionPrincipalSet(
            requester_ref="user_1",
            actor_ref="user_1",
            effect_principal_ref="user_1",
        ),
        authz_snapshot_digest=auth_digest,
        prior_receipt_id="trc_pause_1",
    )
    return DecisionRecordRequest(
        context=context,
        decision_payload_digest=canonical_decision_payload_digest_for(
            source_kind=DecisionSourceKind.CLARIFICATION,
            action=DecisionAction.RESUME,
            expected_source_state="pending",
            target_source_state="resolved",
            source_values_json='{"chosen_value":"north"}',
        ),
        expected_source_state="pending",
        target_source_state="resolved",
        source_values_json='{"chosen_value":"north"}',
    )


@pytest.mark.unit
def test_s5_decision_identity_is_stable_and_excludes_auth_snapshot() -> None:
    first = _decision_request(auth_digest="auth_before")
    after_reauth = _decision_request(auth_digest="auth_after")
    assert first.context.decision_id == after_reauth.context.decision_id
    assert decision_identity_digest(first) == decision_identity_digest(after_reauth)
    assert stable_decision_action_id(first.context.decision_id, "resume") == stable_decision_action_id(
        after_reauth.context.decision_id,
        "resume",
    )


@pytest.mark.unit
def test_s5_control_contract_rejects_execution_actions_and_completed_status() -> None:
    request = _decision_request()
    recorded = RecordedDecision(
        context=request.context,
        state=DecisionState.DECISION_RECORDED,
        decision_digest=decision_identity_digest(request),
        target_generation=None,
        replayed=False,
    )
    with pytest.raises(ValueError, match="execution decision"):
        ControlGenerationRequest(
            decision=recorded,
            status=TerminalStatus.CANCELLED,
            reason_code="user_cancelled",
            message_key="turn.cancelled",
            writer_instance_id="gateway_1",
            writer_lease_epoch=1,
            writer_admission_epoch=1,
            current_authz_snapshot_digest="auth_1",
        )


@pytest.mark.unit
def test_s5_followup_capability_is_explicit_and_cannot_use_empty_generation() -> None:
    assert TerminalFollowupCapability(certified=False, adapter_generation="s5-default").certified is False
    with pytest.raises(ValueError, match="adapter generation"):
        TerminalFollowupCapability(certified=True, adapter_generation="")
