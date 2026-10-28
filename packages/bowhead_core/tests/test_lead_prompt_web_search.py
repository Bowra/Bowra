"""chat-v4 W1 — Lead prompt web_search unblock UT.

Per docs/chat-v4-prd.md §6 invariant I-W1-1 + tech-design CHANGE-W1.1:
- 知识边界段 must NOT contain "NEVER 调用 web search" directive
- Must contain positive routing guidance for external / industry / competitor queries

See: packages/bowhead_core/src/bowhead_core/agents/prompts/lead.py
"""

from __future__ import annotations


def _build_default_prompt() -> str:
    """Build Lead system prompt with default flags (skill catalog gracefully degrades)."""
    from bowhead_core.agents.prompts.lead import build_lead_system_prompt

    return build_lead_system_prompt(
        "ws_test",
        facts=[],
    )


def test_knowledge_boundary_no_longer_says_never_for_web_search() -> None:
    """I-W1-1: reverse the 'NEVER 调用 web search' directive."""
    from bowhead_core.agents.prompts import lead as lead_mod

    # WP M3-12: renders by fetch assignee; constant renamed `_KNOWLEDGE_BOUNDARY_TMPL`.
    # Test rendered output through the builder (historical assignee preserves byte-for-byte previous behavior).
    boundary = lead_mod.build_knowledge_boundary("compute")
    assert "NEVER 调用 web" not in boundary, "Lead prompt 知识边界段 still bans web_search — chat-v4 W1 must unblock"
    assert "NEVER" not in boundary, f"Lead prompt 知识边界段 still contains NEVER directive: {boundary!r}"


def test_knowledge_boundary_contains_positive_web_search_guidance() -> None:
    """I-W1-1: positive routing guidance for when to use web_search."""
    from bowhead_core.agents.prompts import lead as lead_mod

    # WP M3-12: renders by fetch assignee; constant renamed `_KNOWLEDGE_BOUNDARY_TMPL`.
    # Test rendered output through the builder (historical assignee preserves byte-for-byte previous behavior).
    boundary = lead_mod.build_knowledge_boundary("compute")
    assert "web_search" in boundary, "Lead prompt must mention web_search positively"
    # external/industry/competitor signals
    assert all(kw in boundary for kw in ("external", "industry", "competitor")), (
        "Lead prompt must specify when to use web_search (external/industry/competitor)"
    )
    # internal BI/KB routing exclusion
    assert "bi_query" in boundary or "kb_search" in boundary, "Lead prompt must specify NOT to use web_search for internal BI/KB"


def test_full_prompt_contains_web_search_routing() -> None:
    """End-to-end: rendered prompt has both positive web_search mention and BI/KB exclusion."""
    prompt = _build_default_prompt()
    assert "web_search" in prompt
    assert "NEVER 调用 web" not in prompt


def test_prompt_keeps_web_routing_and_tail_without_truncation() -> None:
    """上下文工程批 1-b：不再用硬 cap 丢段，末尾协议必须完整保留。"""
    prompt = _build_default_prompt()
    assert "web_search" in prompt
    assert "</query_classification_decision_tree>" in prompt
    assert prompt.endswith("</lead_orchestrator>")
