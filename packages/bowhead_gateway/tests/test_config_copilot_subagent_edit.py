"""Collection edit: visibility is checked before matching, then owner permission."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.config_copilot import FormContext, get_form, reset_registry_for_tests
from bowhead_core.entitlements import EntitlementDenied
from fastapi import FastAPI

from bowhead_gateway import config_copilot_forms as forms


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch):
    hidden = SimpleNamespace(id="osa_hidden", name="private-helper", display_name="私人助手", visibility="creator_only", created_by="u_other")
    visible = SimpleNamespace(id="osa_visible", name="sales-helper", display_name="销售助手", visibility="creator_only", created_by="u_me")
    shared = SimpleNamespace(id="osa_shared", name="shared-helper", display_name="共享助手", visibility="team_wide", created_by="u_other")

    class Store:
        db_path = "unused"

        async def get_user(self, _user_id):
            return SimpleNamespace(id="u_me", team_id="t_1")

        async def list_team_subagent_summaries(self, _team_id):
            return [hidden, visible, shared]

        async def get_team_subagent(self, _team_id, _subagent_id):
            return SimpleNamespace(
                id="osa_visible",
                display_name="销售助手",
                description="分析销售",
                system_prompt="按月分析",
                tool_refs=["bi_query"],
                max_iterations=12,
                skills=[],
                output_schema=None,
            )

    class AccessResolver:
        def __init__(self, _db_path):
            pass

        async def resolve_many(self, **_kwargs):
            return {
                "osa_visible": SimpleNamespace(resolution=SimpleNamespace(can_manage=True)),
                "osa_shared": SimpleNamespace(resolution=SimpleNamespace(can_manage=False)),
            }

    async def no_grants(*_args, **_kwargs):
        return frozenset()

    monkeypatch.setattr(forms, "TeamSubagentAccessResolver", AccessResolver)
    monkeypatch.setattr(forms, "grants_for_viewer", no_grants)
    monkeypatch.setattr(forms, "TeamSubagentStore", lambda _path: SimpleNamespace(release_definition_state=no_release))
    ctx = FormContext(user_id="u_me", user_email="", team_id="t_1", locale="zh-CN", current_values={})
    ctx.deps.update(user_store=Store(), access_control_store=object())
    return ctx


async def no_release(**_kwargs):
    return None


@pytest.mark.anyio
async def test_visibility_and_owner_ladder(catalog: FormContext) -> None:
    listing = await forms._subagent_read(catalog)
    assert {item["name"] for item in listing["items"]} == {"sales-helper", "shared-helper"}
    assert (await forms._subagent_resolve(catalog, "私人助手")).matched is None
    own = await forms._subagent_resolve(catalog, "销售助手")
    assert own.matched is not None and own.matched.id == "osa_visible"
    other = await forms._subagent_resolve(catalog, "共享助手")
    assert other.not_writable_reason == "not_owner"
    assert (await forms._subagent_resolve(catalog, "销售")).matched.id == "osa_visible"


@pytest.mark.anyio
async def test_active_target_is_rechecked(catalog: FormContext) -> None:
    catalog.target = SimpleNamespace(id="osa_visible", label="销售助手")
    assert (await forms._subagent_target_read(catalog))["system_prompt"] == "按月分析"
    catalog.target = SimpleNamespace(id="osa_hidden", label="私人助手")
    with pytest.raises(RuntimeError, match="not visible"):
        await forms._subagent_target_read(catalog)


@pytest.mark.anyio
async def test_revisioned_target_reads_working_copy_not_legacy_columns(
    catalog: FormContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def revisioned(**_kwargs):
        return ("revisioned", None)

    class ReadStore:
        def __init__(self, _path):
            pass

        async def working_copy(self, **_kwargs):
            return {
                "working_copy": {
                    "definition": {
                        "system_prompt": "新版指令",
                        "tool_refs": ["bi_query"],
                        "max_iterations": 15,
                        "skill_grants": [],
                        "output_schema": None,
                    },
                    "listing": {"display_name": "新版助手", "description": "新版说明"},
                },
                "live": {},
            }

    monkeypatch.setattr(forms, "TeamSubagentStore", lambda _path: SimpleNamespace(release_definition_state=revisioned))
    monkeypatch.setattr(forms, "AgentReleaseReadStore", ReadStore)
    catalog.target = SimpleNamespace(id="osa_visible", label="销售助手")
    fields = await forms._subagent_target_read(catalog)
    assert fields["system_prompt"] == "新版指令"
    assert fields["description"] == "新版说明"


@pytest.mark.anyio
async def test_write_capability_blocks_readonly_and_unlicensed_mcp(catalog: FormContext) -> None:
    class Frozen:
        async def assert_can_run_turn(self, _team_id):
            raise EntitlementDenied("suspended", 403, "frozen")

    catalog.deps.update(entitlements=Frozen(), deployment_mode="local")
    assert (await forms._subagent_resolve(catalog, "销售助手")).not_writable_reason == "suspended"

    class NoMcp:
        async def assert_can_run_turn(self, _team_id):
            return None

        async def is_feature_licensed(self, _team_id, _feature):
            return False

    items = await catalog.deps["user_store"].list_team_subagent_summaries("t_1")
    items[1].team_tool_count = 1
    catalog.deps["entitlements"] = NoMcp()
    assert (await forms._subagent_resolve(catalog, "销售助手")).not_writable_reason == "feature_unlicensed"


def test_form_registration_is_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        reset_registry_for_tests()
        monkeypatch.delenv("FEATURE_CONFIG_EDIT", raising=False)
        forms.register_gateway_forms(FastAPI())
        assert get_form("team_subagent") is None
        monkeypatch.setenv("FEATURE_CONFIG_EDIT", "1")
        forms.register_gateway_forms(FastAPI())
        registered = get_form("team_subagent")
        assert registered is not None and registered.chat_draft_supported
        monkeypatch.setenv("FEATURE_CONFIG_EDIT", "0")
        forms.register_gateway_forms(FastAPI())
        assert get_form("team_subagent") is None
    finally:
        reset_registry_for_tests()
