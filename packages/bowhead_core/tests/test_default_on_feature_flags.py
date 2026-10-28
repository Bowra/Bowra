"""Switches that default to on in code since 2026-10-14 (user decision after 116 enablement).

An unset switch enables the feature; an explicit ``0`` remains the rollback.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from bowhead_core.canvas_rank import is_seq_authoritative
from bowhead_core.canvas_revise import section_revise_enabled
from bowhead_core.effective_language import language_decision_v2_enabled
from bowhead_core.memory_auto_capture import auto_capture_enabled
from bowhead_core.space_governance import space_admin_v2_enabled
from bowhead_core.team_openapi_flags import team_openapi_enabled

pytestmark = pytest.mark.unit

_CASES: list[tuple[str, Callable[[], bool]]] = [
    ("FEATURE_REPORT_EDIT_SYNC_V2", is_seq_authoritative),
    ("FEATURE_CANVAS_SECTION_REVISE", section_revise_enabled),
    ("FEATURE_LANGUAGE_DECISION_V2", language_decision_v2_enabled),
    ("FEATURE_MEMORY_AUTO_CAPTURE", auto_capture_enabled),
    ("FEATURE_SPACE_ADMIN_V2", space_admin_v2_enabled),
    ("FEATURE_TEAM_OPENAPI_SAAS", team_openapi_enabled),
]


@pytest.mark.parametrize(("env_name", "is_enabled"), _CASES, ids=[name for name, _ in _CASES])
def test_unset_switch_enables_and_explicit_zero_disables(monkeypatch: pytest.MonkeyPatch, env_name: str, is_enabled: Callable[[], bool]) -> None:
    # Team OpenAPI is unconditional in `local`; the switch only governs `saas`.
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "saas")
    monkeypatch.delenv(env_name, raising=False)
    assert is_enabled()

    monkeypatch.setenv(env_name, "0")
    assert not is_enabled()
