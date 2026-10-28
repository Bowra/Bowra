"""Planner preview/confirm exact Agent identity boundary."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from bowhead_core.agent_role_candidates import (
    ExactAgentRoleChoice,
    LeadRoleCandidateRoster,
    LeadRoleChoice,
    new_turn_candidate,
)
from bowhead_gateway.routes.plans import _resolve_plan_agent_choice
from fastapi import HTTPException


def _candidate(*, candidate_id: str, preferred: bool = False):
    return new_turn_candidate(
        role_id="image",
        agent_id="system:marketing-analyst",
        runtime_profile_name="marketing-analyst",
        assignment_id=None,
        revision_ref="arv_image",
        release_id=None,
        behavior_fingerprint="sha256:" + "a" * 64,
        is_primary=True,
        is_preferred=preferred,
        display_name="图片设计师",
        description="生成图片",
        candidate_id=candidate_id,
    )


def _wire(choice: ExactAgentRoleChoice) -> dict[str, object]:
    return json.loads(choice.canonical_json())


def test_preview_choice_revalidates_against_current_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    candidate = _candidate(candidate_id="lrc_" + "a" * 32)
    roster = LeadRoleCandidateRoster.build({"image": (candidate,)})
    submitted = ExactAgentRoleChoice.freeze(
        choice=LeadRoleChoice("image", candidate.candidate_id, "task_match"),
        candidate=candidate,
    )

    resolved = _resolve_plan_agent_choice(
        agent_name="marketing-analyst",
        submitted=_wire(submitted),
        existing=None,
        role_roster=roster,
        role_profiles={"image": frozenset({"marketing-analyst"})},
    )

    assert resolved == submitted


def test_persisted_choice_survives_new_request_handle_but_not_new_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    old = _candidate(candidate_id="lrc_" + "a" * 32)
    existing = ExactAgentRoleChoice.freeze(
        choice=LeadRoleChoice("image", old.candidate_id, "task_match"),
        candidate=old,
    )
    same_identity = _candidate(candidate_id="lrc_" + "b" * 32)
    resolved = _resolve_plan_agent_choice(
        agent_name="marketing-analyst",
        submitted=None,
        existing=existing,
        role_roster=LeadRoleCandidateRoster.build({"image": (same_identity,)}),
        role_profiles={"image": frozenset({"marketing-analyst"})},
    )
    assert resolved.candidate_id == same_identity.candidate_id
    assert resolved.revision_ref == existing.revision_ref

    submitted_from_lead_card = _wire(existing)
    reminted = _resolve_plan_agent_choice(
        agent_name="marketing-analyst",
        submitted=submitted_from_lead_card,
        existing=existing,
        role_roster=LeadRoleCandidateRoster.build({"image": (same_identity,)}),
        role_profiles={"image": frozenset({"marketing-analyst"})},
    )
    assert reminted.candidate_id == same_identity.candidate_id

    changed = replace(
        same_identity,
        candidate_id="lrc_" + "c" * 32,
        revision_ref="arv_new_image",
    )
    with pytest.raises(HTTPException) as exc:
        _resolve_plan_agent_choice(
            agent_name="marketing-analyst",
            submitted=None,
            existing=existing,
            role_roster=LeadRoleCandidateRoster.build({"image": (changed,)}),
            role_profiles={"image": frozenset({"marketing-analyst"})},
        )
    assert exc.value.status_code == 409


def test_preferred_candidate_is_the_only_implicit_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    preferred = new_turn_candidate(
        role_id="image",
        agent_id="tsa_preferred",
        runtime_profile_name="marketing-analyst",
        assignment_id="asa_preferred",
        revision_ref="arv_preferred",
        release_id="arl_preferred",
        behavior_fingerprint="sha256:" + "d" * 64,
        is_primary=True,
        is_preferred=True,
        display_name="团队图片设计师",
        description="生成图片",
        candidate_id="lrc_" + "d" * 32,
    )
    other = _candidate(candidate_id="lrc_" + "e" * 32)

    resolved = _resolve_plan_agent_choice(
        agent_name="marketing-analyst",
        submitted=None,
        existing=None,
        role_roster=LeadRoleCandidateRoster.build({"image": (preferred, other)}),
        role_profiles={"image": frozenset({"marketing-analyst"})},
    )
    assert resolved.agent_id == "tsa_preferred"
    assert resolved.reason_code == "user_preference"
