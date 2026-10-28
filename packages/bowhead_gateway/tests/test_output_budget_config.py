"""Output metadata survives authorized V2 drafts without persisting defaults."""

import pytest
from bowhead_gateway.routes.admin_llm_config_v2 import V2ModelDeclaration, V2ProviderDraft, context_window_catalog
from bowhead_router.model_capabilities import output_budget_defaults
from pydantic import ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "32000"])
def test_model_and_provider_reject_invalid_capacity(value):
    with pytest.raises(ValidationError):
        V2ModelDeclaration(id="deployment", capabilities=["chat"], max_output_tokens=value)
    with pytest.raises(ValidationError):
        V2ProviderDraft(runtime_key="example", type="openai_compatible", max_output_tokens=value)


def test_roundtrip_preserves_provider_default_model_override_and_absence():
    original = {
        "runtime_key": "example",
        "type": "openai_compatible",
        "max_output_tokens": 64000,
        "models": [{"id": "deployment", "capabilities": ["chat"], "max_output_tokens": 128000}, {"id": "automatic", "capabilities": ["tool"]}],
    }
    provider = V2ProviderDraft.model_validate(original).provider_config()
    assert provider["max_output_tokens"] == 64000
    assert provider["models"][0]["max_output_tokens"] == 128000
    assert "max_output_tokens" not in provider["models"][1]
    assert "output_budget_defaults" not in provider


@pytest.mark.asyncio
async def test_catalog_defaults_are_the_runtime_constants_and_contain_no_team_configuration():
    catalog = await context_window_catalog(user=object())
    assert catalog["output_budget_defaults"] == output_budget_defaults()
    assert catalog["output_budget_defaults"]["automatic_ceiling"] == 256000
    assert catalog["output_budget_defaults"]["unknown_capacity"] == 32000
