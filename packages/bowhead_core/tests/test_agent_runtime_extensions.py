"""Published optional methods stay separate from baseline execution grants."""

import copy

import pytest
from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensionError, read_agent_runtime_extensions
from bowhead_core.chat_references import SkillResourceRef

pytestmark = pytest.mark.unit
REVISION = "sha256:" + "a" * 64


def policy(*, kind="builtin", version=None, enabled=True):
    selector = {"source_kind": kind, "id": "dupont"}
    if version is not None:
        selector["version"] = version
    return {
        "runtime_extensions": {
            "schema_version": 1,
            "skill_read": {
                "enabled": enabled,
                "selection_sources": ["user_explicit"],
                "allowed_sources": [kind],
                "resource_allowlist": [selector],
                "require_read_only_contract": True,
                "max_selected": 3,
            },
        }
    }


def resource(**changes):
    return SkillResourceRef.model_validate({"kind": "skill", "source_kind": "builtin", "id": "dupont", "version": REVISION, **changes})


def test_explicit_read_selection_is_pinned_or_limited_to_resource_identity():
    exact = read_agent_runtime_extensions(policy(version=REVISION))
    exact.require_selection([resource()])
    for changed in (resource(id="another"), resource(source_kind="team"), resource(version="sha256:" + "b" * 64)):
        with pytest.raises(AgentRuntimeExtensionError, match="extension_denied"):
            exact.require_selection([changed])
    unpinned = read_agent_runtime_extensions(policy())
    unpinned.require_selection([resource(version="sha256:" + "b" * 64)])
    assert exact.digest != unpinned.digest


def test_missing_disabled_and_empty_selection_never_enable_extension():
    assert read_agent_runtime_extensions({}) is None
    assert read_agent_runtime_extensions({"runtime_extensions": None}) is None
    with pytest.raises(AgentRuntimeExtensionError, match="extension_denied"):
        read_agent_runtime_extensions(policy(enabled=False)).require_selection([resource()])
    with pytest.raises(AgentRuntimeExtensionError, match="extension_denied"):
        read_agent_runtime_extensions(policy()).require_selection([])


def test_semantic_policy_hash_ignores_selector_order_and_detaches_source():
    first = policy()
    rule = first["runtime_extensions"]["skill_read"]
    rule["allowed_sources"].append("team")
    rule["resource_allowlist"].append({"source_kind": "team", "id": "tsk_method", "version": REVISION})
    original = read_agent_runtime_extensions(first)
    second = copy.deepcopy(first)
    second["runtime_extensions"]["skill_read"]["allowed_sources"].reverse()
    second["runtime_extensions"]["skill_read"]["resource_allowlist"].reverse()
    assert read_agent_runtime_extensions(second).digest == original.digest
    rule["resource_allowlist"][0]["id"] = "other"
    original.require_selection([resource()])
    assert read_agent_runtime_extensions(first).digest != original.digest


@pytest.mark.parametrize(
    "change",
    [
        "version_bool",
        "version_future",
        "enabled_integer",
        "readonly_false",
        "readonly_integer",
        "model_selection",
        "wildcard",
        "duplicate",
        "undeclared_source",
        "empty_enabled",
        "budget_bool",
        "budget_large",
        "extra",
        "too_many",
        "floating",
    ],
)
def test_malformed_policy_is_rejected_without_legacy_fallback(change):
    value = policy()
    raw = value["runtime_extensions"]
    rule = raw["skill_read"]
    if change == "version_bool":
        raw["schema_version"] = True
    elif change == "version_future":
        raw["schema_version"] = 2
    elif change == "enabled_integer":
        rule["enabled"] = 1
    elif change == "readonly_false":
        rule["require_read_only_contract"] = False
    elif change == "readonly_integer":
        rule["require_read_only_contract"] = 1
    elif change == "model_selection":
        rule["selection_sources"] = ["agent_auto"]
    elif change == "wildcard":
        rule["resource_allowlist"][0]["id"] = "*"
    elif change == "duplicate":
        rule["resource_allowlist"].append(dict(rule["resource_allowlist"][0]))
    elif change == "undeclared_source":
        rule["allowed_sources"] = ["team"]
    elif change == "empty_enabled":
        rule["resource_allowlist"] = []
    elif change == "budget_bool":
        rule["max_selected"] = True
    elif change == "budget_large":
        rule["max_selected"] = 4
    elif change == "extra":
        rule["invoke"] = True
    elif change == "too_many":
        rule["resource_allowlist"] = [{"source_kind": "builtin", "id": f"method{i}"} for i in range(33)]
    else:
        rule["resource_allowlist"][0]["version"] = "latest"
    with pytest.raises(AgentRuntimeExtensionError, match="agent_runtime_extensions_invalid"):
        read_agent_runtime_extensions(value)


@pytest.mark.parametrize("audience", ["custom", "teamwide"])
def test_personal_selectors_cannot_enter_a_shared_definition(audience):
    configured = read_agent_runtime_extensions(policy(kind="user"))
    configured.require_private_audience("private")
    with pytest.raises(AgentRuntimeExtensionError, match="private_source"):
        configured.require_private_audience(audience)
