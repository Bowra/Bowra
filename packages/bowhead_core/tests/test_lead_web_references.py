"""Web formatting guidance must reach both supported Lead prompt compositions."""

from __future__ import annotations

import pytest
from bowhead_core.agents.prompts.lead import build_lead_system_prompt


@pytest.mark.unit
@pytest.mark.parametrize("prefix_order", ["0", "1"])
def test_web_reference_guidance_keeps_literal_urls_and_registered_citations(monkeypatch: pytest.MonkeyPatch, prefix_order: str) -> None:
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", prefix_order)
    prompt = build_lead_system_prompt("ws_test", facts=[])
    assert prompt.count("<web_reference_rules>") == 1
    rules = prompt.split("<web_reference_rules>", 1)[1].split("</web_reference_rules>", 1)[0]
    assert "supported by retrieved content" in rules
    assert "If no supported label is known, use the URL" in rules
    assert "inline code or a fenced code block" in rules
    assert "turn's answer language" in rules
    assert "do not invent citation IDs for web links" in rules
    assert "<data_reference_rules>" in prompt
