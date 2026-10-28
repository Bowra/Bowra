"""builtin_plugin_ids in draft, Coach and chat-editor definitions (Capability Center tech design §6)."""

from __future__ import annotations

import pytest
from bowhead_gateway.builtin_plugin_fields import expand_selected_builtin_plugins, has_resource_selection
from bowhead_gateway.routes.team_subagent_drafts import DraftDefinitionBody, ManualDefinitionBody
from pydantic import ValidationError

_MANUAL = {
    "name": "store-weekly",
    "display_name": "Store weekly",
    "description": "Weekly store report",
    "system_prompt": "Report weekly store results.",
    "tool_refs": [],
    "max_iterations": 10,
}


def test_absent_field_is_omitted_so_stored_definitions_keep_their_hashes():
    assert "builtin_plugin_ids" not in ManualDefinitionBody.model_validate(_MANUAL).model_dump()
    draft = {key: value for key, value in _MANUAL.items() if key != "tool_refs"}
    assert "builtin_plugin_ids" not in DraftDefinitionBody.model_validate(draft).model_dump()


def test_empty_selection_is_treated_as_absent(monkeypatch):
    # An empty list neither changes the stored definition nor trips the switch-off check.
    assert "builtin_plugin_ids" not in ManualDefinitionBody.model_validate({**_MANUAL, "builtin_plugin_ids": []}).model_dump()


def test_proposal_documents_refuse_invalid_builtin_selections(monkeypatch):
    from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, _validate_builtin_plugin_selection

    _validate_builtin_plugin_selection({"builtin_plugin_ids": []})
    _validate_builtin_plugin_selection({"builtin_plugin_ids": ["builtin:designs"]})
    with pytest.raises(AgentChangeProposalError, match="builtin_plugin_not_selectable"):
        _validate_builtin_plugin_selection({"builtin_plugin_ids": ["builtin:email"]})


def test_valid_selection_is_kept():
    manual = ManualDefinitionBody.model_validate({**_MANUAL, "builtin_plugin_ids": ["builtin:data-analysis"]})
    assert manual.model_dump()["builtin_plugin_ids"] == ["builtin:data-analysis"]
    assert has_resource_selection(manual)
    assert has_resource_selection({"builtin_plugin_ids": ["builtin:designs"]})
    assert not has_resource_selection({"tool_refs": ["web_search"]})


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (["designs"], "builtin_plugin_ids_invalid"),
        (["builtin:nope"], "builtin_plugin_unknown"),
        (["builtin:email"], "builtin_plugin_not_selectable"),
    ],
)
def test_invalid_selection_uses_the_core_codes(value, code):
    with pytest.raises(ValidationError, match=code):
        ManualDefinitionBody.model_validate({**_MANUAL, "builtin_plugin_ids": value})


def test_expansion_matches_publication():
    expanded = expand_selected_builtin_plugins({"tool_refs": ["web_search"], "builtin_plugin_ids": ["builtin:designs"]})
    assert expanded["resolved_builtin_plugins"] == [{"name": "designs", "version": 1}]
    assert "canvas_create_design" in expanded["tool_refs"]
