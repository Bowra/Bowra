"""Presentation contracts share source metadata without granting access or changing writes."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from bowhead_core.config_copilot import get_form, reset_registry_for_tests
from bowhead_core.configuration_agent import _read_schema
from bowhead_core.configuration_contracts import DOMAINS
from bowhead_core.configuration_field_guidance import contract_fields, field_contract, project_schema
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from bowhead_gateway.configuration_field_guidance import page_field_contract
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def registrations():
    reset_registry_for_tests()
    register_gateway_forms(FastAPI())
    yield
    reset_registry_for_tests()


@pytest.mark.parametrize("domain", DOMAINS)
def test_page_and_agent_share_authorized_source_descriptions(domain, monkeypatch):
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    registration = get_form(domain)
    contract = page_field_contract(domain)["field_contract"]
    agent = _read_schema(registration, registration.field_policy, {})
    page_fields = contract_fields(contract)
    agent_fields = contract_fields({"schema": agent})
    assert page_fields.keys() == agent_fields.keys()
    for field_id, node in page_fields.items():
        assert node["description"] == agent_fields[field_id]["description"]
        assert node["x-bowhead-field"] == agent_fields[field_id]["x-bowhead-field"]
    assert "api_key" not in json.dumps(contract)
    assert "fingerprint" not in json.dumps(contract)


def test_projection_removes_unreachable_secret_definitions_and_preserves_recursive_content():
    source = {
        "type": "object",
        "properties": {"safe": {"$ref": "#/$defs/Safe"}, "secret": {"$ref": "#/$defs/Secret"}},
        "$defs": {
            "Safe": {"type": "object", "description": "Safe content.", "properties": {"child": {"$ref": "#/$defs/Safe"}}},
            "Secret": {"type": "string", "default": "credential-sentinel"},
        },
    }
    projected = project_schema(source, {"safe"})
    contract = field_contract("sample", projected)
    assert "credential-sentinel" not in json.dumps(contract)
    assert "Secret" not in contract["schema"].get("$defs", {})
    assert contract["schema"]["properties"]["safe"]["properties"]["child"]["$ref"] == "#/$defs/Safe"
    assert "secret" in source["properties"]


def test_bound_changes_do_not_approve_changed_meaning_or_initialize_values():
    initial = page_field_contract("team_execution_limits")["field_contract"]
    narrowed = page_field_contract("team_execution_limits", {"ranges": {"sandbox_max_timeout_s": {"min": 5, "max": 20}}})["field_contract"]
    field_id = "team_execution_limits.sandbox_max_timeout_s"
    assert contract_fields(narrowed)[field_id]["maximum"] == 20
    assert (
        contract_fields(initial)[field_id]["x-bowhead-field"]["text_revision"]
        == contract_fields(narrowed)[field_id]["x-bowhead-field"]["text_revision"]
    )
    assert initial["contract_revision"] != narrowed["contract_revision"]
    source = get_form("team_model_retry").schema.model_json_schema()
    changed = copy.deepcopy(source)
    changed["properties"]["max_retries"]["description"] = "Changed retry semantics."
    before = contract_fields(field_contract("team_model_retry", source))
    after = contract_fields(field_contract("team_model_retry", changed))
    assert before["team_model_retry.max_retries"]["x-bowhead-field"] != after["team_model_retry.max_retries"]["x-bowhead-field"]


def test_metadata_failure_does_not_fail_original_read():
    assert page_field_contract("unregistered-domain") == {}


@pytest.fixture
def guard(tmp_path):
    spec = importlib.util.spec_from_file_location("field_guidance_guard", ROOT / "scripts/check_configuration_field_guidance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bindings = json.loads((ROOT / module.BINDINGS).read_text())
    paths = {
        module.MANIFEST,
        module.BINDINGS,
        *(module.FRONTEND / f"edition/shared-messages/{locale}.json" for locale in ("en-US", "zh-CN", "de-DE", "fr-FR")),
        *(module.FRONTEND / item["file"] for item in bindings.values() if item["kind"] == "control"),
    }
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / path).read_bytes())
    return module, tmp_path


def test_guard_blocks_unreviewed_nested_fields_and_does_not_auto_approve_translations(guard, monkeypatch):
    module, root = guard
    assert module.check(root) == []
    original = module.field_contract

    def changed(domain, source):
        source = copy.deepcopy(source)
        if domain == "team_model_config":
            source["properties"]["tiers"]["properties"]["new_assignment"] = {"type": "string", "description": "A new assignment."}
        return original(domain, source)

    monkeypatch.setattr(module, "field_contract", changed)
    assert any("new_assignment" in issue for issue in module.check(root))
    before = json.loads((root / module.MANIFEST).read_text())["reviewed"]
    module.write_english(root)
    assert json.loads((root / module.MANIFEST).read_text())["reviewed"] == before
    assert any("Translation review is stale" in issue for issue in module.check(root))


@pytest.mark.parametrize("mutation", ["binding", "translation", "review"])
def test_guard_rejects_removed_binding_or_translation(guard, mutation):
    module, root = guard
    if mutation == "binding":
        path = root / module.BINDINGS
        data = json.loads(path.read_text())
        data["team_model_retry.max_retries"]["marker"] = "missing-control-binding"
    elif mutation == "translation":
        path = root / module.FRONTEND / "edition/shared-messages/zh-CN.json"
        data = json.loads(path.read_text())
        data["configuration_fields"].clear()
    else:
        path = root / module.MANIFEST
        data = json.loads(path.read_text())
        data["reviewed"]["zh-CN"]["team_model_retry.max_retries"] = "old-source"
    path.write_text(json.dumps(data))
    assert module.check(root)


@pytest.mark.parametrize("mutation", ["meaning", "range", "example", "annotation"])
def test_guard_detects_source_mutations(guard, monkeypatch, mutation):
    module, root = guard
    original = module.field_contract

    def changed(domain, source):
        source = copy.deepcopy(source)
        if domain == "team_model_retry":
            node = source["properties"]["max_retries"]
            if mutation == "meaning":
                node["description"] = "Changed retry behavior."
            elif mutation == "range":
                node["maximum"] += 1
            elif mutation == "example":
                node["examples"] = [{"api_key": "synthetic-credential"}]
            else:
                node["x-bowhead-field"]["evaluate"] = "unreviewed-expression"
        return original(domain, source)

    monkeypatch.setattr(module, "field_contract", changed)
    errors = module.check(root)
    assert errors, mutation
    assert any("retry" in error or "max_retries" in error or "annotation" in error or "example" in error for error in errors)
