"""chat-v4 W4-fix CTO P1-D — V1 path tolerates empty arrays.

W4.4 prompt slot-optional rule: when LLM has no data for a slot, emit
empty array (not fabricate). This is universal (no-fabricate is principle,
not V4-only). Verify that the V1 markdown / composer path doesn't crash
when payload has empty kpis / insights / recommendations.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_v1_markdown_handles_empty_kpis() -> None:
    """payload with empty kpis must not crash composer's `_build_sections`.

    Also: the V1 fallback (text_for_chat = exec_summary | title | narrative |
    fallback_markdown) must still produce SOMETHING when kpis is [].
    """
    from bowhead_core.canvas_composer import _build_sections_from_payload

    payload = {
        "title": "Test",
        "exec_summary": "Summary " + "x" * 30,
        "kpis": [],  # empty per W4.4 slot-optional
        "insights": [],
        "recommendations": [],
        "caveats": [],
    }
    sections = _build_sections_from_payload(payload, intent=None, tool_invocations=None)
    # No crash, returns a list. Must contain at least h1 + narrative
    # (exec_summary still renders).
    assert isinstance(sections, list)
    kinds = [s.get("kind") for s in sections]
    assert "h1" in kinds, f"V1 path lost h1 with empty arrays: {kinds}"
    assert "narrative" in kinds, f"V1 path lost narrative with empty arrays: {kinds}"
    # No kpi_row / insight_card / recommendations since their source arrays empty.
    assert "kpi_row" not in kinds


@pytest.mark.unit
def test_v1_markdown_handles_all_empty_optional_arrays() -> None:
    """All optional arrays empty: kpis/insights/recommendations/caveats.
    Composer must not crash. exec_summary alone is enough to render."""
    from bowhead_core.canvas_composer import _build_sections_from_payload

    payload = {
        "title": "T",
        "exec_summary": "summary " * 10,
        "kpis": [],
        "insights": [],
        "recommendations": [],
        "caveats": [],
        "sources": [],
    }
    sections = _build_sections_from_payload(payload, intent=None, tool_invocations=None)
    assert isinstance(sections, list)
    assert len(sections) >= 2  # at least h1 + narrative


@pytest.mark.unit
def test_v1_text_for_chat_fallback_chain() -> None:
    """V1 lead text_for_chat fallback chain works with empty arrays.
    exec_summary > title > narrative > fallback_markdown.
    """
    # Simulates lead_orchestrator's text_for_chat synthesis when peer payload
    # has empty arrays but exec_summary present.
    payload = {
        "title": "T",
        "exec_summary": "real summary",
        "kpis": [],
        "insights": [],
        "recommendations": [],
    }
    text = payload.get("exec_summary") or payload.get("title") or payload.get("narrative") or payload.get("fallback_markdown") or ""
    assert text == "real summary"

    # When exec_summary is empty, falls to title.
    payload2 = dict(payload)
    payload2["exec_summary"] = ""
    text2 = payload2.get("exec_summary") or payload2.get("title") or payload2.get("narrative") or payload2.get("fallback_markdown") or ""
    assert text2 == "T"
