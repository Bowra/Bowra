"""批 5ⓑ review 修复轮：扁平沙箱入口 `python_exec` 必须在**每一条按名字认沙箱的下游面**上活着。

★ 这批 review 逮到的是一整族同形问题：仓里有约 10 处消费面按
`tool_name=="skill_invoke"` + `arguments["name"]=="python_sandbox"` 这一对认沙箱。
换个顶层工具名，工具照跑，但**用户看得见的东西集体消失**——画布里沙箱产的表没了、
「代码」页签空了、SkillCard 不见了、pycell 把源码 repr 截断到 120 字。

收敛策略：展示链统一走 `_surface_inner_skill_name`（`python_exec` → `python_sandbox`），
直接读 `inv.tool_name` 的几处（decomposer / mappers / pycell / trace emit）各自认两个名字。
本文件对**两条入口**跑同一组断言——扁平那一列全绿才算接线接完。
"""

from __future__ import annotations

import re

import pytest
from bowhead_core.canvas_composers.mappers import _map_code_block
from bowhead_core.pycell import render_code
from bowhead_core.subagent import (
    _extract_step_code_input,
    _prettify_input_preview,
    _surface_inner_skill_name,
    _tool_header_zh,
)
from bowhead_core.types import ToolInvocation

_CODE = "import pandas as pd\nprint(INPUTS['rows'].get('columns'))"
_FLAT_ARGS = {"code": _CODE, "inputs": {"rows": {"artifact_id": "art_x"}}}
_NESTED_ARGS = {"name": "python_sandbox", "script": "runner.py", "input": dict(_FLAT_ARGS)}
_SANDBOX_RESULT = {"ok": True, "result_json": {"stdout": "col\n1\n2\n", "outputs": []}}

# (label, tool_name, arguments) — two entry points, the same assertions
_ENTRIES = [
    pytest.param("skill_invoke", _NESTED_ARGS, id="老路 skill_invoke"),
    pytest.param("python_exec", _FLAT_ARGS, id="扁平 python_exec"),
]


def _inv(tool_name: str, args: dict) -> ToolInvocation:
    return ToolInvocation(tool_name=tool_name, arguments=args, result=_SANDBOX_RESULT, call_id="tl_x", duration_ms=12)


class _MI:
    """`_map_code_block` 要的最小 model-input 形状。"""

    def __init__(self, invs: list[ToolInvocation]) -> None:
        self.tool_invocations = invs
        self.turn_id = "t_1"
        self.canvas_id = "cv_1"
        self.user_id = "u_1"
        self.session_id = "s_1"


@pytest.mark.parametrize(("tool_name", "args"), _ENTRIES)
def test_step_surface_converges_to_the_pseudo_tool_name(tool_name: str, args: dict) -> None:
    """step / trace / FE 目录全按 `python_sandbox` 这个伪工具名认人——两条入口都要收敛到它。"""
    assert _surface_inner_skill_name(tool_name, args) == "python_sandbox"
    # Convergence enables mapping to Chinese headers (otherwise the step list shows a bare English identifier).
    assert _tool_header_zh(_surface_inner_skill_name(tool_name, args)) != "python_exec"


@pytest.mark.parametrize(("tool_name", "args"), _ENTRIES)
def test_step_label_never_shows_the_tool_code(tool_name: str, args: dict) -> None:
    """步骤行的副标题不许出现工具代号。

    116 真机 dogfood 2026-09-07 实拍：这一行渲的是「🐍 运行代码 · 运行: python_sandbox」——
    抬头已经说了在运行代码，后半段除了把内部标识符摆给客户看没给任何信息。
    （这条分支本身是对的：它挡住了「整段 Python 源码变成步骤标签」，只是标签选错了词。）

    突变验证：把 `_prettify_input_preview` 里那句改回 `"运行: python_sandbox"` → 本测试红。
    """
    surfaced = _surface_inner_skill_name(tool_name, args)
    label = _prettify_input_preview(surfaced, args)
    assert label, "步骤副标题不能是空的"
    for code in ("python_sandbox", "python_exec", "skill_invoke"):
        assert code not in label, f"步骤副标题泄漏了工具代号 {code}：{label!r}"


@pytest.mark.parametrize(("tool_name", "args"), _ENTRIES)
def test_step_label_follows_the_ui_language(tool_name: str, args: dict) -> None:
    """步骤副标题跟界面语言走。

    116 dogfood 2026-09-07：英文界面下这一行渲的是「🐍 Run Code · 运行: (代码)」——
    抬头（走 `_prettify_output_preview` 的 `_t`）已经英文了，副标题还是中文，
    因为 `_prettify_input_preview` 整个函数**从来没有 locale 概念**。
    以前看不出来：这条路上的串多半是用户查询原文或工具代号，本来就"没有语言"。

    突变验证：把 `_t("运行: (代码)", "Run: (code)")` 改回裸中文 → en 那半红。
    """
    from bowhead_core.agents._lead_context import reset_current_ui_language, set_current_ui_language

    CJK = re.compile(r"[一-鿿]")
    surfaced = _surface_inner_skill_name(tool_name, args)
    # Restore the previous value with the token: the ContextVar default is English now, and forcing
    # zh-CN in `finally` leaked a Chinese UI into later tests on the same xdist worker.
    token = set_current_ui_language("zh-CN")
    try:
        zh = _prettify_input_preview(surfaced, args)
        assert CJK.search(zh), f"中文界面下不该是英文：{zh!r}"

        set_current_ui_language("en-US")
        en = _prettify_input_preview(surfaced, args)
        assert not CJK.search(en), f"英文界面下漏了中文：{en!r}"
        assert en.strip(), "英文串不能是空的"
    finally:
        reset_current_ui_language(token)


@pytest.mark.parametrize(("tool_name", "args"), _ENTRIES)
def test_step_code_input_carries_the_source(tool_name: str, args: dict) -> None:
    """FE 的 Code 视图读 `step.codeInput`——拿不到源码这个视图就是空的。"""
    surfaced = _surface_inner_skill_name(tool_name, args)
    out = _extract_step_code_input(surfaced, args)
    assert out is not None, "code_input 为空 → FE Code 视图无内容"
    assert "import pandas" in out["code"]


@pytest.mark.parametrize(("tool_name", "args"), _ENTRIES)
def test_pycell_renders_real_python_not_a_truncated_repr(tool_name: str, args: dict) -> None:
    """pycell 的特判就是为了别把 Python 源码当普通 arg `repr()` 截断到 120 字。"""
    rendered = render_code(tool_name, args)
    assert rendered.startswith("# python_sandbox"), rendered[:80]
    assert "import pandas" in rendered
    assert "code=" not in rendered.splitlines()[0], "掉进通用分支了（源码会被 repr 截断）"


@pytest.mark.parametrize(("tool_name", "args"), _ENTRIES)
def test_code_block_section_is_emitted(tool_name: str, args: dict) -> None:
    """画布「代码」页签靠 code_block section；没有它 `codeAvailable` 恒 false。"""
    blocks = _map_code_block(_MI([_inv(tool_name, args)]))
    assert blocks, "没产出 code_block section → 画布代码页签为空"
    assert any("import pandas" in str(b.get("code", "")) for b in blocks)


def test_flat_entry_is_role_bound_like_its_sibling() -> None:
    """★ 安全面：`python_exec` 真跑任意 Python，role 绑定必须与 `skill_invoke` 一致。

    不在 `tool_aliases.yaml` 里给它 `allowed_roles`，codegen 会让它落到默认的宽 role 集
    （lead/analyst/composer）——于是 **Lead 能直接跑任意 Python**，而它的兄弟 `skill_invoke`
    是被拒的。这正是审计里反复出现的 G15 形状。
    """
    from bowhead_core.agents._generated.role_bindings import _PARADIGM_V2_ROLE_MAP as ROLE_BINDINGS

    assert ROLE_BINDINGS["python_exec"] == ROLE_BINDINGS["skill_invoke"], (
        f"python_exec 的 role 绑定与 skill_invoke 不一致：{ROLE_BINDINGS.get('python_exec')} vs {ROLE_BINDINGS['skill_invoke']}"
    )
    assert "lead" not in ROLE_BINDINGS["python_exec"], "Lead 不该能直接跑任意 Python"


# ── Four surfaces caught in review round 3 (the previous tests knew nothing about them) ──


@pytest.mark.parametrize(("tool_name", "args"), _ENTRIES)
def test_grant_targets_are_identical_for_both_entries(tool_name: str, args: dict) -> None:
    """★ P1 授权面：两条入口派生的授权目标必须**同集合**。

    扁平入口如果只派生 `tool:python_exec`，一个被拒了 `builtin_capability:python_sandbox`
    的团队换个入口就能继续跑任意 Python —— 与 role 绑定那个洞同源，只是漏在 grant 侧。
    """
    from bowhead_core.resource_access import resource_grant_targets_for_tool

    got = set(resource_grant_targets_for_tool(tool_name, args))
    assert ("skill", "python_sandbox", "invoke") in got, f"{tool_name} 没派生 skill:python_sandbox 执行授权目标"
    assert ("builtin_capability", "python_sandbox", "use") in got, f"{tool_name} 没派生 python_sandbox 能力目标"


@pytest.mark.parametrize(("tool_name", "args"), _ENTRIES)
def test_step_label_is_not_a_chunk_of_source_code(tool_name: str, args: dict) -> None:
    """步骤行的标签——不认扁平 args 就会掉到「第一个非空字符串字段」兜底上，
    于是**整段 Python 源码变成步骤标签**（聊天步骤行 / 报告横幅上一坨多行代码）。"""
    from bowhead_core.subagent import _prettify_input_preview

    label = _prettify_input_preview(_surface_inner_skill_name(tool_name, args), args)
    assert "\n" not in label, f"标签里有换行 = 塞进了源码：{label!r}"
    assert "import pandas" not in label, f"标签就是源码本身：{label!r}"
    assert label.strip(), "标签为空"


@pytest.mark.parametrize(("tool_name", "args"), _ENTRIES)
def test_auto_chart_candidate_is_collected(tool_name: str, args: dict) -> None:
    """ι 的自动建图兜底按 tool_name 挑候选，而 compute 正是拿到扁平入口的那个 profile。"""
    from bowhead_core.canvas_composers.auto_chart import collect_auto_chart_candidates

    # ⚠ These outputs are at the **result top level**, not inside result_json: auto_chart reads this level.
    # Both entry points are parameterized, so an incorrect fixture fails both sides, clearly distinguishing
    # a fixture mistake from a disconnected flat entry point.
    # The AND gate requires mime=text/csv, 1–2 numeric columns, and 3–200 rows; missing any condition yields no candidate.
    preview = {"columns": ["月份", "营业额"], "rows": [["1月", 1], ["2月", 2], ["3月", 3]]}
    result = {"ok": True, "outputs": [{"name": "out.csv", "artifact_id": "art_o", "mime": "text/csv", "preview": preview}]}
    inv = ToolInvocation(tool_name=tool_name, arguments=args, result=result, call_id="tl_c")
    assert collect_auto_chart_candidates([inv]), "自动建图候选为空 → ι 兜底对这条入口失效"


def test_flat_entry_risk_class_matches_the_sandbox() -> None:
    """自建 subagent 的工具选择器按风险分类渲染；不分类就是一行永久锁死的噪声。"""
    from bowhead_core.subagent_authoring_catalog import classify_native_risk

    assert classify_native_risk("python_exec") == classify_native_risk("python_sandbox")


# ── Step summaries must not dump result dictionaries (caught twice in 116 dogfood, 2026-09-09) ──


def test_report_tool_summaries_never_dump_the_result_dict() -> None:
    """`write_report` / `read_report` 的**步骤行摘要**不许是结果字典。

    ★★ 这条是我自己栽出来的：上一轮我把这两个补在**前端** tool-result-formatter 上
    （那是工具详情抽屉那条路），前端测试全绿，**真机上一个字没变**——步骤行读的是
    后端 `output_preview`。我刚把「同一份数据还有谁在渲」写成 chat-pipeline，
    转头就自己犯了。所以守卫必须钉在**后端这一层**，前端那份不能替它作证。

    突变：删掉 `_prettify_output_preview` 里的 write_report 分支 → 本条红。
    """
    from bowhead_core.agents._lead_context import reset_current_ui_language, set_current_ui_language
    from bowhead_core.subagent import _prettify_output_preview

    cases = [
        ("write_report", {"ok": True, "section_id": "sec_7b89dd0d3f8c", "section_ids": ["sec_7b89dd0d3f8c"], "seq": 4, "rev": 1, "owner": "compute"}),
        ("write_report", {"ok": True, "section_id": "sec_a", "appended": ["sec_b"], "updated": ["sec_a"], "chapters": 2}),
        ("read_report", {"ok": True, "outline": [{"section_id": "sec_x"}, {}], "figures": [{"id": "sec_f"}]}),
        ("read_report", {"ok": True, "outline": [], "sections": [], "figures": []}),
    ]
    token = set_current_ui_language("zh-CN")
    try:
        for locale in ("zh-CN", "en-US"):
            set_current_ui_language(locale)
            for tool, result in cases:
                out = _prettify_output_preview(tool, result, None)
                assert out and out.strip(), f"{tool}/{locale} 摘要为空"
                assert "{" not in out, f"{tool}/{locale} 把结果字典倒出来了：{out!r}"
                assert "sec_" not in out, f"{tool}/{locale} 漏了 section_id：{out!r}"
    finally:
        reset_current_ui_language(token)
