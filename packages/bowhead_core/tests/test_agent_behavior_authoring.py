from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from bowhead_core.agent_behavior_authoring import (
    AGENT_AUTHORING_CATALOG_SCHEMA_VERSION,
    AgentBehaviorAuthoringError,
    AgentBehaviorAuthoringService,
)
from bowhead_core.agent_capability_catalog import CapabilityCatalog


def _catalog_with_reviewed_analysis(tmp_path: Path) -> CapabilityCatalog:
    current = CapabilityCatalog.bootstrap()
    definition = current.get_definition("cap_analysis", 1).model_dump(mode="json")
    implementation = current.get_implementation(
        "capi_analysis_reasoning",
        1,
    ).model_dump(mode="json")
    implementation.update(
        {
            "eval_suite": "analysis-contract-v1",
            "lead_role_ids": ["compute"],
            "review_state": "reviewed",
        },
    )
    (tmp_path / "catalog.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "definitions": [definition],
                "implementations": [implementation],
            },
        ),
        encoding="utf-8",
    )
    return CapabilityCatalog.bootstrap(tmp_path)


def test_default_snapshot_is_localized_exact_and_honest_about_review_state() -> None:
    snapshot = AgentBehaviorAuthoringService().snapshot()

    assert snapshot.schema_version == AGENT_AUTHORING_CATALOG_SCHEMA_VERSION == 2
    assert snapshot.model_policy.to_public_dict() == {
        "allowed_roles": ["analyst"],
        "effective_role": "analyst",
        "locked": True,
        "policy_version": "team-agent-model-policy-v1",
    }
    assert snapshot.primary_capabilities
    assert len({item.capability_id for item in snapshot.primary_capabilities}) == len(
        snapshot.primary_capabilities,
    )
    assert all(item.display_name and item.display_name_en for item in snapshot.primary_capabilities)
    assert all("@" in item.exact_ref for item in snapshot.primary_capabilities)
    selectable = {item.exact_ref for item in snapshot.primary_capabilities if item.selectable}
    assert selectable == {"cap_visualize@1"}
    assert {item.verification_state for item in snapshot.primary_capabilities} <= {"blocked", "shadow", "reviewed"}


def test_working_copy_keeps_unproven_primary_as_incomplete_owner_intent() -> None:
    service = AgentBehaviorAuthoringService()

    result = service.validate_definition_intent(
        {"model": "analyst", "primary_capability_ref": "cap_analysis@1"},
    )

    assert result.status == "incomplete"
    assert result.primary_capability_ref == "cap_analysis@1"
    assert "primary_capability_unproven" in result.reason_codes
    assert result.working_copy_validation()["status"] == "incomplete"


@pytest.mark.parametrize(
    ("definition", "code"),
    [
        ({"model": "lead"}, "agent_model_role_not_allowed"),
        (
            {"primary_capability_ref": "cap_analysis"},
            "primary_capability_unknown",
        ),
        (
            {"primary_capability_ref": "cap_missing@1"},
            "primary_capability_unknown",
        ),
        (
            {"behavior_fingerprint": "sha256:" + "0" * 64},
            "agent_behavior_derived_fields_forbidden",
        ),
        (
            {"effective_model_role": "lead"},
            "agent_behavior_derived_fields_forbidden",
        ),
    ],
)
def test_authoring_rejects_policy_bypass_and_derived_fields(
    definition: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(AgentBehaviorAuthoringError, match=code) as error:
        AgentBehaviorAuthoringService().validate_definition_intent(definition)

    assert error.value.code == code


def test_reviewed_implementation_with_role_becomes_selectable(tmp_path: Path) -> None:
    service = AgentBehaviorAuthoringService(_catalog_with_reviewed_analysis(tmp_path))

    option = service.snapshot().primary_capabilities[0]
    result = service.validate_definition_intent(
        {"primary_capability_ref": "cap_analysis@1"},
    )

    assert option.exact_ref == "cap_analysis@1"
    assert option.selectable is True
    assert option.verification_state == "reviewed"
    assert option.eligible_role_ids == ("compute",)
    assert result.status == "catalog_eligible"
