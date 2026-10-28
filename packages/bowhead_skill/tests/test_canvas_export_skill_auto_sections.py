"""canvas-xlsx/pptx/pdf 可靠性修复 — 服务端权威灌入 sections，不再要求 LLM 逐字塞
整份 canvas JSON 进 skill_invoke 的流式 tool-call 参数。

真实故障 (prod t_76b9db41ad57, 2026-07-19): 用户要 xlsx，compute peer 建好 8-chart
canvas 后两次调 skill_invoke(canvas-xlsx) 都因 `streamed tool args JSON
invalid/truncated` 失败 (LLM 把整份 sections 塞进流式参数被截断)，随后放弃，从未
产出 .xlsx —— 用户看到的"下载不下来"其实是文件从没生成。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_skill.runtime import SkillRuntime
from bowhead_skill.tools import _CANVAS_EXPORT_SKILLS, _inject_current_canvas_sections, build_skill_tools

from bowhead_skill import SkillCatalog

_CATALOG_ROOT = Path(__file__).resolve().parents[1] / "catalog"


class _FakeSection:
    def __init__(
        self,
        sec_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        quarantined: bool = False,
        quarantine_reason: str | None = None,
    ) -> None:
        self._id = sec_id
        self._kind = kind
        self._payload = payload
        self.quarantined = quarantined
        self.quarantine_reason = quarantine_reason

    @property
    def id(self) -> str:
        return self._id

    @property
    def kind(self) -> str:
        return self._kind

    def to_report_section(self) -> dict[str, Any]:
        out = dict(self._payload)
        out.update({"id": self._id, "kind": self._kind})
        return out


class _FakeCanvas:
    def __init__(self, canvas_id: str, title: str | None) -> None:
        self.id = canvas_id
        self.title = title


class _FakeCanvasStore:
    """duck-types the two CanvasStore methods _inject_current_canvas_sections uses."""

    def __init__(self, canvas: _FakeCanvas | None, sections: list[_FakeSection]) -> None:
        self._canvas = canvas
        self._sections = sections
        self.export_projection_calls: list[str] = []

    async def get_canvas_by_session(self, session_id: str) -> _FakeCanvas | None:
        return self._canvas

    async def list_export_projection(self, canvas_id: str) -> list[_FakeSection]:
        self.export_projection_calls.append(canvas_id)
        return self._sections


def _ctx(canvas_store: Any = None, session_id: str = "") -> Any:
    return SimpleNamespace(canvas_store=canvas_store, session_id=session_id)


# ─── Gate against exporting before compose (116 dogfood 1.0.74 P1) ──────────────────────────────


async def test_blocks_export_when_only_tool_stage_sections_exist() -> None:
    """画布上只有工具阶段产物（chart / code_block）⇒ 报告还没 compose ⇒ 不许导出。

    真机形状（t_f3441c8bb6af）：单轮「做份报告 + 导出成 Word」里，导出跑在 compose
    **之前 6 秒**，画布上只有 chart+code_block。旧闸只问「有没有内容」，rows 非空就放行，
    于是产出一份只有裸数据表、没有标题/KPI/叙述的 .docx，而答案照说「完整报告已导出」。
    比空文件更坏——它看着像成品。
    """
    store = _FakeCanvasStore(
        _FakeCanvas("cv_race", "Bowhead Canvas"),
        [
            _FakeSection("sec_c1", "code_block", {"code": "print(1)"}),
            _FakeSection("sec_ch1", "chart", {"spec": {}}),
            _FakeSection("sec_ch2", "chart", {"spec": {}}),
        ],
    )
    input_data: dict[str, Any] = {}
    reason = await _inject_current_canvas_sections("canvas-docx", input_data, _ctx(store, "s_race"))

    assert reason is not None, "compose 未完成却放行 = 交付半成品"
    assert "has not been composed yet" in reason
    assert "Do NOT tell the user the file was generated." in reason
    assert "sections" not in input_data  # No content injected, so the caller will not render.


async def test_allows_export_once_the_report_is_composed() -> None:
    """compose 产物在场（h1/narrative/kpi_row 任一）⇒ 照常导出。

    对照实验（t_7c3295aa5b6b）：同一会话第二轮、画布已完整时导出，产物包含标题/KPI/
    叙述/建议——这一支必须放行，否则把好用的路径也堵了。
    """
    store = _FakeCanvasStore(
        _FakeCanvas("cv_done", "2026年7月经营总结"),
        [
            _FakeSection("sec_c1", "code_block", {"code": "print(1)"}),
            _FakeSection("sec_ch1", "chart", {"spec": {}}),
            _FakeSection("sec_h1", "h1", {"text": "2026年7月经营总结"}),
            _FakeSection("sec_n1", "narrative", {"markdown": "7 月营业额 9.80 亿…"}),
        ],
    )
    input_data: dict[str, Any] = {}
    reason = await _inject_current_canvas_sections("canvas-docx", input_data, _ctx(store, "s_done"))

    assert reason is None
    assert [s["kind"] for s in input_data["sections"]] == ["code_block", "chart", "h1", "narrative"]
    assert input_data["title"] == "2026年7月经营总结"


async def test_narrative_alone_is_enough_to_export() -> None:
    """只有叙述没有 h1 也算成型——判据是「有没有 compose 产物」，不是「齐不齐」。"""
    store = _FakeCanvasStore(
        _FakeCanvas("cv_n", None),
        [_FakeSection("sec_n", "narrative", {"markdown": "结论……"})],
    )
    input_data: dict[str, Any] = {}

    assert await _inject_current_canvas_sections("canvas-pptx", input_data, _ctx(store, "s_n")) is None
    assert input_data["sections"][0]["kind"] == "narrative"


# ─── _inject_current_canvas_sections: unit-level ───────────────────────


async def test_injects_sections_and_title_from_active_canvas() -> None:
    store = _FakeCanvasStore(
        _FakeCanvas("cv_1", "Q3 Capex Model"),
        [
            _FakeSection("sec_1", "h1", {"text": "Capex"}),
            _FakeSection("sec_2", "kpi_row", {"cells": [{"lbl": "Total", "val": "$314.7B"}]}),
        ],
    )
    input_data: dict[str, Any] = {}
    await _inject_current_canvas_sections("canvas-xlsx", input_data, _ctx(store, "s_1"))

    assert [s["id"] for s in input_data["sections"]] == ["sec_1", "sec_2"]
    assert input_data["sections"][0] == {"text": "Capex", "id": "sec_1", "kind": "h1"}
    assert input_data["title"] == "Q3 Capex Model"
    assert store.export_projection_calls == ["cv_1"]


async def test_quarantined_section_blocks_export_instead_of_injecting() -> None:
    """i18n v1 R4 parity (canvas_export.py:470-484): a quarantined section = known-bad
    data. The live job-based export path rejects the whole export rather than shipping
    it; this must do the same instead of silently baking the bad numbers into the file."""
    store = _FakeCanvasStore(
        _FakeCanvas("cv_1", "Q3 Capex Model"),
        [
            _FakeSection("sec_1", "h1", {"text": "Capex"}),
            _FakeSection(
                "sec_2",
                "kpi_row",
                {"cells": [{"lbl": "Total", "val": "$999B (WRONG SCALE)"}]},
                quarantined=True,
                quarantine_reason="off-by-1000 scale error",
            ),
        ],
    )
    input_data: dict[str, Any] = {}
    reason = await _inject_current_canvas_sections("canvas-xlsx", input_data, _ctx(store, "s_1"))

    assert reason is not None
    assert "1 canvas section(s) are quarantined" in reason
    assert "sec_2" in reason
    assert "off-by-1000 scale error" in reason
    # the block must be all-or-nothing — no partial/contaminated injection either.
    assert "sections" not in input_data
    assert "title" not in input_data


async def test_multiple_quarantined_sections_all_listed_up_to_five() -> None:
    quarantined_sections = [_FakeSection(f"sec_q{i}", "kpi_row", {}, quarantined=True, quarantine_reason=f"reason{i}") for i in range(7)]
    store = _FakeCanvasStore(_FakeCanvas("cv_1", "T"), quarantined_sections)
    input_data: dict[str, Any] = {}
    reason = await _inject_current_canvas_sections("canvas-xlsx", input_data, _ctx(store, "s_1"))

    assert reason is not None
    assert "7 canvas section(s) are quarantined" in reason
    assert reason.count(" - section ") == 5  # capped preview, not all 7


@pytest.mark.parametrize("name", sorted(_CANVAS_EXPORT_SKILLS))
async def test_applies_to_all_three_export_skills(name: str) -> None:
    store = _FakeCanvasStore(_FakeCanvas("cv_1", None), [_FakeSection("sec_1", "h1", {"text": "x"})])
    input_data: dict[str, Any] = {}
    await _inject_current_canvas_sections(name, input_data, _ctx(store, "s_1"))
    assert input_data["sections"] == [{"text": "x", "id": "sec_1", "kind": "h1"}]


async def test_explicit_title_override_is_preserved() -> None:
    store = _FakeCanvasStore(_FakeCanvas("cv_1", "Server Title"), [_FakeSection("sec_1", "h1", {})])
    input_data: dict[str, Any] = {"title": "LLM-supplied title"}
    await _inject_current_canvas_sections("canvas-xlsx", input_data, _ctx(store, "s_1"))
    assert input_data["title"] == "LLM-supplied title"


async def test_non_export_skill_is_noop() -> None:
    store = _FakeCanvasStore(_FakeCanvas("cv_1", "T"), [_FakeSection("sec_1", "h1", {})])
    input_data: dict[str, Any] = {"code": "print(1)"}
    await _inject_current_canvas_sections("python_sandbox", input_data, _ctx(store, "s_1"))
    assert input_data == {"code": "print(1)"}
    assert store.export_projection_calls == []


async def test_no_canvas_store_falls_back_to_llm_sections() -> None:
    """无 canvas_store (如未接真 ctx 的测试/eval harness) → no-op, 保留 LLM 显式传的 sections."""
    input_data: dict[str, Any] = {"sections": [{"id": "s1", "kind": "h1", "text": "manual"}]}
    await _inject_current_canvas_sections("canvas-xlsx", input_data, _ctx(None, "s_1"))
    assert input_data == {"sections": [{"id": "s1", "kind": "h1", "text": "manual"}]}


async def test_ctx_is_none_is_noop() -> None:
    input_data: dict[str, Any] = {"sections": [{"id": "s1", "kind": "h1"}]}
    await _inject_current_canvas_sections("canvas-xlsx", input_data, None)
    assert input_data == {"sections": [{"id": "s1", "kind": "h1"}]}


async def test_no_active_canvas_for_session_falls_back() -> None:
    store = _FakeCanvasStore(None, [])
    input_data: dict[str, Any] = {"sections": [{"id": "s1", "kind": "h1"}]}
    await _inject_current_canvas_sections("canvas-xlsx", input_data, _ctx(store, "s_1"))
    assert input_data == {"sections": [{"id": "s1", "kind": "h1"}]}


async def test_empty_canvas_falls_back_to_llm_sections() -> None:
    """canvas 存在但目前 0 section (peer 还没 add_chart 过) → 别用空数组覆盖."""
    store = _FakeCanvasStore(_FakeCanvas("cv_1", "T"), [])
    input_data: dict[str, Any] = {"sections": [{"id": "s1", "kind": "h1"}]}
    await _inject_current_canvas_sections("canvas-xlsx", input_data, _ctx(store, "s_1"))
    assert input_data == {"sections": [{"id": "s1", "kind": "h1"}]}
    assert store.export_projection_calls == ["cv_1"]


async def test_store_exception_is_swallowed_not_raised() -> None:
    class _BoomStore:
        async def get_canvas_by_session(self, session_id: str) -> Any:
            raise RuntimeError("db unavailable")

    input_data: dict[str, Any] = {"sections": [{"id": "s1", "kind": "h1"}]}
    await _inject_current_canvas_sections("canvas-xlsx", input_data, _ctx(_BoomStore(), "s_1"))
    assert input_data == {"sections": [{"id": "s1", "kind": "h1"}]}


# ─── end-to-end through skill_invoke's _invoke handler ─────────────────


class _CapturingRuntime:
    def __init__(self) -> None:
        self.last_input_data: dict[str, Any] | None = None
        self.run_call_count = 0

    async def run(self, spec: Any, *, entry_script: Any, input_data: dict[str, Any], extra_args: Any) -> Any:
        self.run_call_count += 1
        self.last_input_data = input_data

        class _R:
            ok = True
            exit_code = 0
            stdout = stderr = ""
            result_json: dict[str, Any] = {}
            outputs: list[Any] = []
            workdir = None
            duration_ms = 0
            error = None

        return _R()


def _mk_builtin_skill(root: Any, name: str) -> None:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nbody\n", encoding="utf-8")
    (d / "scripts").mkdir()
    (d / "scripts" / "render_from_canvas.py").write_text("print('ok')\n", encoding="utf-8")


async def test_skill_invoke_end_to_end_injects_canvas_sections(tmp_path: Any) -> None:
    _mk_builtin_skill(tmp_path, "canvas-xlsx")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _CapturingRuntime()
    inv = next(t for t in build_skill_tools(cat, rt) if t.name == "skill_invoke")

    store = _FakeCanvasStore(
        _FakeCanvas("cv_1", "Top3 Capex"),
        [_FakeSection("sec_1", "h1", {"text": "Capex"})],
    )
    ctx = _ctx(store, "s_1")

    out = await inv.handler(
        {"name": "canvas-xlsx", "script": "render_from_canvas.py", "input": {}},
        ctx,
    )

    assert out["ok"] is True
    assert rt.last_input_data is not None
    assert rt.last_input_data["sections"] == [{"text": "Capex", "id": "sec_1", "kind": "h1"}]
    assert rt.last_input_data["title"] == "Top3 Capex"


async def test_skill_invoke_end_to_end_quarantined_canvas_blocks_before_running(tmp_path: Any) -> None:
    """A quarantined section must short-circuit BEFORE runtime.run — never render a file
    from (or silently drop) known-bad data; the tool call fails loudly instead."""
    _mk_builtin_skill(tmp_path, "canvas-xlsx")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _CapturingRuntime()
    inv = next(t for t in build_skill_tools(cat, rt) if t.name == "skill_invoke")

    store = _FakeCanvasStore(
        _FakeCanvas("cv_1", "Top3 Capex"),
        [_FakeSection("sec_1", "kpi_row", {}, quarantined=True, quarantine_reason="off-by-1000 scale error")],
    )
    ctx = _ctx(store, "s_1")

    out = await inv.handler(
        {"name": "canvas-xlsx", "script": "render_from_canvas.py", "input": {}},
        ctx,
    )

    assert "error" in out
    assert "quarantined" in out["error"]
    assert rt.run_call_count == 0


async def test_skill_invoke_end_to_end_without_ctx_uses_llm_sections(tmp_path: Any) -> None:
    """ctx=None (旧式单测/无真会话调用) 仍走老路径: 用 LLM 显式传的 sections."""
    _mk_builtin_skill(tmp_path, "canvas-xlsx")
    cat = SkillCatalog(roots=[tmp_path])
    cat.refresh()
    rt = _CapturingRuntime()
    inv = next(t for t in build_skill_tools(cat, rt) if t.name == "skill_invoke")

    out = await inv.handler(
        {
            "name": "canvas-xlsx",
            "script": "render_from_canvas.py",
            "input": {"sections": [{"id": "s1", "kind": "h1", "text": "manual"}]},
        },
        None,
    )

    assert out["ok"] is True
    assert rt.last_input_data is not None
    assert rt.last_input_data["sections"] == [{"id": "s1", "kind": "h1", "text": "manual"}]


# ─── real reproduction: large canvas, real catalog skill, real subprocess ──


@pytest.mark.skipif(not _CATALOG_ROOT.is_dir(), reason="bowhead_skill catalog missing")
async def test_real_canvas_xlsx_produces_file_from_large_canvas_with_tiny_llm_args() -> None:
    """Reproduces prod t_76b9db41ad57 end-to-end against the REAL canvas-xlsx skill +
    a real subprocess SkillRuntime: an 8-section canvas (same order of magnitude as the
    failing turn) renders to a real .xlsx while the LLM's own skill_invoke call carries
    an EMPTY input — i.e. zero bytes of canvas JSON ever cross the LLM's streamed
    tool-call arguments, so the truncation failure mode is structurally impossible."""
    cat = SkillCatalog(roots=[_CATALOG_ROOT])
    cat.refresh()
    rt = SkillRuntime()
    inv = next(t for t in build_skill_tools(cat, rt) if t.name == "skill_invoke")

    sections = [
        _FakeSection("sec_eb", "eyebrow", {"text": "2025-2026E"}),
        _FakeSection("sec_h1", "h1", {"text": "Top 3 互联网巨头 Capex 支出模型"}),
        _FakeSection(
            "sec_kpi",
            "kpi_row",
            {"cells": [{"lbl": "2025 合计", "val": "$314.7B"}, {"lbl": "2026E", "val": "$525.0B"}]},
        ),
    ] + [
        _FakeSection(
            f"sec_tbl_{i}",
            "data_table",
            {
                "caption": f"Table {i}",
                "columns": [{"key": "k", "header": "Metric"}, {"key": "v", "header": "Value", "numeric": True}],
                "rows": [{"k": f"row{j}", "v": j} for j in range(5)],
            },
        )
        for i in range(6)
    ]
    store = _FakeCanvasStore(_FakeCanvas("cv_repro", "Top 3 互联网巨头 Capex 支出模型"), sections)
    ctx = _ctx(store, "s_repro")

    out = await inv.handler(
        {"name": "canvas-xlsx", "script": "render_from_canvas.py", "input": {}},
        ctx,
    )

    assert out["ok"] is True, out
    result_json = out["result_json"]
    assert result_json["sections"] == len(sections)
    workdir = Path(result_json["path"]).parent
    assert (workdir / "output.xlsx").is_file()
    assert (workdir / "output.xlsx").stat().st_size > 0


async def test_empty_canvas_with_no_llm_sections_blocks_instead_of_empty_file() -> None:
    """★ 真机形状（dev 2026-08-06, t_cfd9a579fe2a）：导出与画布重写同轮，
    export skill 抢在新 section 落库之前跑 ⇒ projection 此刻为空。

    此时**不能**放行：canvas-pdf/docx 的 main() 都有 `(empty canvas)` 占位兜底，
    放行的后果是产出一份空文件、skill 仍 ok:true、产物照常登记成 deliverable、
    模型照说"已成功导出可直接下载" —— 用户下载到空文档。宁可响亮地失败。
    """
    store = _FakeCanvasStore(_FakeCanvas("cv_1", "T"), [])
    input_data: dict[str, Any] = {"title": "只传了标题，没传 sections"}
    reason = await _inject_current_canvas_sections("canvas-docx", input_data, _ctx(store, "s_1"))

    assert reason is not None, "空 canvas + 调用方没传 sections 必须 block，否则产空文件还报成功"
    assert "empty file" in reason
    assert "Do NOT tell the user the file was generated" in reason
    assert "sections" not in input_data


@pytest.mark.parametrize("name", sorted(_CANVAS_EXPORT_SKILLS))
async def test_empty_canvas_block_applies_to_every_export_skill(name: str) -> None:
    """闸对四种格式一视同仁 —— 这不是 docx 专有问题，pdf/pptx/xlsx 同样会产空文件。"""
    store = _FakeCanvasStore(_FakeCanvas("cv_1", None), [])
    assert await _inject_current_canvas_sections(name, {}, _ctx(store, "s_1")) is not None


# ── ★ 116 production 1.0.74 reproduction: one branch is insufficient; all four early-return paths must share one decision ──
#
# t_f9c3fc0d5ab4: export ran before the canvas was created → `get_canvas_by_session` returned None →
# the previous version returned from an **earlier point** (only the `not rows` branch blocked export) →
# it still produced a 36776B empty `(empty canvas)` .docx and registered it as a deliverable.


async def test_no_canvas_yet_blocks_not_silently_empty() -> None:
    """★ 真机形状：画布还没建出来（导出抢跑）。"""
    store = _FakeCanvasStore(None, [])  # get_canvas_by_session → None
    reason = await _inject_current_canvas_sections("canvas-docx", {"title": "T"}, _ctx(store, "s_1"))
    assert reason is not None, "画布不存在时放行 = 产一份空文件还报成功"
    assert "no canvas content to export" in reason


async def test_missing_canvas_store_blocks_when_caller_passed_nothing() -> None:
    """ctx 里没有 canvas_store（peer 上下文没带全）——同样不能产空文件。"""
    reason = await _inject_current_canvas_sections("canvas-pdf", {}, _ctx(None, "s_1"))
    assert reason is not None


async def test_missing_session_id_blocks_when_caller_passed_nothing() -> None:
    store = _FakeCanvasStore(_FakeCanvas("cv_1", "T"), [_FakeSection("s1", "h1", {})])
    reason = await _inject_current_canvas_sections("canvas-pdf", {}, _ctx(store, ""))
    assert reason is not None


async def test_store_exception_blocks_when_caller_passed_nothing() -> None:
    """取数抛错也是"没有内容可渲染"，不能静默产空文件。"""

    class _BoomStore:
        async def get_canvas_by_session(self, session_id: str) -> Any:
            raise RuntimeError("db unavailable")

    reason = await _inject_current_canvas_sections("canvas-xlsx", {}, _ctx(_BoomStore(), "s_1"))
    assert reason is not None


@pytest.mark.parametrize(
    "ctx_kwargs",
    [
        {"canvas_store": None, "session_id": "s_1"},
        {"canvas_store": _FakeCanvasStore(None, []), "session_id": "s_1"},
        {"canvas_store": _FakeCanvasStore(_FakeCanvas("cv_1", "T"), []), "session_id": "s_1"},
    ],
    ids=["no_store", "no_canvas", "empty_projection"],
)
async def test_caller_supplied_sections_still_pass_through_every_path(ctx_kwargs: dict) -> None:
    """老契约不能被这次收口打破：调用方显式传了 sections 一律放行。"""
    input_data: dict[str, Any] = {"sections": [{"id": "s1", "kind": "h1"}]}
    reason = await _inject_current_canvas_sections("canvas-pdf", input_data, SimpleNamespace(**ctx_kwargs))
    assert reason is None
    assert input_data["sections"] == [{"id": "s1", "kind": "h1"}]
