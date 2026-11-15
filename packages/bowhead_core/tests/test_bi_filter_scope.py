"""A compute return must record a scope judgment for filter values Datarc recognized but did not apply.

Regression check (2026-11-15, 1.7.38): for 「女装 2026年各月 月坪效」 the bi_query hint named 「女装」, yet two models labelled
the all-store rows as 女装 without ever addressing it. The judgment stays with the model; `subagent_return` only
refuses a payload that records none. The integration case feeds a real bi_query handler result into the gate.
"""

from __future__ import annotations

import base64

import pytest
from bowhead_core.agents._lead_context import set_current_subagent_invocations
from bowhead_core.bi_filter_scope import MAX_REFUSALS, REFUSAL_MARKER, review_scope_checks, unapplied_values_in_run
from bowhead_core.model_view import project_tool_result
from bowhead_core.subagent_return_tool import consume_returned_payload, register_subagent_return_tool
from bowhead_core.tool_delivery_review import TerminalCorrectionRefusal, failed_calls
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_core.types import ToolInvocation
from test_bi_query_fallback_measure import _cut, _download, _run

_VALUE = "女装"
_REPORT = {"models": ["门店表"], "measures": ["月坪效"], "dimensions": ["每月"], "date_filters": ["「日期」的「2026年」（2026-01-01 至 2026-11-14）"]}
_PARSE = {"tokens": [{"word": _VALUE, "kind": "dimension_value", "field": "种类大类", "value": _VALUE}], "unknown_words": [], "half_match_values": []}


def _query(report: dict | None = None, *, batched: bool = False, call_id: str = "q1") -> ToolInvocation:
    item = {"_parse": _PARSE, "query_explain": report if report is not None else _REPORT, "rows": [{"每月": "2026年1月", "月坪效": 1987.2}]}
    return ToolInvocation("bi_query", {}, call_id=call_id, result={"results": [item]} if batched else item)


def _check(handling: str, *, in_scope: bool = True, value: str = _VALUE) -> dict:
    return {"exec_summary": "月坪效走势。", "filter_scope_checks": [{"value": value, "in_scope": in_scope, "handling": handling}]}


def _refused_return(call_id: str) -> ToolInvocation:
    return ToolInvocation("subagent_return", {}, call_id=call_id, result=TerminalCorrectionRefusal({"ok": False, REFUSAL_MARKER: [_VALUE]}))


@pytest.mark.unit
@pytest.mark.parametrize("batched", [False, True])
def test_values_come_from_results_whose_report_lists_no_condition(batched: bool) -> None:
    assert unapplied_values_in_run([_query(batched=batched)]) == [_VALUE]
    assert unapplied_values_in_run([_query({**_REPORT, "filters": ["种类大类=女装"]}, batched=batched)]) == []
    failed = ToolInvocation("bi_query", {}, result=_query().result, error="timeout")
    assert unapplied_values_in_run([failed]) == []


@pytest.mark.unit
def test_a_return_without_a_judgment_is_refused_and_names_the_value() -> None:
    refusal = review_scope_checks({"exec_summary": "女装月坪效走势。"}, [_query()])
    assert refusal is not None and refusal[REFUSAL_MARKER] == [_VALUE]
    assert refusal["_schema_retry"] is True and f"「{_VALUE}」" in refusal["error"]


@pytest.mark.unit
@pytest.mark.parametrize(("handling", "in_scope"), [("stated_limit", True), ("incidental", False)])
def test_a_consistent_judgment_is_accepted(handling: str, in_scope: bool) -> None:
    assert review_scope_checks(_check(handling, in_scope=in_scope), [_query()]) is None


@pytest.mark.unit
@pytest.mark.parametrize(("handling", "in_scope"), [("incidental", True), ("stated_limit", False), ("ignored", True)])
def test_a_contradictory_judgment_is_refused(handling: str, in_scope: bool) -> None:
    assert review_scope_checks(_check(handling, in_scope=in_scope), [_query()]) is not None


@pytest.mark.unit
def test_requeried_needs_a_query_that_reports_a_condition() -> None:
    assert review_scope_checks(_check("requeried"), [_query()]) is not None
    requery = _query({**_REPORT, "models": ["品类表"], "filters": ["种类大类=女装"]}, call_id="q2")
    assert review_scope_checks(_check("requeried"), [_query(), requery]) is None


@pytest.mark.unit
def test_runs_without_unapplied_values_and_bails_need_nothing() -> None:
    assert review_scope_checks({"exec_summary": "ok"}, [_query({**_REPORT, "filters": ["种类大类=女装"]})]) is None
    assert review_scope_checks({"exec_summary": "", "error": {"kind": "domain_mismatch"}}, [_query()]) is None


@pytest.mark.unit
def test_the_refusal_budget_lets_the_return_through() -> None:
    prior = [_refused_return(f"r{index}") for index in range(MAX_REFUSALS)]
    assert review_scope_checks({"exec_summary": "女装月坪效走势。"}, [_query(), *prior]) is None


@pytest.mark.unit
def test_the_hint_leads_the_model_view_of_a_query_result() -> None:
    single = {"rows": [], "query_explain": _REPORT, "_preflight_hint": "note"}
    assert next(iter(project_tool_result("bi_query", single))) == "_preflight_hint"
    batched = project_tool_result("bi_query", {"results": [single]})
    assert next(iter(batched["results"][0])) == "_preflight_hint"
    assert list(single) == ["rows", "query_explain", "_preflight_hint"]  # The stored result keeps its order.


@pytest.mark.asyncio
async def test_subagent_return_refuses_then_accepts_after_a_real_query_result() -> None:
    value_prop = "d54=" + base64.b64encode(_VALUE.encode()).decode()
    cut = _cut(("2026年", "dateF"), (_VALUE, value_prop), (" ", "unknown"), ("月坪效", "m12"), ("每月", "groupByF_monthly"))
    out = await _run(
        "t-filter-scope", _download(["每月", "月坪效"], [{"每月": "2026年1月", "月坪效": 1987.2}], _REPORT), cut, "2026年 女装 月坪效 每月"
    )
    assert "not restricted to them: 「女装」" in out["_preflight_hint"] and "filter_scope_checks" in out["_preflight_hint"]

    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = ToolExecutionContext(workspace_id="ws_t", member_uid="m_t", user_id="u_t", turn_id="t_t", datarc=None, session_id="s_t")  # type: ignore[arg-type]
    calls = [ToolInvocation("bi_query", {}, call_id="query", result=out)]
    token = set_current_subagent_invocations(calls)
    try:
        refused = await reg.execute("subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "女装月坪效 1 月 1987.2。"}}, ctx)
        assert isinstance(refused.result, TerminalCorrectionRefusal) and refused.result[REFUSAL_MARKER] == [_VALUE]
        assert consume_returned_payload(ctx) is None
        refused.call_id = "refused_return"
        calls.append(refused)
        assert "refused_return" not in failed_calls(calls)  # A correction, not a failure the delivery review must explain.
        payload = {"delivery_surface": "chat", "exec_summary": "数据源里月坪效不能按女装筛选；全部门店 1 月为 1987.2。"}
        payload["filter_scope_checks"] = [{"value": _VALUE, "in_scope": True, "handling": "stated_limit"}]
        accepted = await reg.execute("subagent_return", {"payload": payload}, ctx)
        assert accepted.result["ok"] is True
    finally:
        token.var.reset(token)
