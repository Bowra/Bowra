"""CI error detail on the Owner and Team API surfaces: grant replacement, proposal origin, gate failure summary."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalStore, AgentProposalBaseline
from bowhead_core.agent_version_automation import AgentVersionAutomationStore
from bowhead_core.openapi_store import OpenApiStore
from bowhead_core.public_entries import PublicEntryStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.routes import agents, team_openapi
from fastapi import HTTPException
from test_agents_routes import _enable_revisioned_agent
from test_agents_routes import app_client as app_client

from bowhead_core import agent_version_service

pytestmark = pytest.mark.unit

_EXPIRES = "2099-01-01T00:00:00+00:00"
_REPOSITORY = "acme/finance-agent"
_REF = "refs/heads/main"


async def _key(db_path, *, name: str, scopes: list[str]) -> tuple[OpenApiStore, dict]:
    store = OpenApiStore(str(db_path))
    key, _token = await store.create_key(team_id="team_1", name=name, scopes=scopes, expires_at=_EXPIRES)
    return store, key


async def _grant(db_path, key: dict, scopes: list[str]) -> dict:
    return await AgentVersionAutomationStore(str(db_path)).create_grant(
        team_id="team_1",
        agent_id="osa_1",
        actor_user_id="user_1",
        principal_id=key["id"],
        scopes=scopes,
        repositories=[_REPOSITORY],
        refs=[_REF],
        expires_at=_EXPIRES,
    )


def _request(db_path, openapi_store: OpenApiStore, path: str, method: str = "GET", **state) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(openapi_store=openapi_store, user_store=UserStore(db_path), **state)),
        method=method,
        url=SimpleNamespace(path=path),
    )


@pytest.mark.asyncio
async def test_owner_replaces_a_grant_in_one_request(app_client) -> None:
    client, db_path = app_client
    _enable_revisioned_agent(db_path)
    _, key = await _key(db_path, name="CI", scopes=["agent_versions:read"])
    old = await _grant(db_path, key, ["gate:read"])
    body = {
        "principal_id": key["id"],
        "scopes": ["gate:read"],
        "repositories": [_REPOSITORY],
        "refs": ["refs/heads/release"],
        "expires_at": _EXPIRES,
        "replaces_grant_id": old["id"],
    }

    created = await client.post("/api/v1/agents/osa_1/automation-grants", json=body)

    assert created.status_code == 201, created.text
    assert created.json()["replaced_grant_id"] == old["id"]
    new_id = created.json()["grant"]["id"]
    listed = {item["id"]: item["status"] for item in (await client.get("/api/v1/agents/osa_1/automation-grants")).json()["items"]}
    assert listed == {new_id: "active", old["id"]: "revoked"}

    # Replaying the same request finds the grant already replaced and creates nothing.
    replay = await client.post("/api/v1/agents/osa_1/automation-grants", json=body)
    assert replay.status_code == 404
    assert replay.json()["detail"]["error"]["code"] == "agent_automation_grant_not_found"
    assert len((await client.get("/api/v1/agents/osa_1/automation-grants")).json()["items"]) == 2

    plain = await client.post("/api/v1/agents/osa_1/automation-grants", json={**body, "replaces_grant_id": None})
    assert plain.status_code == 201 and plain.json()["replaced_grant_id"] is None


@pytest.mark.asyncio
async def test_owner_sees_which_repository_commit_and_key_sent_a_ci_proposal(app_client, monkeypatch) -> None:
    client, db_path = app_client
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    _enable_revisioned_agent(db_path)
    draft = await client.patch(
        "/api/v1/agents/osa_1/working-copy",
        json={
            "definition": {
                "max_iterations": 10,
                "model": None,
                "output_schema": None,
                "skill_grants": [],
                "system_prompt": "Initial private instructions.",
                "tool_refs": [],
            },
            "listing": {"description": "Owner draft", "display_name": "Finance Helper"},
            "audience": {"schema_version": 1, "effective_mode": "private", "user_ids": [], "group_ids": []},
            "expected_row_version": None,
        },
    )
    assert draft.status_code == 200, draft.text
    working = (await client.get("/api/v1/agents/osa_1/version-workbench")).json()["next_draft_identity"]
    openapi_store, key = await _key(db_path, name="ci-prod", scopes=["agent_versions:write"])
    grant = await _grant(db_path, key, ["proposal:create"])
    operations = [{"op": "replace", "path": "/listing/description", "value": "Reviewed CI change"}]
    canonical = json.dumps(operations, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body = team_openapi.AutomationProposalBody(
        grant_id=grant["id"],
        repository=_REPOSITORY,
        ref=_REF,
        commit_sha="ABCDEF1",
        base_release_id=working["base_release_id"],
        working_copy_id=working["id"],
        working_copy_row_version=working["row_version"],
        patch_hash="sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        operations=operations,
        atomic_groups=[{"id": "listing", "operation_indexes": [0], "depends_on": []}],
    )
    created = await team_openapi.create_automation_agent_proposal(
        "osa_1",
        body,
        _request(db_path, openapi_store, "/api/openapi/v1/agents/osa_1/version-proposals", "POST"),
        "origin-proposal",
        {**key, "team_id": "team_1"},
    )
    proposal_path = f"/api/v1/agents/osa_1/change-proposals/{created['proposal_id']}"
    expected = {
        "repository": _REPOSITORY,
        "ref": _REF,
        "commit_sha": "abcdef1",
        "grant_id": grant["id"],
        "key_name": "ci-prod",
    }
    assert (await client.get(proposal_path)).json()["origin"] == expected
    listed = (await client.get("/api/v1/agents/osa_1/change-proposals")).json()["items"]
    assert [item["origin"] for item in listed] == [expected]

    await openapi_store.set_status("team_1", key["id"], "deleted")
    assert (await client.get(proposal_path)).json()["origin"] == {**expected, "key_name": None}

    coach = await AgentChangeProposalStore(str(db_path)).create(
        team_id="team_1",
        agent_id="osa_1",
        effective_owner_user_id="user_1",
        source_type="coach",
        source_ref="coach:turn_origin:edit",
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Coach instructions."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0]}],
        expected_baseline=AgentProposalBaseline(working["id"], working["row_version"], working["base_release_id"]),
    )
    assert (await client.get(f"/api/v1/agents/osa_1/change-proposals/{coach.id}")).json()["origin"] is None


@pytest.mark.asyncio
async def test_ci_proposal_with_a_non_finite_number_is_an_invalid_patch(app_client, monkeypatch) -> None:
    _client, db_path = app_client
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    _enable_revisioned_agent(db_path)
    openapi_store, key = await _key(db_path, name="ci-nan", scopes=["agent_versions:write"])
    grant = await _grant(db_path, key, ["proposal:create"])
    operations = [{"op": "replace", "path": "/definition/max_iterations", "value": float("nan")}]
    # The client can hash NaN with Python's default json.dumps; the server must still refuse it as a 422.
    canonical = json.dumps(operations, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body = team_openapi.AutomationProposalBody(
        grant_id=grant["id"],
        repository=_REPOSITORY,
        ref=_REF,
        commit_sha="abcdef1",
        base_release_id="arl_missing",
        working_copy_id="awc_missing",
        working_copy_row_version=1,
        patch_hash="sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        operations=operations,
        atomic_groups=[{"id": "behavior", "operation_indexes": [0], "depends_on": []}],
    )
    with pytest.raises(HTTPException) as refused:
        await team_openapi.create_automation_agent_proposal(
            "osa_1",
            body,
            _request(db_path, openapi_store, "/api/openapi/v1/agents/osa_1/version-proposals", "POST"),
            "nan-proposal",
            {**key, "team_id": "team_1"},
        )
    assert refused.value.status_code == 422
    assert refused.value.detail == {"code": "agent_change_proposal_invalid_patch", "reason": "non_finite_number"}


_COUNTS = {
    "required_failed": 2,
    "introduced": 1,
    "also_failing_on_live": 0,
    "not_run_on_live": 1,
    "unmet_points": [{"case_key": "quality.1", "point_id": "p_3f2a"}],
}
_OVERVIEW = {"cases": 4, "candidate_pass_rate": 0.5, "live_pass_rate": 1.0, "newly_failing": 2, "newly_passing": 0}


async def _gate(db_path, monkeypatch, *, snapshot: dict | None, entries: PublicEntryStore | None = None) -> dict:
    openapi_store, key = await _key(db_path, name="Gate CI", scopes=["agent_versions:read"])
    grant = await _grant(db_path, key, ["gate:read"])

    class _Service:
        async def workbench(self, **_kwargs):
            return {
                "candidate_identity": {"candidate_id": "arc_gate", "candidate_hash": "sha256:" + "2" * 64},
                "active_snapshot_identity": snapshot,
                "readiness": "blocked" if snapshot else "missing",
                "reason_codes": ["required_quality_failed"] if snapshot else [],
                "primary_action": {"kind": "rebase_draft", "enabled": True, "disabled_reason_codes": []},
                "comparison_overview": _OVERVIEW if snapshot else None,
            }

    async def _service(*_args, **_kwargs):
        return _Service()

    monkeypatch.setattr(agents, "_version_service", _service)
    state = {"public_entry_store": entries} if entries is not None else {}
    return await team_openapi.read_automation_agent_gate(
        "osa_1",
        "arc_gate",
        grant["id"],
        _REPOSITORY,
        _REF,
        _request(db_path, openapi_store, "/api/openapi/v1/agents/osa_1/release-candidates/arc_gate/gate", **state),
        {**key, "team_id": "team_1"},
    )


@pytest.mark.asyncio
async def test_gate_failure_summary_is_null_before_a_snapshot(app_client, monkeypatch) -> None:
    _client, db_path = app_client
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    _enable_revisioned_agent(db_path)
    gate = await _gate(db_path, monkeypatch, snapshot=None)
    assert gate["failure_summary"] is None
    assert gate["comparison_overview"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_entry", [True, False])
async def test_gate_failure_summary_links_the_owner_through_the_team_entry(app_client, monkeypatch, with_entry) -> None:
    """Counts come from the core reader (covered on real comparisons in core); here the link is under test."""
    _client, db_path = app_client
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    _enable_revisioned_agent(db_path)
    entries = PublicEntryStore(str(db_path))
    await entries.initialize()
    if with_entry:
        await entries.create_bootstrap_active(team_id="team_1", base_url="https://agents.example.com/bowhead", actor_id="user_1")
    reads: list[dict] = []

    async def _counts(_db, **kwargs):
        reads.append(kwargs)
        return dict(_COUNTS)

    # The gate reads the counts through the Release tab outcome it shares with Coach (release_check_outcome_conn).
    monkeypatch.setattr(agent_version_service, "read_snapshot_failure_summary_conn", _counts)

    gate = await _gate(db_path, monkeypatch, snapshot={"id": "aevs_gate", "hash": "sha256:" + "4" * 64, "execution_id": "aec_gate"}, entries=entries)

    assert reads == [{"team_id": "team_1", "agent_id": "osa_1", "candidate_id": "arc_gate", "snapshot_id": "aevs_gate"}]
    owner_url = "https://agents.example.com/bowhead/agents/manage/?agent_id=osa_1&section=release" if with_entry else None
    assert gate["failure_summary"] == {**_COUNTS, "owner_url": owner_url}
    # CI reads the same content-free overview the Owner sees (R15).
    assert gate["comparison_overview"] == _OVERVIEW


@pytest.mark.asyncio
async def test_team_api_reference_documents_the_ci_contract(app_client, monkeypatch) -> None:
    """The reference states the rules a CI author needs and the hash rule the server really applies."""
    _client, db_path = app_client
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(gateway_extensions=SimpleNamespace(organization_api_reference=()))))
    monkeypatch.setattr(team_openapi, "_local", lambda _request: None)
    monkeypatch.setattr(team_openapi, "available_scopes", lambda _request: {"agent_versions:read", "agent_versions:write"})
    reference = team_openapi._reference_markdown(await team_openapi.documentation(request, caller=None))  # type: ignore[arg-type]

    for expected in (
        "`atomic_groups`",
        "`depends_on`",
        "`/listing/description`",
        "`/definition/model_policy/`",
        'json.dumps(operations, sort_keys=True, separators=(",", ":"), ensure_ascii=False)',
        "examples/agent-ci/",
        "`evaluation_context.workspace_id`",
        "`evaluation_context.policy.revision_id`",
        "`failure_summary`",
        "`also_failing_on_live`",
        "`failure_summary.unmet_points`",
        "`comparison_overview`",
        "`409 agent_eval_plan_behind`",
    ):
        assert expected in reference, expected
