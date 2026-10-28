"""Space instructions through the Configuration Agent: the page's access rule, exact text and versioned saves."""

import json
from types import SimpleNamespace

import aiosqlite
import httpx2
import pytest
from bowhead_core.config_copilot import reset_registry_for_tests
from bowhead_core.config_copilot_chat_tools import set_proposal_writer_provider
from bowhead_core.config_draft_store import ConfigDraftStore
from bowhead_core.configuration_agent import prepare_configuration_agent
from bowhead_core.events import TurnEventBus
from bowhead_core.space_instructions import SpaceInstructionsStore
from bowhead_core.tool_registry import ToolExecutionContext
from bowhead_core.user_store import UserStore
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from bowhead_gateway.routes import config_proposals
from fastapi import FastAPI
from test_configuration_agent_personal_settings import _call

pytestmark = pytest.mark.unit

FORM = "space_instructions"


@pytest.fixture
async def instructions_ctx(tmp_path, monkeypatch, initialize_migrated_core_database):
    database = tmp_path / "instructions.db"
    initialize_migrated_core_database(database)
    store = UserStore(database)
    await store.initialize()
    team = await store.create_team(name="Instructions Team", max_seats=10)
    admin = await store.create_user(team_id=team.id, email="admin@example.com", password="Passw0rd!")
    member = await store.create_user(team_id=team.id, email="member@example.com", password="Passw0rd!")
    manager = await store.create_user(team_id=team.id, email="manager@example.com", password="Passw0rd!")
    outsider = await store.create_user(team_id=team.id, email="outsider@example.com", password="Passw0rd!")
    async with aiosqlite.connect(store.db_path) as conn:
        await conn.execute("UPDATE team_members SET role = 'team_admin' WHERE team_id = ? AND user_id = ?", (team.id, manager.id))
        await conn.commit()
    workspace_id = await store.create_local_workspace(team_id=team.id, name="Pharma", creator_user_id=admin.id)
    await store.add_member(workspace_id=workspace_id, user_id=member.id, role="member")
    app = FastAPI()
    app.state.user_store = store
    app.state.space_instructions_store = SpaceInstructionsStore(database)
    app.state.settings = SimpleNamespace(auth_mode="dev", dev_default_user=admin.id)
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    reset_registry_for_tests()
    register_gateway_forms(app)
    app.state.config_draft_store = ConfigDraftStore(store.db_path)
    await app.state.config_draft_store.initialize()
    app.include_router(config_proposals.router)
    set_proposal_writer_provider(lambda: app.state.config_draft_store)
    users = {"admin": admin, "member": member, "manager": manager, "outsider": outsider}
    yield SimpleNamespace(app=app, users=users, users_db=store.db_path, workspace_id=workspace_id, instructions=app.state.space_instructions_store)
    set_proposal_writer_provider(None)
    reset_registry_for_tests()


async def specialist(ctx, who="admin"):
    user = ctx.users[who]
    lead = ToolExecutionContext(
        user_id=user.id,
        team_id=user.team_id,
        member_uid=user.id,
        workspace_id=ctx.workspace_id,
        session_id="s_instructions",
        turn_id="t_instructions",
        role="lead",
        datarc=None,
        config_lite_supported=True,
        user_text="Add to this Space's instructions: new products launched within 24 months.",
        parent_bus=TurnEventBus(),
    )
    child, _ = await prepare_configuration_agent(lead, form_id=FORM)
    return child


async def proposal(ctx, content, who="admin"):
    child = await specialist(ctx, who)
    read = await _call(ctx, child, "configuration_read", form_id=FORM)
    assert read["ok"] and read["editable_fields"] == ["content"], read
    result = await _call(ctx, child, "configuration_propose", form_id=FORM, patch={"content": content})
    assert result["ok"], result
    return child, result


def client(ctx, who="admin"):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=ctx.app), base_url="http://gw", headers={"x-bowhead-user-id": ctx.users[who].id})


async def test_admin_suggestion_is_confirmed_by_the_user_into_a_new_version(instructions_ctx):
    ctx = instructions_ctx
    await ctx.instructions.save(
        workspace_id=ctx.workspace_id, content="Answer for brand managers.", created_by=ctx.users["admin"].id, expected_version=0
    )
    child = await specialist(ctx)
    read = await _call(ctx, child, "configuration_read", form_id=FORM)
    assert read["fields"]["content"]["value"] == "Answer for brand managers."
    assert read["revision"] == 1 and read["field_constraints"] == {"content": {"maxLength": 6000}}
    assert read["actions"] == ["read", "propose", "confirm"]
    assert read["target"]["workspace_id"] == ctx.workspace_id and read["target"]["workspace_name"] == "Pharma"
    text = "Answer for brand managers.\n\nNew products launched within 24 months."
    result = await _call(ctx, child, "configuration_propose", form_id=FORM, patch={"content": text})
    assert result["ok"] and result["rows"][0]["baseline_revision"] == 1, result
    assert (await ctx.instructions.current(ctx.workspace_id)).version == 1
    refusal = await _call(ctx, child, "configuration_apply", form_id=FORM, proposal_id=result["proposal_id"])
    assert refusal["error"] == "user_confirmation_required"
    async with client(ctx) as http:
        saved = await http.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")
        assert saved.status_code == 200, saved.text
        current = await ctx.instructions.current(ctx.workspace_id)
        assert (current.version, current.content, current.created_by) == (2, text, ctx.users["admin"].id)
        async with aiosqlite.connect(ctx.users_db) as conn:
            rows = await (await conn.execute("SELECT payload_json FROM admin_audit_log WHERE target_id = 'space_instructions'")).fetchall()
        # The audit names the versions; the texts stay in the version history instead of being copied.
        assert [json.loads(row[0])["before"] for row in rows] == ["version 1"]
        assert [json.loads(row[0])["after"] for row in rows] == ["version 2"]
        assert (await http.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")).json()["replayed"] is True
        assert (await ctx.instructions.current(ctx.workspace_id)).version == 2
        assert (await http.post(f"/api/v1/config-proposals/{result['proposal_id']}/undo")).status_code == 409


async def test_long_text_reaches_the_specialist_exactly_and_placeholders_are_refused(instructions_ctx):
    ctx = instructions_ctx
    # Longer than the generic 2000-character read cap and containing a phrase the generic read would escape.
    original = "Act as the brand team's analyst.\n" + "\n".join(f"Rule {index}: keep the {index}th definition." for index in range(120))
    assert len(original) > 2000
    await ctx.instructions.save(workspace_id=ctx.workspace_id, content=original, created_by=ctx.users["admin"].id, expected_version=0)
    child = await specialist(ctx)
    read = await _call(ctx, child, "configuration_read", form_id=FORM)
    assert read["fields"]["content"]["value"] == original
    echoed = await _call(
        ctx, child, "configuration_propose", form_id=FORM, patch={"content": original + "\n…（内容过长已截断，完整内容请引导用户到设置页查看）"}
    )
    assert echoed["error"] == "invalid_patch"
    too_long = await _call(ctx, child, "configuration_propose", form_id=FORM, patch={"content": "x" * 6001})
    assert too_long["error"] == "content_too_long"
    kept = await _call(ctx, child, "configuration_propose", form_id=FORM, patch={"content": original + "\nOne more rule."})
    assert kept["ok"] and kept["rows"][0]["from"] == original, kept


async def test_page_access_rule_applies_to_members_team_managers_and_outsiders(instructions_ctx):
    ctx = instructions_ctx
    member = await specialist(ctx, "member")
    read = await _call(ctx, member, "configuration_read", form_id=FORM)
    assert read["ok"] and read["editable_fields"] == [] and read["actions"] == ["read"]
    assert not (await _call(ctx, member, "configuration_propose", form_id=FORM, patch={"content": "Mine"}))["ok"]
    outsider = await specialist(ctx, "outsider")
    denied = await _call(ctx, outsider, "configuration_read", form_id=FORM)
    assert not denied["ok"] and "fields" not in denied
    # A Team manager who is not in the Space manages its instructions, as on the page.
    _, result = await proposal(ctx, "Set by the Team manager.", who="manager")
    async with client(ctx, "manager") as http:
        assert (await http.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")).status_code == 200
    assert (await ctx.instructions.current(ctx.workspace_id)).content == "Set by the Team manager."


async def test_newer_manual_version_blocks_confirmation_and_same_text_is_applied_without_a_write(instructions_ctx):
    ctx = instructions_ctx
    admin = ctx.users["admin"].id
    _, stale = await proposal(ctx, "Suggested text.")
    await ctx.instructions.save(workspace_id=ctx.workspace_id, content="Manual text.", created_by=admin, expected_version=0)
    async with client(ctx) as http:
        failed = await http.post(f"/api/v1/config-proposals/{stale['proposal_id']}/apply")
        assert failed.status_code == 409
        assert failed.json()["detail"]["error"]["code"] == "configuration_baseline_changed"
        assert (await ctx.instructions.current(ctx.workspace_id)).content == "Manual text."
        _, same = await proposal(ctx, "Agreed text.")
        await ctx.instructions.save(workspace_id=ctx.workspace_id, content="Agreed text.", created_by=admin, expected_version=1)
        applied = await http.post(f"/api/v1/config-proposals/{same['proposal_id']}/apply")
        assert applied.status_code == 200 and applied.json()["status"] == "applied", applied.text
    assert (await ctx.instructions.current(ctx.workspace_id)).version == 2


async def test_losing_space_admin_rights_after_the_suggestion_blocks_confirmation(instructions_ctx):
    ctx = instructions_ctx
    _, result = await proposal(ctx, "Suggested text.")
    await ctx.app.state.user_store.add_member(workspace_id=ctx.workspace_id, user_id=ctx.users["admin"].id, role="member")
    async with client(ctx) as http:
        assert (await http.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")).status_code == 403
    assert await ctx.instructions.current(ctx.workspace_id) is None


async def test_archived_space_is_reported_and_an_applied_card_keeps_its_status(instructions_ctx):
    ctx = instructions_ctx
    child, result = await proposal(ctx, "Suggested text.")
    async with client(ctx) as http:
        assert (await http.post(f"/api/v1/config-proposals/{result['proposal_id']}/apply")).status_code == 200
        async with aiosqlite.connect(ctx.app.state.user_store.db_path) as conn:
            await conn.execute("UPDATE workspaces SET status = 'archived' WHERE id = ?", (ctx.workspace_id,))
            await conn.commit()
        # Reloading the chat still shows the real status instead of a card that offers Apply again.
        status = await http.get(f"/api/v1/config-proposals/{result['proposal_id']}")
        assert status.status_code == 200 and status.json()["status"] == "applied", status.text
    read = await _call(ctx, await specialist(ctx), "configuration_read", form_id=FORM)
    assert read["ok"] and read["space_archived"] is True and read["editable_fields"] == [] and read["actions"] == ["read"]


async def test_lead_receipts_carry_text_lengths_not_the_instructions(instructions_ctx):
    from bowhead_core.configuration_agent import configuration_agent_result

    ctx = instructions_ctx
    await ctx.instructions.save(
        workspace_id=ctx.workspace_id, content="Answer for brand managers.", created_by=ctx.users["admin"].id, expected_version=0
    )
    child, result = await proposal(ctx, "Answer for brand managers.\n\nNew rule.")
    assert result["rows"][0]["to"] == "Answer for brand managers.\n\nNew rule."
    receipts = child._configuration_agent_binding.receipts
    assert receipts[0]["fields"]["content"]["value"] == "<26 characters>"
    assert (receipts[1]["rows"][0]["from"], receipts[1]["rows"][0]["to"]) == ("<26 characters>", "<37 characters>")
    assert "brand managers" not in str(configuration_agent_result(child))
