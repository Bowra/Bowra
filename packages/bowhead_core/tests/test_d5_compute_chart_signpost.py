"""D5 — compute prompt signposts output→chart; Lead peer-block says don't pre-fetch.

These are the prompt-reinforcement half of D5 (the structural half is
_output_artifacts_from_result_json in bowhead_skill). Routing/charting efficacy itself is
LLM-driven and validated by real-LLM dogfood, not UT; these just assert the directives are
present in the assembled prompts.
"""

from __future__ import annotations

from bowhead_core.agents.prompts.peer_block import PeerEntry, render_peer_block
from bowhead_core.subagent import _build_compute_system_prompt


def test_compute_prompt_has_output_chart_signpost() -> None:
    """批 2 后 fallback 与 live 同源，这条同时守住两边的 signpost。

    措辞按 live compute.md 的实况断言：sandbox 产物带真 art id（`output_artifacts`）+
    「出数据不出图，画图走 add_chart」这条指路。"""
    p = _build_compute_system_prompt()
    assert "output_artifacts" in p, "compute peer must be told sandbox outputs carry real art_ ids"
    assert "add_chart" in p
    assert "savefig" in p, "必须仍在教「别 savefig 出 PNG，出数据喂 add_chart」"


def test_peer_block_has_no_prefetch_rule() -> None:
    """D5 的「别先自己取数再派活」规则仍在，但**措辞随交权转正改了**。

    2026-08-21 Lead 交出取数权转正后它已经没有 `bi_query`，原文案「别先自己 bi_query
    取数」反过来在暗示它能——所以规则改写成「先决策再派活」（数据一律由 compute 取，
    bi_schema 判断完立刻 spawn）。**意图没丢，是搬家不是删除**，故本用例改断言新措辞
    而不是删掉。"""
    block = render_peer_block(
        [PeerEntry("compute", "主分析 agent. 跑 bi.* + python.", "system", tool_names=frozenset({"bi_query"}), primary_capability="cap_data_query")]
    )
    assert "Decide first, then dispatch" in block, "Lead must still be told to decide-then-dispatch instead of fetching data itself"
    assert "all data is fetched by compute" in block
    assert "先决策再取数" not in block, "旧措辞预设 Lead 有 bi_query，转正后不该复活"
