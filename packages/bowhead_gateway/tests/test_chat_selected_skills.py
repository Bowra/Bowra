"""A Skill-only `/` send admitted as an ordinary Lead Turn.

docs/sprints/skill-invocation-2026-11-17/tech-design.md §10.2 (slice S2).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.skill_read_material import SkillReadMaterialError
from bowhead_core.skill_read_resolution import SkillReadResolutionError, SkillReadSourceResolver
from bowhead_gateway.routes.chat import ChatSendRequest
from bowhead_gateway.selected_skills import admit_selected_skills
from fastapi import HTTPException
from pydantic import ValidationError
from test_chat_reference_submission import fingerprint  # noqa: F401

pytestmark = pytest.mark.unit
SKILL = {"kind": "skill", "source_kind": "user", "id": "sk_c029829564ae", "version": "sha256:" + "b" * 64}
OTHER = {**SKILL, "id": "sk_other"}


def payload(**changes):
    return {
        "session_id": "s_skill",
        "workspace_id": "ws_test",
        "message": "/comprehensive-business-report 分析2026年1-10月的经营情况",
        "selected_skills": [SKILL],
        "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "planning_mode": "off",
        **changes,
    }


def test_selected_skills_are_semantic_input_of_the_submission(fingerprint):  # noqa: F811
    request = ChatSendRequest.model_validate(payload())
    assert request.selected_skills == [SkillResourceRef.model_validate(SKILL)]
    assert fingerprint(payload()) != fingerprint(payload(selected_skills=[OTHER]))
    plain = payload()
    del plain["selected_skills"]
    assert fingerprint(payload()) != fingerprint(plain)


@pytest.mark.parametrize(
    "change",
    [
        {"selected_skills": []},
        {"selected_skills": [SKILL, SKILL]},
        {"selected_skills": [SKILL, OTHER, {**SKILL, "id": "sk_3"}, {**SKILL, "id": "sk_4"}]},
        {"selected_skills": [{**SKILL, "kind": "agent"}]},
        {"selected_skills": [{**SKILL, "version": "1.1.0"}]},
        {"model_control_batch": "references"},
        {"submission_kind": "steer", "expected_parent_turn_id": "t_prior", "expected_execution_generation": 1},
        {"client_submission_id": None, "submission_kind": None, "submission_contract_version": None},
        {"plan_execution_id": "plan_other"},
        {"client_info": {"agent_draft_id": "sad_other"}},
        {"context_maintenance": {"contract": "context-maintenance.v1", "expected_generation": "mcg_" + "a" * 32}, "message": ""},
    ],
)
def test_selected_skills_need_an_ordinary_durable_start(change):
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate(payload(**change))


def _request(tmp_path, prepare) -> SimpleNamespace:
    db_path = str(tmp_path / "core.db")
    resolver = SkillReadSourceResolver(db_path, access_factory=lambda: None)
    resolver.prepare = prepare  # type: ignore[method-assign]
    orchestrator = SimpleNamespace(session_store=SimpleNamespace(db_path=db_path), reference_skill_sources_factory=lambda scope: resolver)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator)))


async def _admit(request, skills):
    return await admit_selected_skills(request, skills, team_id="team_1", user_id="u_1", workspace_id="ws_1", session_id=None)


@pytest.mark.asyncio
async def test_each_selected_skill_is_prepared_with_the_request_scope(tmp_path, monkeypatch):
    seen = []

    async def prepare(resource, *, scope, require_declaration=False):
        seen.append((resource.id, scope.team_id, scope.user_id, scope.workspace_id, scope.session_id))

    skills = [SkillResourceRef.model_validate(SKILL), SkillResourceRef.model_validate(OTHER)]
    assert await _admit(_request(tmp_path, prepare), skills) == tuple(skills)
    assert seen == [("sk_c029829564ae", "team_1", "u_1", "ws_1", None), ("sk_other", "team_1", "u_1", "ws_1", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SkillReadMaterialError("reference_skill_version_changed"), "selected_skill_changed"),
        # The source or the sender's access kept changing during the check: not an edit of the Skill.
        (SkillReadResolutionError("reference_skill_source_changed"), "selected_skill_check_interrupted"),
        (SkillReadMaterialError("reference_skill_material_too_large"), "selected_skill_too_large"),
        (SkillReadResolutionError("reference_skill_source_unavailable"), "selected_skill_unavailable"),
        (RuntimeError("store down"), "selected_skill_unavailable"),
    ],
)
async def test_refusals_name_the_failing_condition(tmp_path, monkeypatch, error, code):
    """A refusal names its condition; a source that changed during the check is checked once more first."""

    async def prepare(*args, **kwargs):
        raise error

    with pytest.raises(HTTPException) as caught:
        await _admit(_request(tmp_path, prepare), [SkillResourceRef.model_validate(SKILL)])
    assert caught.value.detail == {"error": {"code": code, "admission": "not_started"}}


@pytest.mark.asyncio
async def test_an_access_change_during_the_check_is_checked_again(tmp_path):
    """116 2026-11-22: another session added the sender to a group mid-check; a second check admits the send."""
    calls = []

    async def prepare(resource, *, scope, require_declaration=False):
        calls.append(resource.id)
        if len(calls) == 1:
            raise SkillReadResolutionError("reference_skill_source_changed")

    skills = [SkillResourceRef.model_validate(SKILL)]
    assert await _admit(_request(tmp_path, prepare), skills) == tuple(skills)
    assert calls == ["sk_c029829564ae", "sk_c029829564ae"]


@pytest.mark.asyncio
async def test_an_edited_skill_is_not_checked_again(tmp_path):
    calls = []

    async def prepare(resource, *, scope, require_declaration=False):
        calls.append(resource.id)
        raise SkillReadMaterialError("reference_skill_version_changed")

    with pytest.raises(HTTPException) as caught:
        await _admit(_request(tmp_path, prepare), [SkillResourceRef.model_validate(SKILL)])
    assert caught.value.detail["error"]["code"] == "selected_skill_changed" and len(calls) == 1
