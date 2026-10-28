"""V2ModelDeclaration 必须能携带 api_surface（Responses 批 0）。

extra="forbid" 之前把这个键整个挡死 ⇒ 翻面模型的验证草稿带不动它、证据在新签名下
永久 stale（tech-design §8.1）。合法值收、非法值 422、未设不落库
（model_dump(exclude_none=True)）。
"""

from __future__ import annotations

import pytest
from bowhead_gateway.routes.admin_llm_config_v2 import V2ModelDeclaration
from pydantic import ValidationError


@pytest.mark.unit
def test_api_surface_accepts_responses_and_dumps_it() -> None:
    declaration = V2ModelDeclaration(id="gpt-51", capabilities=["chat"], api_surface="responses")
    dumped = declaration.model_dump(exclude_none=True)
    assert dumped["api_surface"] == "responses"


@pytest.mark.unit
def test_api_surface_absent_stays_absent() -> None:
    declaration = V2ModelDeclaration(id="gpt-51", capabilities=["chat"])
    assert "api_surface" not in declaration.model_dump(exclude_none=True)


@pytest.mark.unit
def test_api_surface_invalid_value_rejected() -> None:
    with pytest.raises(ValidationError):
        V2ModelDeclaration(id="gpt-51", capabilities=["chat"], api_surface="response")


@pytest.mark.unit
def test_api_surface_rejected_on_non_openai_compatible_provider_draft() -> None:
    """review R1-P2-8：provider 级校验——azure 草稿带 responses 模型必须 422。"""
    from bowhead_gateway.routes.admin_llm_config_v2 import V2ProviderDraft

    with pytest.raises(ValidationError, match="only supported for openai_compatible"):
        V2ProviderDraft(
            runtime_key="az",
            type="azure_openai",
            endpoint="https://example.com",
            models=[V2ModelDeclaration(id="gpt-51", capabilities=["chat"], api_surface="responses")],
        )
    # Positive control: openai_compatible accepts it.
    draft = V2ProviderDraft(
        runtime_key="oc",
        type="openai_compatible",
        base_url="https://example.com/v1",
        models=[V2ModelDeclaration(id="gpt-51", capabilities=["chat"], api_surface="responses")],
    )
    assert draft.models[0].api_surface == "responses"
