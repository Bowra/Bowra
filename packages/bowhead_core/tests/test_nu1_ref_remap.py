"""ν1 第一遍映射 + 直接剥（2026-09-06，`BOWHEAD_REF_REMAP_FIRST_PASS` 默认 1）。

116 90 天：1244 个 spawn 里 19.5% 至少被 ν1 拒一次，每次拒绝 peer 多绕一圈；被拒的 417 个引用逐个对库，
多数是**拿错命名空间**（call id 当 section id、code_block 段当 artifact、artifact 当 kb 引用），有唯一真实目标。
新路：先按本轮 tool_invocations + 画布 sections 建索引映射回去，剩下没有目标的直接剥（无证据则 bail），**不重试**。
两层：纯函数（索引 / 映射）→ 穿过真 `subagent_return` 工具（接线 + 计数器不再增长 + 开关关回旧路）。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core._ref_validation import build_ref_remap_index, remap_invalid_refs
from bowhead_core.subagent_return_tool import CTX_PAYLOAD_ATTR, PI_B_BAIL_KIND, TERMINAL_FLAG, register_subagent_return_tool
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

# ── Pure functions ──


def _inv(tool_name: str, call_id: str, result: Any) -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool_name, call_id=call_id, result=result, nested=None)


def _sec(sid: str, kind: str, payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(id=sid, kind=kind, payload=payload)


def _sandbox(call_id: str, files: list[tuple[str, str, str]], *, stdout_art: str | None = None) -> SimpleNamespace:
    """生产形状（bowhead_skill/tools.py `_invoke` + `_output_artifacts_from_result_json`）：顶层 `outputs` 是**文件名字符串**，
    带 id 的字典在 `output_artifacts`（只有 stdout / structured 有 data_role，文件产物只有 mime）；runner 顺序=文件在前、stdout 最后。"""
    arts = [{"artifact_id": a, "name": n, "mime": m, "size": 1} for n, m, a in files]
    if stdout_art:
        arts.append({"artifact_id": stdout_art, "name": "sandbox-stdout.txt", "mime": "text/plain", "size": 1, "data_role": "stdout"})
    return _inv(
        "python_sandbox",
        call_id,
        {
            "ok": True,
            "exit_code": 0,
            "stdout": "…",
            "result_json": {"outputs": [dict(a) for a in arts]},
            "outputs": [n for n, _, _ in files],
            "output_artifacts": arts,
            "error": None,
        },
    )


@pytest.mark.unit
def test_index_maps_call_ids_artifacts_code_blocks_and_chart_data_artifacts() -> None:
    """review 修：add_chart 的结果没有 artifact_id（生产形状），图的数据 artifact 在 chart 段的 `spec.data_artifact_id`；
    多产出记 data_role / mime；同一数据 artifact 喂了两张图时不映射；code_block 段的 call_id **不**登记成 chart 目标。"""
    invs = [
        _inv("add_chart", "call_00_chart", {"section_id": "sec_chart1", "kind": "chart"}),
        _sandbox("call_01_py", [("fig.png", "image/png", "art_png")], stdout_art="art_stdout"),
        _inv("bi_query", "call_02_bi", {"results": [{"artifact_id": "art_bi1", "_ref_artifact_id": "art_bi1"}]}),
        _inv("subagent_return", "call_03_ret", "not-a-dict"),
    ]
    secs = [
        _sec("sec_img1", "image", {"artifact_id": "art_png", "caption": "x"}),
        _sec("sec_code1", "code_block", {"call_id": "call_01_py", "output_artifact_ids": ["art_png", "art_stdout"]}),
        _sec("sec_chart1", "chart", {"spec": {"data_artifact_id": "art_csv", "source_meta": {"tool_call_id": "call_09_src"}}}),
        _sec("sec_chart2", "chart", {"spec": {"data_artifact_id": "art_shared"}}),
        _sec("sec_chart3", "chart", {"spec": {"data_artifact_id": "art_shared"}}),
        _sec("sec_narr", "narrative", {"markdown": "…"}),
    ]
    idx = build_ref_remap_index(tool_invocations=invs, sections=secs)
    assert idx["call_to_section"]["call_00_chart"] == "sec_chart1"
    assert idx["call_to_section"]["call_09_src"] == "sec_chart1", "图 spec.source_meta.tool_call_id（产数据的 sandbox call）→ 图段"
    assert "call_01_py" not in idx["call_to_section"], "code_block 段的 call_id 不是 chart 目标（review 第二轮 P2）"
    assert idx["artifact_to_section"]["art_png"] == "sec_img1"
    assert idx["artifact_to_section"]["art_csv"] == "sec_chart1", "图的数据 artifact 唯一对应一张图 ⇒ 映射"
    assert "art_shared" not in idx["artifact_to_section"], "同一数据 artifact 喂两张图 ⇒ 不猜"
    assert idx["call_to_artifacts"]["call_01_py"] == ["art_png", "art_stdout"] and idx["call_to_artifacts"]["call_02_bi"] == ["art_bi1"]
    assert idx["section_to_artifacts"]["sec_code1"] == ["art_png", "art_stdout"]
    assert idx["artifact_roles"] == {"art_stdout": "stdout", "art_bi1": "structured"}, "文件产物没有 data_role；bi_query 产物按表记"
    assert idx["artifact_mimes"]["art_png"] == "image/png"
    assert idx["chart_data_artifacts"] == ["art_csv", "art_shared"]


@pytest.mark.unit
def test_remap_fixes_namespace_mistakes_and_leaves_hopeless_ones() -> None:
    idx = build_ref_remap_index(
        tool_invocations=[
            _inv("add_chart", "call_00_chart", {"section_id": "sec_chart1"}),
            _sandbox("call_01_py", [], stdout_art="art_out1"),
            _sandbox("call_04_multi", [("fig.png", "image/png", "art_png2")], stdout_art="art_std2"),
            _sandbox("call_05_ambig", [("a.csv", "text/csv", "art_a"), ("b.csv", "text/csv", "art_b")]),
            # Review round 2 P1 shape: df.to_csv + print ⇒ [csv(no role), stdout]
            _sandbox("call_06_csv", [("out.csv", "text/csv", "art_csv6")], stdout_art="art_std6"),
            # Chart data CSV + stdout: computation must reference chart data (frontend embeds only when computation == chart data_artifact_id).
            _sandbox("call_07_chartdata", [("series.csv", "text/csv", "art_series")], stdout_art="art_std7"),
            # Only PNG + stdout; peer uses sandbox call ID as chart_section_id ⇒ follow artifact to image section.
            _sandbox("call_08_img", [("plot.png", "image/png", "art_plot")], stdout_art="art_std8"),
        ],
        sections=[
            _sec("sec_code1", "code_block", {"call_id": "call_01_py", "output_artifact_ids": ["art_out1"]}),
            _sec("sec_code8", "code_block", {"call_id": "call_08_img", "output_artifact_ids": ["art_plot", "art_std8"]}),
            _sec("sec_chart1", "chart", {"spec": {"data_artifact_id": "art_chart1"}}),
            _sec("sec_chart7", "chart", {"spec": {"data_artifact_id": "art_series"}}),
            _sec("sec_img8", "image", {"artifact_id": "art_plot"}),
        ],
    )
    payload = {
        "insights": [
            {
                "narrative": "合规率 [num:11.39:%:src:sec_code1#highlight=r2] 偏低，占比 [num:3:%:src:call_05_ambig]",
                "evidence": [
                    {"kind": "chart", "chart_section_id": "call_00_chart"},  # Call ID used as section ID
                    {"kind": "chart", "chart_section_id": "art_chart1"},  # Chart data artifact used as section
                    {"kind": "computation", "computation_artifact_id": "sec_code1"},  # Code section used as artifact (sole output)
                    {"kind": "computation", "computation_artifact_id": "call_01_py"},  # Call ID used as artifact (sole output)
                    {"kind": "computation", "computation_artifact_id": "call_04_multi"},  # PNG + stdout ⇒ choose stdout
                    {"kind": "computation", "computation_artifact_id": "call_05_ambig"},  # Two CSVs without role ⇒ no remapping
                    {"kind": "chart", "chart_section_id": "22"},  # Fabricated
                    {"kind": "computation", "computation_artifact_id": "manual_calc"},  # Fabricated
                    {"kind": "table", "table_artifact_id": "call_06_csv"},  # Table ⇒ CSV, not stdout
                    {"kind": "computation", "computation_artifact_id": "call_06_csv"},  # Computation ⇒ stdout (CSV is not any chart's data)
                    {"kind": "computation", "computation_artifact_id": "call_07_chartdata"},  # Computation ⇒ chart data CSV > stdout
                    {"kind": "table", "table_artifact_id": "call_04_multi"},  # Table ⇒ no table among PNG + stdout ⇒ no remapping
                    {"kind": "chart", "chart_section_id": "call_08_img"},  # Sandbox call used as chart ⇒ via PNG to image section, not code section
                ],
            }
        ]
    }
    errors = [
        {"kind": "chart_ref_not_in_allowlist", "field": "insights[0].evidence[0].chart_section_id", "ref_id": "call_00_chart"},
        {"kind": "chart_ref_not_in_allowlist", "field": "insights[0].evidence[1].chart_section_id", "ref_id": "art_chart1"},
        {"kind": "data_ref_not_in_allowlist", "field": "insights[0].evidence[2].computation_artifact_id", "ref_id": "sec_code1"},
        {"kind": "data_ref_not_in_allowlist", "field": "insights[0].evidence[3].computation_artifact_id", "ref_id": "call_01_py"},
        {"kind": "data_ref_not_in_allowlist", "field": "insights[0].evidence[4].computation_artifact_id", "ref_id": "call_04_multi"},
        {"kind": "data_ref_not_in_allowlist", "field": "insights[0].evidence[5].computation_artifact_id", "ref_id": "call_05_ambig"},
        {"kind": "chart_ref_not_in_allowlist", "field": "insights[0].evidence[6].chart_section_id", "ref_id": "22"},
        {"kind": "data_ref_not_in_allowlist", "field": "insights[0].evidence[7].computation_artifact_id", "ref_id": "manual_calc"},
        {"kind": "data_ref_not_in_allowlist", "field": "insights[0].evidence[8].table_artifact_id", "ref_id": "call_06_csv"},
        {"kind": "data_ref_not_in_allowlist", "field": "insights[0].evidence[9].computation_artifact_id", "ref_id": "call_06_csv"},
        {"kind": "data_ref_not_in_allowlist", "field": "insights[0].evidence[10].computation_artifact_id", "ref_id": "call_07_chartdata"},
        {"kind": "data_ref_not_in_allowlist", "field": "insights[0].evidence[11].table_artifact_id", "ref_id": "call_04_multi"},
        {"kind": "chart_ref_not_in_allowlist", "field": "insights[0].evidence[12].chart_section_id", "ref_id": "call_08_img"},
        {
            "kind": "num_src_not_in_allowlist",
            "field": "insights[0].narrative",
            "ref_id": "sec_code1",
            "value": "[num:11.39:%:src:sec_code1#highlight=r2]",
        },
        {"kind": "num_src_not_in_allowlist", "field": "insights[0].narrative", "ref_id": "call_05_ambig", "value": "[num:3:%:src:call_05_ambig]"},
        {"kind": "kb_ref_not_in_allowlist", "field": "insights[0].narrative", "ref_id": "call_01_py", "value": "[ref:call_01_py]"},
    ]
    out, remapped = remap_invalid_refs(payload, errors, idx)
    ev = out["insights"][0]["evidence"]
    assert ev[0]["chart_section_id"] == "sec_chart1" and ev[1]["chart_section_id"] == "sec_chart1"
    assert ev[2]["computation_artifact_id"] == "art_out1" and ev[3]["computation_artifact_id"] == "art_out1"
    assert ev[4]["computation_artifact_id"] == "art_std2", "PNG + stdout ⇒ 取 stdout（不是第一个 PNG）"
    assert ev[5]["computation_artifact_id"] == "call_05_ambig", "两个 CSV 分不清 ⇒ 不映射，留给 strip（不绑 src 比绑错安全）"
    assert ev[6]["chart_section_id"] == "22" and ev[7]["computation_artifact_id"] == "manual_calc", "没有目标的原样留给 strip"
    assert ev[8]["table_artifact_id"] == "art_csv6", "表格证据指到 CSV，不指 stdout 文本（review 第二轮 P1）"
    assert ev[9]["computation_artifact_id"] == "art_std6", "计算证据：CSV 不是任何图的数据 ⇒ stdout"
    assert ev[10]["computation_artifact_id"] == "art_series", "计算证据：图的数据 CSV 优先于 stdout（前端据此内嵌图）"
    assert ev[11]["table_artifact_id"] == "call_04_multi", "PNG + stdout 里没有表 ⇒ 不映射"
    assert ev[12]["chart_section_id"] == "sec_img8", "sandbox call 当图：经唯一图片产物到 image 段，不是 code 段"
    narrative = out["insights"][0]["narrative"]
    assert "[num:11.39:%:src:art_out1#highlight=r2]" in narrative, "正文里 [num:] 的 src 也映射，highlight 尾巴保留"
    assert "[num:3:%:src:call_05_ambig]" in narrative, "映射不了的留给 strip 去烘焙"
    assert {r["from"] for r in remapped} == {
        "call_00_chart",
        "art_chart1",
        "sec_code1",
        "call_01_py",
        "call_04_multi",
        "call_06_csv",
        "call_07_chartdata",
        "call_08_img",
    }
    assert all(r["kind"] != "kb_ref_not_in_allowlist" for r in remapped), "正文 [ref:] 不映射"


# ── Wiring: through real subagent_return ──


class _Canvas:
    id = "canvas_test"


class _CanvasStore:
    def __init__(self, sections: list[Any]) -> None:
        self._secs = sections

    async def get_canvas_by_session(self, session_id: str) -> _Canvas:
        return _Canvas()

    async def list_sections(self, canvas_id: str) -> list[Any]:
        return list(self._secs)

    async def list_section_ids_by_kind(self, canvas_id: str, *, kind: str) -> set[str]:
        return {s.id for s in self._secs if s.kind == kind}


class _ArtifactStore:
    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    async def list_for_session(self, *, session_id: str, user_id: str) -> list[Any]:
        return [SimpleNamespace(id=i) for i in self._ids]


def _ctx(sections: list[Any], artifacts: list[str], invocations: list[Any]) -> ToolExecutionContext:
    ctx = ToolExecutionContext(workspace_id="ws_test", member_uid="m_test", user_id="u_test", turn_id="turn_t1", datarc=None, session_id="s_test")  # type: ignore[arg-type]
    ctx.canvas_store = _CanvasStore(sections)
    ctx.artifact_store = _ArtifactStore(artifacts)
    ctx._current_subagent_invocations = invocations
    ctx._current_subagent_id = "sub_A"
    return ctx


def _payload(chart_ref: str, *, narrative: str = "部门支出在两个维度都是结构性问题，合规率和趋势都低。" * 2) -> dict[str, Any]:
    return {
        "title": "EA 看板",
        "exec_summary": "整体合规率 11.39%。这里 narrative 足够长可以触发 evidence pass.",
        "kpis": [{"label": "合规率", "value": "11.39%", "sentiment": "bad"}],
        "insights": [
            {
                "title": "部门支出拖累",
                "thesis": "占 90% 单量但合规率 1.85%",
                "evidence": [{"kind": "chart", "chart_section_id": chart_ref, "caption": "对比"}],
                "narrative": narrative,
                "confidence": "high",
            }
        ],
        "recommendations": [{"priority": "P0", "action": "OA 必填字段", "owner": "IT 系统组"}],
    }


@pytest.fixture(autouse=True)
def _env() -> Any:
    prev = {k: os.environ.get(k) for k in ("BOWHEAD_REF_VALIDATE_STRICT", "BOWHEAD_REF_ALLOWLIST_INJECT", "BOWHEAD_REF_REMAP_FIRST_PASS")}
    os.environ["BOWHEAD_REF_VALIDATE_STRICT"] = "1"
    os.environ["BOWHEAD_REF_ALLOWLIST_INJECT"] = "0"
    os.environ.pop("BOWHEAD_REF_REMAP_FIRST_PASS", None)  # Defaults to 1
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.mark.unit
async def test_call_id_as_chart_ref_is_remapped_and_accepted_first_pass() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    secs = [_sec("sec_chart1", "chart", {"spec": {}})]
    invs = [_inv("add_chart", "call_00_chart", {"section_id": "sec_chart1", "kind": "chart"})]
    ctx = _ctx(secs, [], invs)
    inv = await reg.execute("subagent_return", {"payload": _payload("call_00_chart")}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert not inv.result.get("ν_ref_retry")
    assert getattr(ctx, "_ν_retry_counts", {}) == {}, "映射成功不进重试计数"
    stored = getattr(ctx, CTX_PAYLOAD_ATTR, None)
    assert isinstance(stored, dict), "payload 应已交给 composer（挂在 ctx 上）"
    assert stored["insights"][0]["evidence"][0]["chart_section_id"] == "sec_chart1", "call id 被映射成真实 section id"


@pytest.mark.unit
async def test_hopeless_ref_is_stripped_first_pass_without_retry() -> None:
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _ctx([], [], [])
    inv = await reg.execute("subagent_return", {"payload": _payload("sec_FAKE_XXX")}, ctx)
    assert inv.result["ok"] is True, inv.result
    assert not inv.result.get("ν_ref_retry"), "编的 id 不再打回 peer"
    assert getattr(ctx, "_ν_retry_counts", {}) == {}


@pytest.mark.unit
async def test_hopeless_ref_with_no_evidence_left_still_bails(monkeypatch: pytest.MonkeyPatch) -> None:
    """剥完没有任何证据 ⇒ 仍走既有 bail 信封（语义不变，只是不再先拒两次）。
    与 test_pi_b 同款：把 strip 桩成「剥成空壳」，因为真·空壳 payload 在 ν1 之前就会被 schema 检查拦下。"""

    def _strip_empty(payload: dict, errors: list) -> dict:
        return {
            "title": payload.get("title") or "(no title)",
            "exec_summary": "",
            "kpis": [],
            "insights": [{"title": "x", "thesis": "", "narrative": "", "evidence": [{"kind": "chart", "chart_section_id": None}]}],
            "recommendations": [],
        }

    monkeypatch.setattr("bowhead_core._ref_validation._strip_invalid_refs", _strip_empty)
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _ctx([], [], [])
    inv = await reg.execute("subagent_return", {"payload": _payload("sec_FAKE_XXX")}, ctx)
    assert inv.result["ok"] is True and inv.result.get(TERMINAL_FLAG) is True, inv.result
    assert inv.result.get("bail_kind") == PI_B_BAIL_KIND
    assert getattr(ctx, "_ν_retry_counts", {}).get(("turn_t1", "sub_A"), 0) == 0, "第一遍就 bail，不走重试计数"


@pytest.mark.unit
async def test_kill_switch_restores_reject_then_strip() -> None:
    os.environ["BOWHEAD_REF_REMAP_FIRST_PASS"] = "0"
    reg = ToolRegistry()
    register_subagent_return_tool(reg)
    ctx = _ctx([], [], [])
    inv = await reg.execute("subagent_return", {"payload": _payload("sec_FAKE_XXX")}, ctx)
    assert inv.result["ok"] is False and inv.result.get("ν_ref_retry") is True
    assert ctx._ν_retry_counts[("turn_t1", "sub_A")] == 1
