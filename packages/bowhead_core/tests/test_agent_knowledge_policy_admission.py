"""Source-policy admission, retained readers and preservation at the original writer."""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from bowhead_core.agent_execution_binding import AgentExecutionBindingError, AgentExecutionBindingStore
from bowhead_core.agent_knowledge_policy import KnowledgeAccessError, require_supported_knowledge_access
from bowhead_core.agent_revision_store import AgentAudience, AgentRevisionStore
from bowhead_core.subagent import PROFILES, _materialize_system_profile_snapshot, _system_profile_snapshot, _team_revision_projection
from test_agent_execution_binding import _create_team_agent, _insert_release, _setup
from test_agent_revision_store import _Clock, _db, _definition, _listing, _seed, _seed_eval_workspace, _Validator

_SELECTED = {"schema_version": 1, "mode": "selected_files", "file_ids": ["kbf_private"]}


@pytest.mark.parametrize(
    "policy",
    [
        None,
        {},
        {**_SELECTED, "schema_version": 2},
        "invalid",
        {**_SELECTED, "file_ids": None},
        {**_SELECTED, "file_ids": ["kbf_a", "kbf_a"]},
        {**_SELECTED, "defaults": {"top_k": True}},
        {**_SELECTED, "unknown": True},
    ],
)
def test_invalid_policy_never_becomes_legacy(policy):
    with pytest.raises(KnowledgeAccessError):
        require_supported_knowledge_access({"knowledge_access": policy})


@pytest.mark.parametrize("policy", [_SELECTED, {**_SELECTED, "file_ids": []}, {"schema_version": 1, "mode": "context_readable"}])
@pytest.mark.asyncio
async def test_original_writer_saves_canonical_policy_and_prevents_omission(tmp_path, monkeypatch, policy):
    monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "1")
    db_path = _db(tmp_path)
    _seed(db_path)
    validator = AsyncMock(return_value=True)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock(), knowledge_source_validator=validator)
    arguments = {
        "team_id": "team_release",
        "agent_id": "osa_release",
        "actor_user_id": "user_owner",
        "listing": _listing(),
        "audience": AgentAudience("private"),
    }
    working = await store.save_working_copy(**arguments, definition={**_definition(), "knowledge_access": policy}, expected_row_version=None)
    with pytest.raises(KnowledgeAccessError, match="preservation_required"):
        await store.save_working_copy(**arguments, definition=_definition(), expected_row_version=working.row_version)
    with sqlite3.connect(db_path) as db:
        saved = json.loads(db.execute("SELECT definition_json FROM team_subagent_working_copy WHERE id=?", (working.id,)).fetchone()[0])
    assert saved["knowledge_access"] == policy
    if policy.get("file_ids"):
        validator.assert_awaited_once_with(("kbf_private",), "team_release", "user_owner")
    monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "0")
    assert require_supported_knowledge_access(saved).canonical_value() == policy
    with pytest.raises(KnowledgeAccessError, match="admission_closed"):
        await store.save_working_copy(**arguments, definition=saved, expected_row_version=working.row_version)


@pytest.mark.asyncio
async def test_preparation_rechecks_current_file_access_before_freezing(tmp_path, monkeypatch):
    monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "1")
    db_path = _db(tmp_path)
    _seed(db_path)
    _seed_eval_workspace(db_path)
    validator = AsyncMock(return_value=True)
    store = AgentRevisionStore(str(db_path), validator=_Validator(), clock=_Clock(), knowledge_source_validator=validator)
    working = await store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition={**_definition(), "knowledge_access": _SELECTED},
        listing=_listing(),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )
    validator.return_value = False
    with pytest.raises(KnowledgeAccessError, match="source_unavailable"):
        await store.prepare_working_copy(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            expected_working_copy_id=working.id,
            expected_working_copy_version=working.row_version,
            expected_eval_policy_revision_id=None,
            workspace_id="ws_eval",
            idempotency_key="restricted-draft",
            release_note="Restrict knowledge sources",
        )
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM team_subagent_release_candidate").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_exact_admission_gate_closes_without_erasing_retained_binding(tmp_path, monkeypatch):
    fixture = await _setup(tmp_path)
    await _create_team_agent(fixture)
    await _insert_release(fixture, sequence=2, prompt="Read selected knowledge.", definition_overrides={"knowledge_access": _SELECTED})
    store = AgentExecutionBindingStore(fixture.db_path)
    args = {
        "team_id": fixture.team_id,
        "user_id": fixture.member_id,
        "turn_id": fixture.turn_id,
        "profile_name": "exact-agent",
        "dispatch_key": "restricted",
    }
    monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "0")
    with pytest.raises(AgentExecutionBindingError, match="admission_closed"):
        await store.admit_team_execution(**args)
    monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "1")
    binding = await store.admit_team_execution(**args)
    monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "0")
    resumed = await store.admit_team_execution(**args)
    assert resumed.execution_id == binding.execution_id
    assert resumed.definition["knowledge_access"] == _SELECTED


@pytest.mark.parametrize("source_kind", ["system", "team"])
def test_profile_materialization_retains_policy(source_kind):
    if source_kind == "system":
        definition = {**_system_profile_snapshot(PROFILES["compute"]), "knowledge_access": _SELECTED}
        policy = _materialize_system_profile_snapshot(definition, expected_name="compute").knowledge_access
    else:
        policy = _team_revision_projection({**_definition(), "knowledge_access": _SELECTED}, _listing(), expected_name="release-agent")[
            "knowledge_access"
        ]
    assert policy.canonical_value() == _SELECTED


def test_review_distinguishes_source_broadening_from_retrieval_defaults():
    from bowhead_core.agent_knowledge_policy import knowledge_access_expanded

    original = {"knowledge_access": _SELECTED}
    assert knowledge_access_expanded(original, {"knowledge_access": {"schema_version": 1, "mode": "context_readable"}})
    assert not knowledge_access_expanded(original, {"knowledge_access": {**_SELECTED, "file_ids": []}})
    assert not knowledge_access_expanded(original, {"knowledge_access": {**_SELECTED, "defaults": {"top_k": 2}}})


@pytest.mark.parametrize("resolved_policy", [None, {"schema_version": 1, "mode": "context_readable"}])
def test_plugin_resolution_cannot_drop_or_broaden_authored_source_policy(resolved_policy):
    from bowhead_core.agent_execution_binding import _binding_from_rows

    resolved = _definition()
    if resolved_policy is not None:
        resolved["knowledge_access"] = resolved_policy
    execution = {"agent_source_kind": "team", "resolved_definition_json": json.dumps(resolved)}
    snapshot = {"definition_json": json.dumps({**_definition(), "knowledge_access": _SELECTED})}
    with pytest.raises(AgentExecutionBindingError, match="resolution_changed"):
        _binding_from_rows(execution, snapshot)


@pytest.mark.parametrize("value,expected", [(None, True), ("0", False), ("1", True)])
def test_knowledge_admission_defaults_on_and_supports_explicit_shutdown(monkeypatch, value, expected):
    from bowhead_core.agent_knowledge_policy import source_bindings_enabled
    from bowhead_core.kb_search_budget import knowledge_direct_use_enabled

    for flag in ("FEATURE_KNOWLEDGE_PLUGIN_DIRECT_USE", "FEATURE_KNOWLEDGE_SOURCE_BINDINGS"):
        if value is None:
            monkeypatch.delenv(flag, raising=False)
        else:
            monkeypatch.setenv(flag, value)
    assert source_bindings_enabled() is expected
    assert knowledge_direct_use_enabled() is expected
