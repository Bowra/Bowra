from __future__ import annotations

import pytest
from bowhead_gateway.results_capabilities import resolve_results_capabilities


@pytest.mark.unit
def test_complete_list_and_scale_nav_are_standard_when_cursor_is_ready() -> None:
    capabilities = resolve_results_capabilities(cursor_ready=True)

    assert capabilities.workspace is True
    assert capabilities.complete_list is True
    assert capabilities.scale_nav is True


@pytest.mark.unit
def test_cursor_readiness_is_a_hard_subcapability_dependency() -> None:
    capabilities = resolve_results_capabilities(cursor_ready=False)

    assert capabilities.workspace is True
    assert capabilities.complete_list is False
    assert capabilities.scale_nav is False
