"""Gateway switches that default to on in code since 2026-10-14 (user decision after 116 enablement).

An unset switch enables the feature; an explicit ``0`` remains the rollback.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from bowhead_gateway.document_service import document_chat_continuation_enabled
from bowhead_gateway.source_access_projection import source_attribution_v2_enabled

pytestmark = pytest.mark.unit

_CASES: list[tuple[str, Callable[[], bool]]] = [
    ("FEATURE_DOCUMENT_CHAT_CONTINUATION", document_chat_continuation_enabled),
    ("FEATURE_SOURCE_ATTRIBUTION_V2", source_attribution_v2_enabled),
]


@pytest.mark.parametrize(("env_name", "is_enabled"), _CASES, ids=[name for name, _ in _CASES])
def test_unset_switch_enables_and_explicit_zero_disables(monkeypatch: pytest.MonkeyPatch, env_name: str, is_enabled: Callable[[], bool]) -> None:
    monkeypatch.delenv(env_name, raising=False)
    assert is_enabled()

    monkeypatch.setenv(env_name, "0")
    assert not is_enabled()
