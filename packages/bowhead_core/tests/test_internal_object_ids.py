"""Internal canvas/section/artifact IDs never reach user-visible text.

116 t_7edbc155f8d8 showed `sec_3bf299ae70f0` in the final answer and the Results report
because only bare `art_` IDs were removed. Removal happens where text is produced, so the
live, stored and replayed copies are the same text. Functional markers keep their IDs.
"""

from __future__ import annotations

import re

import pytest
from bowhead_core.agents.lead_orchestrator import _resolve_citations, _strip_data_markers
from bowhead_core.analysis_payload import (
    exposes_internal_identifiers,
    remove_id_matches,
    strip_internal_ids_from_payload,
    strip_internal_object_ids,
)
from bowhead_core.subagent_return_tool import CTX_PAYLOAD_ATTR, register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

pytestmark = pytest.mark.unit

SUMMARY = (
    "绿色柱状图已画好并挂上画布（图表节 sec_3bf299ae70f0）：1-2000 共 20 个百位区间。"
    "数据直接复用已有 artifact art_85365dc88fa4（prime_counts.csv，20 行）。"
)


def test_final_answer_drops_section_ids_with_artifact_ids() -> None:
    expected = "绿色柱状图已画好并挂上画布（图表节）：1-2000 共 20 个百位区间。数据直接复用已有（prime_counts.csv，20 行）。"
    assert _strip_data_markers(SUMMARY) == expected
    assert _resolve_citations(SUMMARY, None) == expected
    assert _resolve_citations(expected, None) == expected, "Idempotent"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Saved to canvas cv_f5a1cf141d7c.", "Saved to."),
        ("The chart (section sec_3bf299ae70f0) is green.", "The chart is green."),
        ("图表节 `sec_3bf299ae70f0` 已更新。", "图表节已更新。"),
        ("修订 rv_0a1b2c3d4e5f 已保存", "修订已保存"),
    ],
)
def test_object_ids_and_their_labels_are_removed(text: str, expected: str) -> None:
    assert strip_internal_object_ids(text) == expected


def test_functional_markers_urls_and_code_keep_their_ids() -> None:
    text = "见 [figure:sec_3bf299ae70f0]，[data:art_85365dc88fa4]，/api/v1/files/art_1301403add43。\n```\nsection_id = 'sec_3bf299ae70f0'\n```"
    assert strip_internal_object_ids(text, artifacts=True) == text


def test_payload_prose_loses_ids_but_reference_fields_keep_them() -> None:
    payload = {
        "exec_summary": SUMMARY,
        "caveats": ["改用图片节上画布（art_1301403add43）。"],
        "insights": [
            {
                "title": "分布",
                "narrative": "见图表节 sec_3bf299ae70f0。",
                "evidence": [{"kind": "chart", "chart_section_id": "sec_3bf299ae70f0", "caption": "图 sec_3bf299ae70f0"}],
            }
        ],
        "sources": [{"label": "prime_counts.csv（复用上一轮 artifact art_85365dc88fa4）", "tool": "python_sandbox"}],
    }
    assert strip_internal_ids_from_payload(payload) == 5
    assert "sec_" not in payload["exec_summary"] and "art_" not in payload["exec_summary"]
    assert payload["caveats"] == ["改用图片节上画布。"]
    assert payload["insights"][0]["narrative"] == "见图表节。"
    assert payload["insights"][0]["evidence"][0] == {"kind": "chart", "chart_section_id": "sec_3bf299ae70f0", "caption": "图"}
    assert payload["sources"][0]["label"] == "prime_counts.csv（复用上一轮）"


def test_internal_identifier_check_uses_the_turns_own_names() -> None:
    names = ["add_chart", "recall", "chart_edit_incomplete"]
    assert exposes_internal_identifiers("用 add_chart 新建失败。", names)
    assert exposes_internal_identifiers("返回 chart_edit_incomplete。", names)
    assert exposes_internal_identifiers("图表节 sec_3bf299ae70f0", [])
    assert not exposes_internal_identifiers("I recall the green chart from prime_counts.csv.", names), "Word-like names never match prose"


async def test_returned_payload_is_stored_without_internal_ids() -> None:
    registry = ToolRegistry()
    register_subagent_return_tool(registry)
    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="m_test", user_id="u_test", turn_id="t_test", datarc=None, session_id="s_test")  # type: ignore[arg-type]
    invocation = await registry.execute("subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": SUMMARY}}, ctx)
    assert invocation.error is None and invocation.result["ok"] is True
    stored = getattr(ctx, CTX_PAYLOAD_ATTR)
    assert "sec_" not in stored["exec_summary"] and "art_" not in stored["exec_summary"]
    assert stored["exec_summary"].startswith("绿色柱状图已画好并挂上画布（图表节）")


# 116 the regression case: the version answer reached the user as "最新是第 5 版，约 25 小时前生成）".
BRACKETED_ID_PROSE = [
    ("最新是第5版（art_a1b2c3d4e5f6，约25小时前生成）。", "最新是第5版（约25小时前生成）。"),
    ("**第1版**（art_043ac670d536，41小时前）—首版。", "**第1版**（41小时前）—首版。"),
    ("**第3版**（art_45d5109e96be）—插入品牌章节。", "**第3版**—插入品牌章节。"),
    ("第2版（约40小时前，art_45d5109e96be）已删除。", "第2版（约40小时前）已删除。"),
    ("两版（art_45d5109e96be、art_6a7a0e58e3d0）都在。", "两版都在。"),
    ("两版（art_45d5109e96be、art_6a7a0e58e3d0，均为 HTML）都在。", "两版（均为 HTML）都在。"),
    ("The latest (art_a1b2c3d4e5f6, 25 h ago) is version 5.", "The latest (25 h ago) is version 5."),
]


@pytest.mark.parametrize(("text", "expected"), BRACKETED_ID_PROSE)
def test_final_answer_keeps_a_bracket_that_holds_more_than_the_id(text: str, expected: str) -> None:
    assert _strip_data_markers(text) == expected
    assert _resolve_citations(text, None) == expected
    assert _resolve_citations(expected, None) == expected, "Idempotent"


@pytest.mark.parametrize(("text", "expected"), BRACKETED_ID_PROSE)
def test_payload_prose_keeps_a_bracket_that_holds_more_than_the_id(text: str, expected: str) -> None:
    assert strip_internal_object_ids(text, artifacts=True) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("（sec_3bf299ae70f0，第二章）已更新。", "（第二章）已更新。"), ("（第二章：sec_3bf299ae70f0）已更新。", "（第二章）已更新。")],
)
def test_object_id_separators_go_with_the_id(text: str, expected: str) -> None:
    assert strip_internal_object_ids(text) == expected


def test_a_line_break_next_to_a_removed_id_stays() -> None:
    assert _strip_data_markers("第一行\n(art_a1b2c3d4e5f6)\n第三行") == "第一行\n\n第三行"


def test_remove_id_matches_leaves_text_without_matches_unchanged() -> None:
    pattern = re.compile(r"(?P<lead>[ \t]*)(?P<open>[(])?x_[0-9]+(?:(?P<close>[)]))?")
    assert remove_id_matches("no ids here", pattern) == "no ids here"
    assert remove_id_matches("see (x_12, note)", pattern) == "see (note)"
