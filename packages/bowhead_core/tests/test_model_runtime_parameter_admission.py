"""Default-on admission retains explicit closure and saved sending policies."""

from __future__ import annotations

import copy

import pytest
from bowhead_core.team_llm_flags import model_runtime_parameters_enabled, runtime_parameter_changes_admitted

pytestmark = pytest.mark.unit


def test_admission_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("FEATURE_MODEL_RUNTIME_PARAMETERS", raising=False)
    assert model_runtime_parameters_enabled()
    config = {"providers": {"service": {"models": [{"id": "model", "runtime_parameters": {"defaults": {"thinking_mode": "off"}}}]}}}
    assert runtime_parameter_changes_admitted({}, config)


def test_closed_admission_keeps_existing_policy_and_allows_explicit_removal(monkeypatch):
    monkeypatch.setenv("FEATURE_MODEL_RUNTIME_PARAMETERS", "0")
    assert not model_runtime_parameters_enabled()
    old = {"providers": {"service": {"models": [{"id": "model", "runtime_parameters": {"defaults": {"thinking_mode": "off"}}}]}}}
    assert runtime_parameter_changes_admitted(old, copy.deepcopy(old))
    changed = copy.deepcopy(old)
    changed["providers"]["service"]["models"][0]["runtime_parameters"]["defaults"]["thinking_mode"] = "on"
    assert not runtime_parameter_changes_admitted(old, changed)
    del changed["providers"]["service"]["models"][0]["runtime_parameters"]
    assert runtime_parameter_changes_admitted(old, changed)
    assert not runtime_parameter_changes_admitted({}, old)
    assert runtime_parameter_changes_admitted({"providers": [{"provider_name": "legacy"}]}, {"providers": {"legacy": {"models": ["model"]}}})
    monkeypatch.setenv("FEATURE_MODEL_RUNTIME_PARAMETERS", "1")
    assert runtime_parameter_changes_admitted({}, old)
