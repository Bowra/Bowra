"""Resource-only Coach discovery reuses live permissions and exact source contracts."""

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from bowhead_core.skill_bridge import build_default_skill_stack
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.tool_registry import build_default_registry
from bowhead_core.user_store import TeamSubagent
from bowhead_gateway.agent_coach_resources import MAX_EXACT_SKILL_FILE_BYTES, AgentCoachResources
from fastapi import HTTPException
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_team_subagent_drafts_routes import _run

pytestmark = pytest.mark.unit


@pytest.fixture
def resources(saved_draft):
    adapter, _, users, _, owner, _, _ = saved_draft
    request = adapter.editor.request
    request.app.state.skill_catalog = build_default_skill_stack()[0]
    request.app.state.skill_instance_store = SkillInstanceStore(users.db_path)
    request.app.state.builtin_skill_reference_metadata = build_builtin_skill_metadata()
    request.app.state.orchestrator = SimpleNamespace(tool_registry=build_default_registry())
    return AgentCoachResources(request, owner, "ws_a", "")


def search(resources, kind="tool", **kwargs):
    return _run(resources.search(kind=kind, query="", offset=0, limit=50, **kwargs))


def test_resource_search_without_draft_returns_exact_schema_and_stable_pages(resources):
    assert _run(resources.inspect())["writable"] is False
    page = _run(resources.search(kind="tool", query="", offset=0, limit=1))
    assert page["ok"] and page["next_offset"] == 1
    item = page["items"][0]
    assert item["can_read"] and item["coach_can_execute"] is False
    read = _run(resources.get(reference=item["reference"]))
    assert read["ok"] and read["resource"]["reference"] == item["reference"]
    assert "input_schema" in read["resource"] and read["resource"]["validation_evidence"] is None
    second = _run(resources.search(kind="tool", query="", offset=1, limit=1, expected_fingerprint=page["catalog_fingerprint"]))
    assert second["ok"] and second["items"][0]["reference"] != item["reference"]
    stale = {**item["reference"], "revision": "old"}
    assert _run(resources.get(reference=stale))["error"] == "resource_unavailable_or_changed"


def test_changed_schema_invalidates_reference_and_pagination(resources, monkeypatch):
    page = search(resources)
    item = page["items"][0]
    original = AgentCoachResources._tool_schemas

    async def updated(self):
        schemas = await original(self)
        schemas[item["reference"]["id"]] = {"type": "object", "required": ["new_input"]}
        return schemas

    monkeypatch.setattr(AgentCoachResources, "_tool_schemas", updated)
    assert search(resources, expected_fingerprint=page["catalog_fingerprint"])["error"] == "resource_catalog_changed"
    assert _run(resources.get(reference=item["reference"]))["error"] == "resource_unavailable_or_changed"


@pytest.mark.parametrize("table", ["users", "teams", "team_members", "workspace_members", "workspaces"])
def test_revocation_blocks_new_page_and_exact_read(resources, table):
    item = search(resources)["items"][0]
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute(f"UPDATE {table} SET status=?", ("archived" if table == "workspaces" else "removed",))
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        search(resources)
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        _run(resources.get(reference=item["reference"]))


def test_other_session_cannot_supply_resource_admission(resources):
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        _run(replace(resources, session_id="s_other").inspect())


def test_skill_exact_read_rejects_arbitrary_path_and_preserves_untrusted_role(resources):
    page = search(resources, "skill")
    assert page["items"], page
    method = page["items"][0]
    result = _run(resources.get(reference=method["reference"]))
    assert result["ok"], result
    assert "SKILL.md" in result["resource"]["files"]
    assert result["resource"]["content_role"] == "untrusted_resource_data"
    assert _run(resources.get(reference=method["reference"], file="../outside"))["error"] == "resource_file_unavailable_or_too_large"


def test_coach_granted_builtin_skill_files_fit_the_exact_read_cap():
    """Every text file of a built-in Skill granted to the Coach must stay exactly readable."""
    packages = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((packages / "bowhead_core/src/bowhead_core/agents/manifests/system/agent-coach.yaml").read_text(encoding="utf-8"))
    builtin = [grant["skill_ref"]["name"] for grant in manifest["skill_grants"] if grant["skill_ref"]["kind"] == "builtin"]
    assert builtin, "the Coach must keep at least one built-in authoring Skill"
    oversized = {}
    for name in builtin:
        package = packages / "bowhead_skill/catalog" / name
        assert (package / "SKILL.md").is_file(), name
        for path in package.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".txt", ".json", ".yaml", ".yml"} and path.stat().st_size > MAX_EXACT_SKILL_FILE_BYTES:
                oversized[str(path.relative_to(packages))] = path.stat().st_size
    assert not oversized, f"exceeds the {MAX_EXACT_SKILL_FILE_BYTES}-byte exact read cap: {oversized}"


def test_new_plugin_is_visible_but_missing_dependencies_are_not_exposed(resources):
    previous = search(resources, "capability")
    bundle = _run(
        TeamPluginStore(resources.store.db_path).create(
            team_id=resources.user.team_id,
            actor_user_id=resources.user.id,
            name="New bundle",
            description="Inspect dependencies",
            tool_refs=["private_unknown_tool"],
            skill_grants=[],
        )
    )
    assert search(resources, "capability", expected_fingerprint=previous["catalog_fingerprint"])["error"] == "resource_catalog_changed"
    page = search(resources, "capability")
    item = next(item for item in page["items"] if item["reference"]["id"] == bundle["id"])
    assert item["can_select"] is None and item["coach_can_execute"] is False
    assert _run(resources.get(reference=item["reference"]))["error"] == "resource_dependencies_unavailable"


def test_owned_legacy_agent_does_not_break_catalog_or_expose_other_owners(resources):
    for identity, owner in (("osa_mine", resources.user.id), ("osa_other", "user_other")):
        _run(
            resources.store.create_team_subagent(
                TeamSubagent(
                    id=identity,
                    team_id=resources.user.team_id,
                    name=identity,
                    created_by=owner,
                    visibility="creator_only",
                )
            )
        )
    page = search(resources, "agent")
    assert [item["reference"]["id"] for item in page["items"]] == ["osa_mine"]
    item = page["items"][0]
    assert item["restriction"] == "baseline_import_required" and not item["can_author"]
    detail = _run(resources.get(reference=item["reference"]))
    assert detail["ok"] and detail["resource"]["working_copy"] is None
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='user_other' WHERE id='osa_mine'")
    assert _run(resources.get(reference=item["reference"]))["error"] == "resource_unavailable_or_changed"


def test_start_draft_recovers_one_owned_target_then_saves_reviewable_fields(resources, saved_draft):
    intent = {"purpose": "Read supplied webpages", "locale": "en-US", "request_id": "create-one", "turn_id": "t_coach"}
    created = _run(resources.authoring.start_draft(**intent))
    assert created["ok"] and not created["agent_created"]
    assert created["target"]["id"] != saved_draft[3].id
    replay = _run(resources.authoring.start_draft(**intent))
    assert replay["target"] == created["target"]
    conflict = _run(resources.authoring.start_draft(**{**intent, "purpose": "Different purpose"}))
    assert conflict["error"] == "idempotency_key_reused"
    adapter = _run(resources.authoring.draft_adapter(created["target"]))
    baseline = _run(adapter.inspect())
    patch = {key: value for key, value in saved_draft[3].definition.items() if key not in {"visibility", "output_schema_text"}}
    patch["name"] = "coach-created"
    proposal = _run(adapter.propose(patch, request_id="initial-fields", turn_id="t_coach"))
    assert proposal["ok"] and not proposal["applied"], proposal
    assert _run(adapter.inspect())["definition"] == baseline["definition"]
    assert _run(resources.store.get_team_subagent_by_name(resources.user.team_id, "coach-created")) is None
    # Another Turn can resolve the same owned target and read the original receipt.
    resumed = _run(replace(resources, session_id="").authoring.draft_adapter(created["target"]))
    assert _run(resumed.get_proposal(proposal["proposal"]["id"]))["proposal"]["id"] == proposal["proposal"]["id"]
    client = saved_draft[-1]
    base = f"/api/v1/team-subagents/drafts/{created['target']['id']}"
    current = client.get(base + "?schema_version=2", headers={"X-Space-Id": "ws_a"}).json()
    applied = client.post(
        base + f"/suggestions/{proposal['proposal']['id']}/apply",
        headers={"X-Space-Id": "ws_a", "If-Match": current["etag"]},
        json={
            "schema_version": 2,
            "selected_fields": proposal["proposal"]["payload"]["changed_fields"],
            "mutation_id": "11111111-1111-4111-8111-111111111111",
        },
    )
    assert applied.status_code == 200, applied.text
    current = client.get(base + "?schema_version=2", headers={"X-Space-Id": "ws_a"}).json()
    validated = client.post(base + "/validate", headers={"X-Space-Id": "ws_a", "If-Match": current["etag"]})
    assert validated.status_code == 200, validated.text
    ready = validated.json()
    confirmed = client.post(
        base + "/create",
        headers={"X-Space-Id": "ws_a", "If-Match": ready["etag"]},
        json={"schema_version": 2, "mutation_id": ready["mutation_id"], "acknowledged_warning_codes": [item["code"] for item in ready["warnings"]]},
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["visibility"] == "creator_only"
    agent = _run(resources.store.get_team_subagent(resources.user.team_id, confirmed.json()["definition_id"]))
    assert agent.created_by == resources.user.id and agent.name == patch["name"]


def test_start_draft_rechecks_scope_in_the_write_transaction(resources, saved_draft, monkeypatch):
    store = saved_draft[1]
    create = store.create_manual_draft

    async def revoke_before_write(**kwargs):
        with sqlite3.connect(resources.store.db_path) as db:
            db.execute("UPDATE workspace_members SET status='removed'")
        return await create(**kwargs)

    monkeypatch.setattr(store, "create_manual_draft", revoke_before_write)
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        _run(resources.authoring.start_draft(purpose="Must not be saved", locale="en-US", request_id="revoke", turn_id="t_coach"))
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM team_subagent_draft").fetchone()[0] == 1


def test_explicit_draft_target_cannot_cross_owner_or_space(resources, saved_draft):
    _, store, _, draft, _, other, _ = saved_draft
    alien = _run(
        store.create_manual_draft(
            team_id=resources.user.team_id,
            owner_user_id=other.id,
            workspace_id=resources.workspace_id,
            locale="en-US",
            purpose="Private draft",
            idempotency_key="other-owner",
        )
    )
    for target in (
        {"kind": "agent_draft", "id": alien.id, "version": alien.row_version},
        {"kind": "agent", "id": draft.id, "version": draft.row_version},
    ):
        with pytest.raises((ValueError, HTTPException)):
            _run(resources.authoring.draft_adapter(target))
    with pytest.raises(ValueError, match="reference_scope_unavailable"):
        _run(
            replace(resources, workspace_id="ws_other").authoring.draft_adapter({"kind": "agent_draft", "id": draft.id, "version": draft.row_version})
        )


async def test_registered_start_and_explicit_target_work_without_selected_draft(resources):
    from bowhead_core.agent_coach import prepare_agent_coach, register_agent_coach_tools
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.events import TurnEventBus
    from bowhead_core.session_store import SessionStore
    from bowhead_core.tool_registry import ToolExecutionContext

    session = await SessionStore(resources.store.db_path).ensure_session(
        session_id=None,
        workspace_id=resources.workspace_id,
        user_id=resources.user.id,
    )
    ctx = ToolExecutionContext(
        role="lead",
        datarc=None,
        user_id=resources.user.id,
        team_id=resources.user.team_id,
        workspace_id=resources.workspace_id,
        session_id=session.id,
        turn_id="t_start",
        member_uid=resources.user.id,
        agent_coach_resources=replace(resources, session_id=session.id),
        parent_bus=TurnEventBus(),
    )
    child, admission = await prepare_agent_coach(ctx)
    assert admission["ok"] and child is not None
    registry = build_default_registry()
    register_agent_coach_tools(registry)
    token = set_current_peer_name("agent-coach")
    try:
        created = await registry.get("agent_draft_start").handler({"purpose": "Read webpages", "request_id": "one"}, child)
        assert created["ok"] and created["agent_created"] is False, created
        inspected = await registry.get("agent_authoring_inspect").handler({"target": created["target"]}, child)
        assert inspected["ok"] and inspected["draft_id"] == created["target"]["id"]
        assert child.subagent_draft_editor is None
    finally:
        reset_current_peer_name(token)
