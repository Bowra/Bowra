"""Original Space metadata saves retain exact identity, revision and permissions."""

from types import SimpleNamespace

import httpx2
import pytest
from bowhead_core.config_copilot import reset_registry_for_tests
from bowhead_core.config_copilot_chat_tools import set_proposal_writer_provider
from bowhead_core.config_draft_store import ConfigDraftStore
from bowhead_core.configuration_agent import prepare_configuration_agent
from bowhead_core.events import TurnEventBus
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_store import UserStore
from bowhead_core.workspace_settings_store import WorkspaceSettingsStore
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from bowhead_gateway.routes import config_proposals
from bowhead_gateway.routes.workspace_settings import router as workspace_router
from fastapi import FastAPI
from test_configuration_agent_personal_settings import _call

pytestmark = pytest.mark.unit


@pytest.fixture
async def metadata_ctx(tmp_path, monkeypatch, initialize_migrated_core_database):
    database = tmp_path / "metadata.db"
    initialize_migrated_core_database(database)
    app = FastAPI()
    app.include_router(workspace_router)
    store = UserStore(database)
    await store.initialize()
    team = await store.create_team(name="Metadata Team", max_seats=10)
    admin = await store.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!")
    outsider = await store.create_user(team_id=team.id, email="outsider@example.com", password="Passw0rd!")
    workspace_id = await store.create_local_workspace(team_id=team.id, name="Original Space", creator_user_id=admin.id)
    app.state.user_store = store
    app.state.workspace_settings_store = WorkspaceSettingsStore(database)
    await app.state.workspace_settings_store.init()
    app.state.ws_id = workspace_id
    app.state.admin_id = admin.id
    app.state.random_id = outsider.id
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user=admin.id)
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    reset_registry_for_tests()
    register_gateway_forms(app)
    app.state.config_draft_store = ConfigDraftStore(app.state.user_store.db_path)
    await app.state.config_draft_store.initialize()
    app.include_router(config_proposals.router)
    set_proposal_writer_provider(lambda: app.state.config_draft_store)
    user = await app.state.user_store.get_user(app.state.admin_id)
    ctx = SimpleNamespace(app=app, user=user, workspace_id=app.state.ws_id)
    yield ctx
    set_proposal_writer_provider(None)
    reset_registry_for_tests()


async def specialist(ctx, user_id=None):
    lead = ToolExecutionContext(
        user_id=user_id or ctx.user.id,
        team_id=ctx.user.team_id,
        member_uid=user_id or ctx.user.id,
        workspace_id=ctx.workspace_id,
        session_id="s_metadata",
        turn_id="t_metadata",
        role="lead",
        datarc=None,
        config_lite_supported=True,
        user_text="Rename this Space to Finance",
        parent_bus=TurnEventBus(),
    )
    child, _ = await prepare_configuration_agent(lead, form_id="workspace_metadata")
    return child


async def proposal(ctx, **patch):
    child = await specialist(ctx)
    before = await _call(ctx, child, "configuration_read", form_id="workspace_metadata")
    assert before["editable_fields"] == ["description", "name"]
    assert before["actions"] == ["read", "propose", "confirm", "open"]
    assert before["field_constraints"] == {"name": {"minLength": 1, "maxLength": 120}, "description": {"maxLength": 2000}}
    result = await _call(ctx, child, "configuration_propose", form_id="workspace_metadata", patch=patch)
    assert result["ok"], result
    return child, result


async def test_open_review_and_confirm_use_original_governed_service(metadata_ctx):
    ctx = metadata_ctx
    original = await ctx.app.state.user_store.get_workspace(ctx.workspace_id)
    child, result = await proposal(ctx, name="Finance", description="Shared analysis")
    opened = await _call(ctx, child, "configuration_open", form_id="workspace_metadata")
    assert opened["ok"] and opened["status"] == "open_requested" and opened["saved"] is False
    assert opened["ui_status"] == "unacknowledged"
    assert opened["target"] == {"page": "space_general", "user_id": ctx.user.id, "team_id": ctx.user.team_id, "workspace_id": ctx.workspace_id}
    assert await ctx.app.state.user_store.get_workspace(ctx.workspace_id) == original
    headers = {"x-bowhead-user-id": ctx.user.id}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=ctx.app), base_url="http://gw", headers=headers) as client:
        review = await client.get(f"/api/v1/config-proposals/{result['proposal_id']}/settings-review")
        assert review.status_code == 200, review.text
        assert all(row["baseline_revision"] == original["revision"] for row in review.json()["rows"])
        assert await ctx.app.state.user_store.get_workspace(ctx.workspace_id) == original
        refusal = await _call(ctx, child, "configuration_apply", form_id="workspace_metadata", proposal_id=result["proposal_id"])
        assert refusal["error"] == "user_confirmation_required"
        saved = await client.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")
        assert saved.status_code == 200, saved.text
        readback = (await client.get(f"/api/v1/workspace-settings/{ctx.workspace_id}/metadata")).json()
        assert readback == {"name": "Finance", "description": "Shared analysis", "revision": original["revision"] + 1}
        replay = await client.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")
        assert replay.json()["replayed"] is True
        assert (await client.get(f"/api/v1/workspace-settings/{ctx.workspace_id}/metadata")).json() == readback
        assert (await client.post(f"/api/v1/config-proposals/{result['proposal_id']}/undo")).status_code == 409


async def test_newer_manual_revision_blocks_confirmation_even_for_an_unchanged_affected_field(metadata_ctx):
    ctx = metadata_ctx
    _, result = await proposal(ctx, name="Finance")
    headers = {"x-bowhead-user-id": ctx.user.id}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=ctx.app), base_url="http://gw", headers=headers) as client:
        current = (await client.get(f"/api/v1/workspace-settings/{ctx.workspace_id}/metadata")).json()
        manual = await client.patch(
            f"/api/v1/workspace-settings/{ctx.workspace_id}/metadata",
            json={"description": "New manual description", "expected_revision": current["revision"]},
        )
        assert manual.status_code == 200, manual.text
        failed = await client.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")
        assert failed.status_code == 409
        assert failed.json()["detail"]["error"]["code"] == "configuration_baseline_changed"
        assert (await client.get(f"/api/v1/workspace-settings/{ctx.workspace_id}/metadata")).json() == manual.json()


async def test_confirming_values_the_space_already_holds_is_applied_without_a_write(metadata_ctx):
    ctx = metadata_ctx
    _, result = await proposal(ctx, name="Finance")
    headers = {"x-bowhead-user-id": ctx.user.id}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=ctx.app), base_url="http://gw", headers=headers) as client:
        current = (await client.get(f"/api/v1/workspace-settings/{ctx.workspace_id}/metadata")).json()
        manual = await client.patch(
            f"/api/v1/workspace-settings/{ctx.workspace_id}/metadata",
            json={"name": "Finance", "expected_revision": current["revision"]},
        )
        assert manual.status_code == 200, manual.text
        applied = await client.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"
        assert (await client.get(f"/api/v1/workspace-settings/{ctx.workspace_id}/metadata")).json() == manual.json()


async def test_actual_users_permissions_and_revocation_filter_metadata_and_actions(metadata_ctx):
    ctx = metadata_ctx
    outsider = await specialist(ctx, ctx.app.state.random_id)
    denied = await _call(ctx, outsider, "configuration_read", form_id="workspace_metadata")
    assert not denied["ok"] and "fields" not in denied
    _, result = await proposal(ctx, name="Finance")
    await ctx.app.state.user_store.add_member(workspace_id=ctx.workspace_id, user_id=ctx.user.id, role="member")
    before = await ctx.app.state.user_store.get_workspace(ctx.workspace_id)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=ctx.app), base_url="http://gw", headers={"x-bowhead-user-id": ctx.user.id}
    ) as client:
        assert (await client.get(f"/api/v1/config-proposals/{result['proposal_id']}")).status_code == 403
        assert (await client.get(f"/api/v1/config-proposals/{result['proposal_id']}/settings-review")).status_code == 403
        assert (await client.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")).status_code == 403
    assert await ctx.app.state.user_store.get_workspace(ctx.workspace_id) == before


async def test_readonly_projection_and_invalid_fields_cannot_create_a_write(metadata_ctx):
    ctx = metadata_ctx
    child = await specialist(ctx)
    for patch in ({"name": " "}, {"name": "x" * 121}, {"description": "x" * 2001}, {"api_key": "secret"}, {"week_start_day": "sunday"}):
        await _call(ctx, child, "configuration_read", form_id="workspace_metadata")
        assert not (await _call(ctx, child, "configuration_propose", form_id="workspace_metadata", patch=patch))["ok"]

    class Readonly:
        async def assert_can_run_turn(self, team_id):
            raise PermissionError("Read-only Team")

    ctx.app.state.entitlements = Readonly()
    result = await _call(ctx, child, "configuration_read", form_id="workspace_metadata")
    assert result["ok"] and result["actions"] == ["read"] and not result["editable_fields"]
    assert not (await _call(ctx, child, "configuration_open", form_id="workspace_metadata"))["ok"]


def test_shared_fixture_preserves_exact_send_identity_and_domain_revision():
    import json
    from pathlib import Path

    from bowhead_core.configuration_settings_target import WorkspaceSettingsSendTarget
    from bowhead_gateway.configuration_workspace_metadata import WorkspaceMetadataDraftDTO

    root = Path(__file__).resolve().parents[2]
    fixture = json.loads((root / "bowhead_frontend/__tests__/fixtures/configuration-space-metadata.json").read_text())
    target = WorkspaceSettingsSendTarget.model_validate(fixture["send"])
    assert fixture["open"]["target"] == {
        "page": "space_general",
        "user_id": target.user_id,
        "team_id": target.team_id,
        "workspace_id": target.workspace_id,
    }
    review = fixture["review"]
    assert WorkspaceMetadataDraftDTO.model_validate({row["field"]: row["to"] for row in review["rows"]}).name == "Finance"
    assert review["rows"][0]["baseline_revision"] == 7
    assert fixture["read"]["revision"] == review["rows"][0]["baseline_revision"]
    assert set(fixture["read"]["fields"]) == {"name", "description"}
    assert fixture["receipt"]["owner_user_id"] == target.user_id
