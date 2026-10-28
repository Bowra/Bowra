"""Exercise the maintenance gate with newly introduced routes and original DTO fields."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from bowhead_core.config_copilot import reset_registry_for_tests
from pydantic import Field, create_model

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def guard(tmp_path):
    spec = importlib.util.spec_from_file_location("configuration_coverage_guard", ROOT / "scripts/check_configuration_coverage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for relative in (
        "config/configuration-coverage.json",
        *[
            str(p.relative_to(ROOT))
            for base in ("packages/bowhead_frontend/app/(app)", "enterprise/frontend/app/(app)")
            for p in (ROOT / base).rglob("page.tsx")
        ],
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    yield module, tmp_path
    reset_registry_for_tests()


def test_new_settings_page_blocks_coverage_until_classified(guard):
    module, root = guard
    assert module.check(root) == []
    route = root / "packages/bowhead_frontend/app/(app)/team/settings/new-feature/page.tsx"
    route.parent.mkdir(parents=True)
    route.write_text("export default function Page() { return null; }\n")
    assert any("Unclassified settings page: /team/settings/new-feature/" in item for item in module.check(root))


def test_original_provider_field_addition_cannot_silently_skip_agent_review(guard, monkeypatch):
    module, root = guard
    assert module.check(root) == []
    expanded = create_model(
        "ExpandedProvider", __base__=module.V2ProviderDraft, regional_endpoint=(str | None, Field(default=None, description="Regional endpoint."))
    )
    monkeypatch.setattr(module, "V2ProviderDraft", expanded)
    assert any("regional_endpoint" in item for item in module.check(root))


def test_new_sandbox_limit_requires_an_explicit_coverage_decision(guard, monkeypatch):
    from dataclasses import fields, make_dataclass

    module, root = guard
    assert module.check(root) == []
    expanded = make_dataclass("ExpandedExecutionLimits", [(item.name, int) for item in fields(module.ExecutionLimits)] + [("max_artifacts", int)])
    monkeypatch.setattr(module, "ExecutionLimits", expanded)
    assert any("sandbox/HTML execution-limit fields" in item for item in module.check(root))
