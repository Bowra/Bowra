"""P2a — _infer_dispatch_role Step-2 reads inv.source instead of the dead dot-prefix.

The legacy startswith("bi.")/"kb."/"canvas." never matched post-omicron underscore names, so
self-served (no-peer) turns always chipped lead_self_answer. Now map the stamped source →
role. Only self-served BI/KB turns change (re-chip to their real role); canvas/compute/web/
model stay lead_self_answer (the dead dot-code never surfaced those either). Step-1 (peer
spawn) is unchanged. Gated by BOWHEAD_DISPATCH_ROLE_SOURCE.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bowhead_core.agents.lead_orchestrator import _infer_dispatch_role


def _res(*invs: Any) -> SimpleNamespace:
    return SimpleNamespace(tool_invocations=list(invs))


def _inv(
    name: str,
    source: str | None = None,
    profile: str | None = None,
    role_id: str | None = None,
) -> SimpleNamespace:
    arguments = {}
    if profile:
        arguments["profile"] = profile
    if role_id:
        arguments["role_id"] = role_id
    return SimpleNamespace(
        tool_name=name,
        source=source,
        arguments=arguments,
        nested=[],
    )


def test_spawn_compute_is_data_analyst() -> None:  # Step-1 unchanged
    assert _infer_dispatch_role(_res(_inv("subagent_spawn", profile="compute"))) == "data_analyst"


def test_spawn_kb_analyst_unchanged() -> None:
    assert _infer_dispatch_role(_res(_inv("subagent_spawn", profile="kb-analyst"))) == "kb_analyst"


def test_behavior_image_dispatch_role_does_not_depend_on_profile_name(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "1")
    assert (
        _infer_dispatch_role(
            _res(
                _inv(
                    "subagent_spawn",
                    profile="random-visual-agent",
                    role_id="image",
                ),
            ),
        )
        == "data_analyst"
    )


def test_self_served_bi_now_data_analyst() -> None:  # the fix
    assert _infer_dispatch_role(_res(_inv("bi_query", source="dataset"))) == "data_analyst"


def test_self_served_kb_now_kb_analyst() -> None:
    assert _infer_dispatch_role(_res(_inv("kb_search", source="kb"))) == "kb_analyst"


def test_self_served_web_stays_lead_self_answer() -> None:
    assert _infer_dispatch_role(_res(_inv("web_search", source="web"))) == "lead_self_answer"


def test_self_served_canvas_stays_lead_self_answer() -> None:
    assert _infer_dispatch_role(_res(_inv("add_chart", source="compute"))) == "lead_self_answer"


def test_zero_tool_is_lead_self_answer() -> None:
    assert _infer_dispatch_role(_res()) == "lead_self_answer"


def test_source_floor_when_unstamped() -> None:
    # source=None → lookup_domain by name (bi_ → dataset)
    assert _infer_dispatch_role(_res(_inv("bi_query", source=None))) == "data_analyst"


def test_mixed_bi_and_kb_prefers_data_analyst() -> None:
    out = _infer_dispatch_role(_res(_inv("bi_query", source="dataset"), _inv("kb_search", source="kb")))
    assert out == "data_analyst"  # priority data_analyst > kb_analyst


def test_flag_off_reverts_to_legacy_dead_path(monkeypatch: Any) -> None:
    monkeypatch.setenv("BOWHEAD_DISPATCH_ROLE_SOURCE", "0")
    # legacy dead dot-prefix never matched → self-served bi_query falls to lead_self_answer
    assert _infer_dispatch_role(_res(_inv("bi_query", source="dataset"))) == "lead_self_answer"
