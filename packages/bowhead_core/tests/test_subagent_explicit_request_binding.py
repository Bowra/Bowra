"""peer 侧「用户点名的东西是硬约束」—— **对 live prompt** 断言。

116 prod 三次少交付都是同一个形状：**用户显式点名的交付形态/列，在整条链路上连输入都不是**，
被启发式静默覆盖。

  ① 「用表格列出营业额/退款额/退款率」→ 0 个 table，换成两张各画一半的图
     （`cv_0f29e38411ec`：三张 chart 全是 `auto_charted:false`，数据分两个 artifact 取）。
  ② 「每月同比变化率」→ artifact `art_a7f1e6dd1cae` **确实有 `营业额同比变化率` 这一列**，
     图却只画了两条绝对值（`cv_51ac5cfb5a5a`）。
  ③ 1.1.10 复跑 ①：走了 **chat 短答**，`add_chart` 一次没调、canvas 上只有两个被
     `canvas.sections_hidden` 隐藏的 code_block，答复却称「20 行明细已输出，可直接查看」。

★★ **本文件为什么必须 boot 之后再断言**：上一版把断言打在 `subagent.py` 的源码字符串上，
六条全绿——而 `subagent.py:_build_compute_system_prompt` 只是**未启动时的占位**，
`register_all_system_manifests` 在 boot 时用 `prompts/system/compute.md` 同名覆盖了它。
于是「规则写好了」与「模型读得到」是两回事，绿闸掩护了一次**完全没送达**的修复
（实测：boot 前 len=18445 含全部标记，boot 后 len=13265 一个标记都不含）。
⇒ 断言必须穿过 boot，取 `PROFILES["compute"].system_prompt`。

⚠ 即便如此，**prompt 里有这段字 ≠ 模型会照做**——真实效果只有真 LLM dogfood 算数。
本文件的作用是保证规则**送得到**、且不被后来人顺手精简掉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "packages" / "bowhead_core" / "src" / "bowhead_core"
CANVAS_TOOLS_PY = SRC / "canvas_tools.py"
COMPUTE_MD = SRC / "agents" / "prompts" / "system" / "compute.md"
SUBAGENT_PY = SRC / "subagent.py"

# Batch 3 (2026-08-20) layered rewrite: this rule moved from its own H2 section to rule 3 under ① 五条不可违反.
# Anchor on the rule's exact text rather than the heading: layout changes must not fail the guard; removal of the rule must.
_BINDING_HEADER = "**用户点名的，逐字照做**"
# Batch 5ⓐ delivery-surface autonomy renamed the sizing table 交付面你定; follow the anchor, preserving semantics (hard form constraints precede delivery decisions).
_SIZING_HEADER = "交付面你定"


@pytest.fixture(scope="module")
def live_compute_prompt() -> str:
    """走生产 boot 路径（`bootstrap_multi_agent` 里的那两句）拿 compute peer 的真 prompt。"""
    from bowhead_core.agents.agent_runner_adapter import register_all_system_manifests
    from bowhead_core.agents.registry import AgentRegistry
    from bowhead_core.subagent import PROFILES

    register_all_system_manifests(AgentRegistry.bootstrap())
    from bowhead_core.agents.prompts.simple_lookup_locale import render_lookup_template

    return render_lookup_template(PROFILES["compute"].system_prompt)


def test_live_prompt_comes_from_compute_md_not_the_placeholder(live_compute_prompt: str) -> None:
    """先钉住「哪份是 live」——这条一旦变了，下面所有断言的对象就换人了。"""
    marker = "Live runtime prompt"
    assert marker in live_compute_prompt, (
        "boot 后的 compute prompt 不再来自 prompts/system/compute.md —— 本文件其余断言的对象已经换了，先确认新的 live 来源再改断言"
    )
    assert marker in COMPUTE_MD.read_text(encoding="utf-8")


def test_binding_rule_is_in_the_live_prompt_and_outranks_the_sizing_shortcut(live_compute_prompt: str) -> None:
    """硬约束块必须在 live prompt 里，且排在交付面捷径**之前**——先后即优先级。"""
    assert _BINDING_HEADER in live_compute_prompt, "「用户点名的东西是硬约束」没进 live prompt"
    assert _SIZING_HEADER in live_compute_prompt, "sizing 块没了（本测试的锚点失效）"
    assert live_compute_prompt.index(_BINDING_HEADER) < live_compute_prompt.index(_SIZING_HEADER), (
        "硬约束块排到了 sizing 捷径后面——模型会先读到「简单 query 走 chat」再读到它"
    )


def test_binding_rule_covers_surface_shape_columns_and_no_verbal_delivery(live_compute_prompt: str) -> None:
    """三次事故各占一条，缺一条就漏掉对应的那种失败。"""
    start = live_compute_prompt.index(_BINDING_HEADER)
    block = live_compute_prompt[start : start + 1600]
    assert "点名了形态就等于点名了交付面" in block, "交付面没绑住 → 会被 chat 短答吃掉（事故 ③）"
    assert 'force_kind="table"' in block, "点名要表格时该走哪条路没写（事故 ①）"
    assert "series" in block, "点名了字段要写进 series 没写（事故 ②）"
    assert "禁止口头交付" in block, "没禁「声称有产物却没调 canvas_add_*」（事故 ③ 的第二半）"
    assert "caveats" in block, "交付不了必须明说，否则退回静默少交付"


def test_delivery_surface_trigger_list_includes_table(live_compute_prompt: str) -> None:
    """触发词只写「图/报告」时，「用表格列出…」不命中 ⇒ 直接降级 chat（事故 ③ 的机制）。"""
    assert "**表格**" in live_compute_prompt, "交付面触发词没把「表格」列进去"
    assert "用户明确要图/报告，或任务需要" not in live_compute_prompt, "旧的窄触发词还在"


def test_return_selfcheck_includes_delivery_vs_request(live_compute_prompt: str) -> None:
    """收尾自检必须包含「交付 vs 请求」对照，且**长在硬约束块里**。

    批 3 前这里钉的是 `**10 铁律**` 这个标题——它只是「自检项存在」的代理，铁律列表一旦
    改版就假红，而把自检那一行删掉、标题留着又会假绿。现在直接钉那一行本身，并要求它
    与硬约束块同处一段（自检是这条规则的执行时点，拆散了就没人在 return 前想起它）。
    """
    assert "对着用户 query 点一遍名" in live_compute_prompt
    start = live_compute_prompt.index(_BINDING_HEADER)
    assert "对着用户 query 点一遍名" in live_compute_prompt[start : start + 1600], "自检那一行离开了硬约束块"


def test_fallback_reads_the_same_compute_md_as_live() -> None:
    """批 2：兜底不再是手抄副本，而是**读同一个 compute.md**——漂移在结构上不可能。

    这里钉「同源」而不是逐条 marker：marker 检查只能发现「我记得抄的那几句」漂了，
    同源检查连没想到的段落一起保住。（负对照：`_COMPUTE_BODY_REST` 之类的副本常量
    不许回来——它一回来，人肉同步的税就回来了。）
    """
    from bowhead_core.subagent import _build_compute_system_prompt

    live = COMPUTE_MD.read_text(encoding="utf-8")
    fallback = _build_compute_system_prompt()
    assert live in fallback, "兜底 prompt 不再包含 compute.md 全文——同源被破坏"
    for marker in (_BINDING_HEADER, "点名了形态就等于点名了交付面", "禁止口头交付"):
        assert marker in fallback, f"缺 {marker!r}"

    # Negative control: duplicated constants must not return as assignments (comments may mention historical names).
    src = SUBAGENT_PY.read_text(encoding="utf-8")
    for banned in ("_COMPUTE_BODY_REST = ", "_COMPUTE_BOOT = "):
        assert banned not in src, f"手抄副本常量 {banned.strip(' =')} 回来了——批 2 退役的正是它"


def test_table_entry_points_are_not_discouraged_by_their_own_schema() -> None:
    """Explicit Table requests use force_kind; raw alone leaves inference active."""
    src = CANVAS_TOOLS_PY.read_text(encoding="utf-8")
    assert "raw=无明显意图" not in src, "`raw` 又被写回「无明显意图」——用户意图明确时模型不会选它"
    assert "For an explicitly requested Table, use " in src
    assert "force_kind=table" in src
    assert "绝大部分场景别填，让推断引擎决定" not in src, "`force_kind` 的劝退句回来了"
    force_kind_block = src[src.index('"force_kind"') : src.index('"column_types"')]
    assert "用户点名了形态就填" in force_kind_block


def test_series_description_warns_that_unlisted_columns_vanish() -> None:
    """series 漏列 = 用户点名的指标静默消失（事故 ② 的直接机制）。"""
    src = CANVAS_TOOLS_PY.read_text(encoding="utf-8")
    block = src[src.index('"series"') : src.index('"series"') + 1400]
    assert "用户点名的指标列必须写进来" in block
    assert "同比变化率" in block, "真机例子被删了——这条规则会退化成一句没人当真的泛泛提醒"
