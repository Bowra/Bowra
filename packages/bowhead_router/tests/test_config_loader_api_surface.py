"""routing.yaml 第三条写入路的 api_surface boot 校验（review R4-P1）——fail-fast，
不许拼写错静默跑 chat 面。"""

from __future__ import annotations

import pytest
from bowhead_router.config_loader import _validated_models


@pytest.mark.unit
def test_valid_declaration_passes_and_legacy_shapes_untouched() -> None:
    spec = {"type": "openai_compatible", "models": [{"id": "m1", "api_surface": "responses"}, {"id": "m2"}, "legacy-string-model"]}
    assert _validated_models("p", spec) == spec["models"]


@pytest.mark.unit
def test_invalid_value_fails_at_boot() -> None:
    spec = {"type": "openai_compatible", "models": [{"id": "m1", "api_surface": "response"}]}
    with pytest.raises(ValueError, match="api_surface invalid"):
        _validated_models("p", spec)


@pytest.mark.unit
def test_responses_on_non_openai_compatible_fails_at_boot() -> None:
    spec = {"type": "azure_openai", "models": [{"id": "m1", "api_surface": "responses"}]}
    with pytest.raises(ValueError, match="only supported for openai_compatible"):
        _validated_models("p", spec)
