"""Exact resource choices survive proposal acceptance and private creation."""

import json
import sqlite3

import pytest
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.team_subagent_store import TeamSubagentStore
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_plugins import exact_method
from test_agent_coach_resources import resources as resources
from test_team_subagent_drafts_routes import _run

pytestmark = pytest.mark.unit


def activate(resources):
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at='2026-10-03T00:00:00Z' WHERE singleton=1")


def plugin(resources):
    return _run(
        TeamPluginStore(resources.store.db_path).create(
            team_id=resources.user.team_id,
            actor_user_id=resources.user.id,
            name="Evidence Plugin",
            description="Use shared methods.",
            tool_refs=["web_fetch"],
            skill_grants=[],
        )
    )


def accept(saved_draft, patch):
    adapter, _, _, draft, _, _, client = saved_draft
    receipt = _run(adapter.propose(patch, request_id="bindings", turn_id="t_original"))
    assert receipt["ok"], receipt
    url = f"/api/v1/team-subagents/drafts/{draft.id}"
    headers = {"X-Space-Id": "ws_a", "If-Match": f'W/"{draft.id}:{draft.row_version}"'}
    applied = client.post(
        url + f"/suggestions/{receipt['proposal']['id']}/apply",
        headers=headers,
        json={"schema_version": 2, "selected_fields": list(patch), "mutation_id": "11111111-1111-4111-8111-111111111111"},
    )
    assert applied.status_code == 200, applied.text
    return url, applied.json()


def review(client, url, draft):
    result = client.post(url + "/validate", headers={"X-Space-Id": "ws_a", "If-Match": draft["etag"]})
    assert result.status_code == 200, result.text
    assert result.json()["actions"]["create"], result.text
    return result.json()


def create(client, url, draft):
    return client.post(
        url + "/create",
        headers={"X-Space-Id": "ws_a", "If-Match": draft["etag"]},
        json={"schema_version": 2, "mutation_id": draft["mutation_id"], "acknowledged_warning_codes": [w["code"] for w in draft["warnings"]]},
    )


def test_proposal_accept_save_review_create_and_replay_preserve_exact_bindings(resources, saved_draft, monkeypatch):
    activate(resources)
    grant = exact_method(resources)
    bundle = plugin(resources)
    patch = {"tool_refs": [], "skill_grants": [grant], "capability_ids": [bundle["id"]]}
    url, applied = accept(saved_draft, patch)
    client = saved_draft[-1]
    assert all(applied["definition"][key] == value for key, value in patch.items())
    saved = client.patch(
        url,
        headers={"X-Space-Id": "ws_a", "If-Match": applied["etag"]},
        json={
            "schema_version": 2,
            "purpose": applied["purpose"],
            "definition": {**applied["definition"], "description": "Edited by the Owner."},
            "text_only_confirmed": False,
        },
    )
    assert saved.status_code == 200, saved.text
    reviewed = review(client, url, saved.json())
    created = create(client, url, reviewed)
    assert created.status_code == 201, created.text
    identity = created.json()["definition_id"]
    with sqlite3.connect(resources.store.db_path) as db:
        definitions = db.execute("SELECT definition_json FROM team_subagent_revision WHERE subagent_id=?", (identity,)).fetchall()
    assert len(definitions) == 1
    definition = json.loads(definitions[0][0])
    assert definition["skill_grants"] == [grant] and definition["capability_ids"] == [bundle["id"]]
    replay = create(client, url, reviewed)
    assert replay.status_code == 200 and replay.json()["replayed"] and replay.json()["definition_id"] == identity


@pytest.mark.parametrize("change", ["plugin", "scope", "membership"])
def test_dependency_change_at_final_transaction_prevents_creation(resources, saved_draft, monkeypatch, change):
    activate(resources)
    bundle = plugin(resources)
    url, applied = accept(saved_draft, {"capability_ids": [bundle["id"]]})
    client = saved_draft[-1]
    reviewed = review(client, url, applied)
    original = TeamSubagentStore.create_team_subagent

    async def changed(self, *args, **kwargs):
        if change == "plugin":
            await TeamPluginStore(self.db_path).update(
                team_id=resources.user.team_id,
                plugin_id=bundle["id"],
                expected_row_version=bundle["row_version"],
                actor_user_id=resources.user.id,
                name=bundle["name"],
                description="Concurrent edit",
                tool_refs=bundle["tool_refs"],
                skill_grants=[],
            )
        else:
            with sqlite3.connect(self.db_path) as db:
                if change == "scope":
                    db.execute("UPDATE workspace_members SET status='removed' WHERE workspace_id='ws_a'")
                else:
                    db.execute("UPDATE team_members SET status='removed' WHERE user_id=?", (resources.user.id,))
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(TeamSubagentStore, "create_team_subagent", changed)
    result = create(client, url, reviewed)
    assert result.status_code in {403, 409, 422}, result.text
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM team_subagent").fetchone()[0] == 0
        assert db.execute("SELECT status FROM team_subagent_draft WHERE id=?", (saved_draft[3].id,)).fetchone()[0] == "ready"


def test_changed_plugin_invalidates_saved_review(resources, saved_draft):
    activate(resources)
    bundle = plugin(resources)
    url, applied = accept(saved_draft, {"capability_ids": [bundle["id"]]})
    client = saved_draft[-1]
    reviewed = review(client, url, applied)
    _run(
        TeamPluginStore(resources.store.db_path).update(
            team_id=resources.user.team_id,
            plugin_id=bundle["id"],
            expected_row_version=bundle["row_version"],
            actor_user_id=resources.user.id,
            name=bundle["name"],
            description="Updated shared purpose",
            tool_refs=bundle["tool_refs"],
            skill_grants=[],
        )
    )
    result = create(client, url, reviewed)
    assert result.status_code == 409 and result.json()["detail"]["error"]["code"] == "subagent_catalog_changed", result.text


def test_exact_personal_source_survives_same_named_builtin_and_follows_its_latest_bytes(resources, saved_draft, tmp_path):
    from bowhead_core.skill_instance_store import SkillInstanceStore

    activate(resources)
    directory = tmp_path / "personal-method"
    directory.mkdir()
    document = directory / "SKILL.md"
    document.write_text("---\nname: agent-authoring\ndescription: Personal evidence method\n---\nRead supplied evidence.\n")
    instance = _run(
        SkillInstanceStore(resources.store.db_path).upsert(
            user_id=resources.user.id,
            name="agent-authoring",
            source_type="user_upload",
            source_path=str(directory),
        )
    )
    page = _run(resources.search(kind="skill", query="agent-authoring", offset=0, limit=50))
    item = next(item for item in page["items"] if item["reference"]["id"] == instance.id)
    grant = _run(resources.get(reference=item["reference"]))["resource"]["exact_read_grant"]
    assert grant["skill_ref"]["kind"] == "user"
    url, applied = accept(saved_draft, {"skill_grants": [grant]})
    reviewed = review(saved_draft[-1], url, applied)
    document.write_text(document.read_text() + "Changed after review.\n")
    # Skills follow their latest revision (agent-skill-gates D2): an edit after
    # review does not invalidate the review, and the release records the new bytes.
    result = create(saved_draft[-1], url, reviewed)
    assert result.status_code == 201, result.text
    with sqlite3.connect(resources.store.db_path) as db:
        stored = json.loads(
            db.execute("SELECT definition_json FROM team_subagent_revision WHERE subagent_id=?", (result.json()["definition_id"],)).fetchone()[0]
        )
    assert stored["skill_grants"] == [{**grant, "revision": f"sha256:{bundle_sha256(directory)}"}]


def test_implied_wrapper_denial_is_rechecked_before_final_create(resources, saved_draft, monkeypatch):
    from bowhead_core.access_control import AccessControlStore

    activate(resources)
    url, applied = accept(saved_draft, {"skill_grants": [exact_method(resources)]})
    reviewed = review(saved_draft[-1], url, applied)
    original = TeamSubagentStore.create_team_subagent

    async def revoke(self, *args, **kwargs):
        await AccessControlStore(self.db_path).create_or_update_resource_grant(
            team_id=resources.user.team_id,
            resource_kind="tool",
            resource_id="skill_read",
            principal_type="user",
            principal_id=resources.user.id,
            permission="use",
            effect="deny",
        )
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(TeamSubagentStore, "create_team_subagent", revoke)
    result = create(saved_draft[-1], url, reviewed)
    assert result.status_code == 422, result.text
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM team_subagent").fetchone()[0] == 0


def test_creation_binding_changes_shared_plugin_impact(resources, saved_draft):
    from test_agent_coach_plugins import administrator

    activate(resources)
    administrator(resources)
    bundle = plugin(resources)
    before = _run(resources.plugins.call("agent_plugin_inspect", {"plugin_id": bundle["id"]}, turn_id="t_original"))
    accept(saved_draft, {"capability_ids": [bundle["id"]]})
    after = _run(resources.plugins.call("agent_plugin_inspect", {"plugin_id": bundle["id"]}, turn_id="t_current"))
    assert before["impact"]["creation_draft_count"] == 0
    assert after["impact"]["creation_draft_count"] == 1
    assert before["impact"]["fingerprint"] != after["impact"]["fingerprint"]
    with pytest.raises(ValueError, match="impact_changed"):
        _run(
            resources.plugins.call(
                "agent_plugin_save",
                {
                    "target": before["target"],
                    "expected_impact": before["impact"]["fingerprint"],
                    "patch": {"description": "New shared purpose"},
                    "request_id": "stale-impact",
                },
                turn_id="t_current",
            )
        )
