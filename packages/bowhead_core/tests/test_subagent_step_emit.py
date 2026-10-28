"""Sprint chat-redesign-2026-05-21 Phase 1.1 — subagent.py dual-emit smoke tests.

Verifies the helper functions + dataclass shapes WITHOUT booting a full
subagent runner (that'd require a router + tool registry mock matrix).
Full integration is covered in subagent_test downstream once dual-emit
window closes (Phase 1.5).
"""

from __future__ import annotations


def test_tool_header_zh_mapped_tools_have_chinese_label() -> None:
    from bowhead_core.subagent import _tool_header_zh

    assert _tool_header_zh("bi_query") == "查询数据"
    assert _tool_header_zh("python_sandbox") == "运行代码"
    assert _tool_header_zh("kb_search") == "搜索知识库"
    assert _tool_header_zh("subagent_spawn") == "派出智能体"


def test_tool_header_zh_never_leaks_the_tool_code_to_the_user() -> None:
    """步骤卡中文抬头不许出现工具代号。

    ⚠ 这张表**压过前端目录**（`humanLabel()` 中文界面下优先用 header_zh），
    所以前端那几张展示表改得再干净，中文用户看到的仍是这里的字。
    capability-surface-audit 2026-09-06 之前它写的是「查询数据 (bi_query)」
    「查 schema (bi_schema)」「跑代码 (Python)」—— 括号里就是代号本身。

    突变验证：把 `_TOOL_HEADER_ZH` 改回不并入生成表 → 本测试红。
    """
    from bowhead_core.agents._generated.tool_display import TOOL_DISPLAY
    from bowhead_core.subagent import _tool_header_zh

    assert len(TOOL_DISPLAY) > 30, "生成表为空的话本测试是空转"
    leaked = []
    for name in sorted(TOOL_DISPLAY):
        header = _tool_header_zh(name)
        if header == name or name in header or "(" in header:
            leaked.append((name, header))
    assert leaked == [], f"这些抬头里漏了工具代号: {leaked}"

    # Retired tools' short names must still replay (persisted in historical traces).
    assert _tool_header_zh("fetch_rows") == "拉取明细"
    assert _tool_header_zh("get_cards") == "取图表列表"


def test_tool_header_zh_unmapped_falls_back_to_raw_name() -> None:
    from bowhead_core.subagent import _tool_header_zh

    # Unknown tool: header == raw name (debuggable, no hidden error)
    assert _tool_header_zh("invent_new_tool_xyz") == "invent_new_tool_xyz"
    assert _tool_header_zh("") == ""


def test_tool_header_zh_team_tool_hides_opaque_source_id() -> None:
    """团队工具的 wire 名不该整串糊到用户脸上。

    116 E2E（2026-07-21）实测步骤条显示 `teammcp.ots_70fde3bcbae4.get_repo_info`
    ——`ots_…` 对用户是纯噪音。团队工具是运行时创建的，永远不可能进静态映射表，
    所以按前缀兜底：只留有意义的 operation 那一截。
    """
    from bowhead_core.subagent import _tool_header_zh

    assert _tool_header_zh("teammcp.ots_70fde3bcbae4.get_repo_info") == "团队工具 · get_repo_info"
    # Malformed/truncated names must fall back unchanged, never render an empty team-tool label.
    assert _tool_header_zh("teammcp.ots_x.") == "teammcp.ots_x."
    assert _tool_header_zh("teammcp.") == "teammcp."


def test_tool_header_zh_dict_covers_canonical_tools() -> None:
    """Regression: every tool name in _TOOL_HEADER_ZH should have a non-empty
    zh-CN header. Catches an accidental empty-string default."""
    from bowhead_core.subagent import _TOOL_HEADER_ZH

    assert len(_TOOL_HEADER_ZH) >= 20  # ~25 canonical entries
    for k, v in _TOOL_HEADER_ZH.items():
        assert isinstance(k, str) and k
        assert isinstance(v, str) and v


def test_execute_tool_signature_accepts_concurrency_group_kwargs() -> None:
    """Phase 1.1 added concurrency_group_id + parent_step_id kwargs to
    `_execute_tool`. Signature check guards against accidental removal."""
    import inspect

    from bowhead_core.subagent import SubagentRunner

    sig = inspect.signature(SubagentRunner._execute_tool)
    params = sig.parameters
    assert "concurrency_group_id" in params
    assert "parent_step_id" in params
    # Both default to None (kwarg-only, with default)
    assert params["concurrency_group_id"].default is None
    assert params["parent_step_id"].default is None
    assert params["concurrency_group_id"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["parent_step_id"].kind == inspect.Parameter.KEYWORD_ONLY
