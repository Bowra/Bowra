"""Export routing rules must reach default and explicit Lead peer rosters."""

from __future__ import annotations

import pytest
from bowhead_core.agents.prompts.lead import build_lead_system_prompt
from bowhead_core.agents.prompts.peer_block import default_prompt_peers

# b594341e0 renamed the rule to "ordinary" exports, separate from template-draft PDFs.
_PDF_XLSX_RULE = "ordinary PDF / Excel (xlsx)"

# canvas-writer was removed in c4d50b03b and is absent from the default roster.
_SYSTEM_PEERS = ("compute", "consultant", "kb-analyst")


def _static_prompt() -> str:
    """peers=None → 从 system manifests 派生默认花名册。"""
    return build_lead_system_prompt("ws_test", facts=[])


def _dynamic_prompt(*, with_image_peer: bool = False) -> str:
    """peers 非空 → 走 `render_peer_block`（**prod 默认路径**）。"""
    names = [*_SYSTEM_PEERS] + (["marketing-analyst"] if with_image_peer else [])
    defaults = {peer.name: peer for peer in default_prompt_peers()}
    peers = [defaults[name] for name in names]
    return build_lead_system_prompt(
        "ws_test",
        facts=[],
        peers=peers,
    )


@pytest.mark.parametrize(
    ("label", "builder"),
    [
        ("static_fallback", _static_prompt),
        ("dynamic_live", _dynamic_prompt),
        ("dynamic_live_with_image_peer", lambda: _dynamic_prompt(with_image_peer=True)),
    ],
)
def test_pdf_xlsx_routing_rule_present_in_every_path(label: str, builder) -> None:
    """PDF/Excel 派活规则必须出现在**每一条**会真被渲染的路径里。

    `dynamic_live*` 是 prod 实际用的那条；只改静态兜底的话它们会红。
    """
    prompt = builder()
    assert _PDF_XLSX_RULE in prompt, f"{label} 缺少 PDF/Excel 派活规则（prod 会不派活）"


@pytest.mark.parametrize(
    ("label", "builder"),
    [
        ("static_fallback", _static_prompt),
        ("dynamic_live", _dynamic_prompt),
    ],
)
def test_pptx_still_routes_to_consultant(label: str, builder) -> None:
    """回归：新增 PDF/Excel 规则不能把既有的 PPT→consultant 规则挤掉。

    consultant + render_deck.py 是今天**唯一**能产出可下载文件的路径
    （23 次「要文件」里那 4 次成功全走它），弄丢它比没加 PDF 规则更糟。
    """
    prompt = builder()
    assert "new PPT slide deck" in prompt  # b594341e0: English, new-deck-only wording
    assert "consultant" in prompt


@pytest.mark.parametrize(
    ("label", "builder"),
    [
        ("static_fallback", _static_prompt),
        ("dynamic_live", _dynamic_prompt),
        ("dynamic_live_with_image_peer", lambda: _dynamic_prompt(with_image_peer=True)),
    ],
)
def test_prompt_keeps_tail_sections_without_truncation(label: str, builder) -> None:
    """上下文工程批 1-b 删除硬截断后，长 prompt 也必须保留末尾决策树。"""
    prompt = builder()
    assert prompt.endswith("</lead_orchestrator>"), f"{label}: Lead prompt 尾部被截断"
    assert "</query_classification_decision_tree>" in prompt, f"{label}: 末尾决策树缺失"
