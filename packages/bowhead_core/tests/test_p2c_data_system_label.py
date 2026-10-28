"""P2c — _data_system_label families derive from the source primitive (lookup_domain).

dataset/kb/web/external mirror lookup_domain (the SSOT); team/MCP tools now count as 外部.
'compute' stays finer here: only python_sandbox is a 计算 data system; canvas_/skill_ (also
source='compute') are rendering tools, not data systems.
"""

from __future__ import annotations

from bowhead_core.report_writer_loop import _data_system_label


def test_families_match_source() -> None:
    assert _data_system_label("bi_query") == "BI"
    assert _data_system_label("bi_schema") == "BI"
    assert _data_system_label("kb_search") == "知识库"
    assert _data_system_label("web_search") == "网络"
    assert _data_system_label("web_fetch") == "网络"


def test_team_mcp_tools_now_counted_external() -> None:  # the new behaviour
    assert _data_system_label("teammcp.acme.get_orders") == "外部"
    assert _data_system_label("mcp.server.tool") == "外部"


def test_python_sandbox_is_compute_data_system() -> None:
    assert _data_system_label("python_sandbox") == "计算"


def test_canvas_and_skill_not_data_systems() -> None:
    # source='compute' but rendering/util tools → unlabelled (precision preserved vs the enum)
    assert _data_system_label("add_chart") is None
    assert _data_system_label("skill_invoke") is None


def test_empty_and_non_data_tools_none() -> None:
    assert _data_system_label("") is None
    assert _data_system_label("subagent_spawn") is None
