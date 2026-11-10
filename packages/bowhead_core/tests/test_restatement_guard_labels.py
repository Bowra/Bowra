"""The chat fabrication guard compares values, not identifiers, and never swaps in worse text.

Texts are the real 116 t_7edbc155f8d8 helper summary and Lead reply (session s_975a366b55fe).
The guard took 299 from `sec_3bf299ae70f0` and the ends of range labels as authoritative
values, flagged Lead's correct 303 and 17, and replaced the reply with a summary that showed
Tool names, a result code and internal IDs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.agents.lead_orchestrator import _compose_turn_output
from bowhead_core.analysis_payload import narrative_restates_payload_incorrectly, payload_authoritative_numbers

pytestmark = pytest.mark.unit

HELPER_SUMMARY = (
    "绿色柱状图已画好并挂上画布（图表节 sec_3bf299ae70f0）：1-2000 共 20 个百位区间，竖向柱状图、柱体为绿色系、每根柱顶标注质数个数。\n\n"
    "实现方式：add_chart 新建原生图表节连续三次返回 chart_edit_incomplete（无法指定绿色），因此改用 python 的 matplotlib 生成绿色柱状图 PNG"
    "（art_1301403add43），再以 add_image 作为图片节上画布。数据直接复用已有 artifact art_85365dc88fa4（prime_counts.csv，20 行：百位区间 / 质数个数），"
    "未重新计算质数、未做任何 BI 查询。\n\n"
    "各区间质数个数：1-100 为 25 个，101-200 为 21 个，201-300 为 16 个，此后逐区间递减，到 1901-2000 为 16 个。 [cite:1]"
)
LEAD_QUESTION = (
    "抱歉，这次改色没有成功。\n\n数据本身没有丢：分区间质数个数仍在 `prime_counts.csv`中（20行，1-100到1901-2000），1–2000共303个质数。"
    "这轮没有新增或改动任何图表节。\n\n接下来有两条可行路径，请您选一条：\n\n1. **重画一张绿色柱状图**\n2. **保留现有图**\n\n"
    "您回复「重画一张绿色柱状图」，我就按方案1执行；数据会直接复用，不会重新计算质数。"
)
LEAD_REPLY = (
    "绿色柱状图已经画好，并已放到画布上。\n\n这次采用图片形式：原生图表组件不支持指定柱体颜色，因此改用同一份 `prime_counts.csv`数据"
    "（20行，百位区间与质数个数两列）重新绘制了一张绿色柱状图，横轴为20个百位区间，纵轴为质数个数，每根柱顶标出具体数值。"
    "数据没有重算，1–2000之间仍是303个质数。\n\n分布规律和之前一致：1-100区间最多（25个），随区间增大整体变稀疏，1301-1400最少（11个），"
    "但百位尺度上有涨落，例如1401-1500回升到17个。\n\n画布上原来那张原生图表节仍保留着，配色未变。如果您希望只留绿色这一张，告诉我，我会把旧图节删掉。"
)
_COUNTS = [25, 21, 16, 16, 17, 14, 16, 14, 15, 14, 16, 12, 15, 11, 17, 12, 15, 12, 12, 13]
_TABLE = "百位区间 质数个数 共 303 个\n" + "\n".join(f"{i * 100 + 1}-{(i + 1) * 100} {n}" for i, n in enumerate(_COUNTS))


def _runtime(narrative: str, tool_names: tuple[str, ...] = ()) -> SimpleNamespace:
    ctx = SimpleNamespace(
        _lead_narrative_text=narrative,
        canvas_store=None,
        _last_nested_invocations=[],
        member_uid="",
        language_context=None,
        artifact_store=None,
        response_kind_pre_flight=None,
    )
    registry = SimpleNamespace(list_names=lambda: list(tool_names))
    return SimpleNamespace(parent_ctx=ctx, parent_bus=SimpleNamespace(), router=None, session_store=None, tool_registry=registry)


def _result(final_answer: str) -> SimpleNamespace:
    failed_edit = SimpleNamespace(result={"error": "chart_edit_incomplete", "hint": "The chart edit did not complete."})
    sandbox = SimpleNamespace(result={"ok": True, "stdout": _TABLE})
    spawn = SimpleNamespace(tool_name="subagent_spawn", result={"ok": True}, nested=[failed_edit, sandbox], source=None, arguments={})
    return SimpleNamespace(tool_invocations=[spawn], intent=None, final_answer=final_answer, duration_ms=1, tokens_input=1, tokens_output=1)


async def _compose(narrative: str, final_answer: str, exec_summary: str) -> tuple[str, SimpleNamespace]:
    runtime = _runtime(narrative, tool_names=("add_chart", "add_image", "subagent_spawn"))
    sctx = SimpleNamespace(turn_id="t_guard", session_id="s_guard", workspace_id="w", user_id="u", prior_turn_status=None)
    _kinds, text = await _compose_turn_output(
        runtime=runtime,  # type: ignore[arg-type]
        sctx=sctx,  # type: ignore[arg-type]
        result=_result(final_answer),
        aggregated_payload={"exec_summary": exec_summary},
        user_text="柱状图请用绿色",
        peer_payloads=[],
    )
    return text, runtime.parent_ctx


def test_identifier_digits_and_range_labels_are_not_authoritative_values() -> None:
    assert payload_authoritative_numbers({"exec_summary": HELPER_SUMMARY}) == {16.0, 20.0, 21.0, 25.0}


def test_correct_reply_beside_identifiers_and_ranges_is_not_drift() -> None:
    payload = {"exec_summary": HELPER_SUMMARY}
    assert narrative_restates_payload_incorrectly(LEAD_REPLY, payload) is False
    assert narrative_restates_payload_incorrectly(LEAD_QUESTION + LEAD_REPLY, payload) is False


def test_range_labels_in_the_reply_are_not_stated_values() -> None:
    payload = {"exec_summary": "门店共 1,350 家，平均面积 1,420 平方米。"}
    assert narrative_restates_payload_incorrectly("面积分布：1301-1400 平方米的门店最多，1401-1500 平方米次之。", payload) is False


def test_drift_is_still_caught_when_the_summary_contains_identifiers() -> None:
    payload = {"exec_summary": "斜率 62.937 箱/月（图表节 sec_3bf299ae70f0，2026-01-01 起），R² = 0.770，预测 3165.758 箱。"}
    assert narrative_restates_payload_incorrectly("斜率 63.885 箱/月，R² = 0.767，预测 3,131 箱。", payload) is True


async def test_guard_keeps_the_correct_reply_and_its_narrative() -> None:
    text, ctx = await _compose(LEAD_QUESTION + LEAD_REPLY, LEAD_REPLY, HELPER_SUMMARY)
    assert text == LEAD_REPLY
    assert getattr(ctx, "_lead_narrative_superseded", False) is False


async def test_guard_never_swaps_in_a_summary_that_shows_internal_names() -> None:
    drifted = "1-100 区间有 24 个质数，101-200 区间有 22 个，201-300 区间有 17 个。"
    text, _ctx = await _compose("", drifted, HELPER_SUMMARY)
    assert text == drifted, "A replacement with Tool names and result codes is not a better answer"
    for leaked in ("add_chart", "add_image", "chart_edit_incomplete", "sec_", "art_"):
        assert leaked not in text


async def test_guard_still_swaps_in_a_clean_grounded_summary() -> None:
    drifted = "1-100 区间有 24 个质数，101-200 区间有 22 个，201-300 区间有 17 个。"
    clean = "1-100 区间有 25 个质数，101-200 区间有 21 个，201-300 区间有 16 个。"
    text, _ctx = await _compose("", drifted, clean)
    assert text == clean
