"""D3b — Lead prompt only cites real web_search URLs and forbids invented authorities.

The old directive '数字必带 source URL' demanded provenance the OpenAI-compat path can't
supply, so the LLM invented an authority ('据国家统计局') to satisfy it. The rule now ties
citations to what web_search actually returned and bans fabricated authorities. (Efficacy is
LLM-driven → real-LLM dogfood; this just asserts the directive is present.)
"""

from __future__ import annotations

from bowhead_core.agents.prompts.lead import build_lead_system_prompt


def test_lead_prompt_bans_invented_authority() -> None:
    p = build_lead_system_prompt("ws_test", facts=[])
    assert "never invent an authoritative source" in p
    assert "only the URLs / titles web_search actually returned" in p
