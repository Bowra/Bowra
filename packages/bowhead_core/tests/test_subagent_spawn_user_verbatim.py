"""用户原话下传 —— sprint spawn-user-verbatim-2026-08。

`task=` 是 Lead 的转述，用户原话此前**不下传**给 peer（代码里管这叫「盲道」，
delivery / language 是它先后打的两个补丁）。第三次发作是**实体词被改写**：
116 实测用户问「品牌蔬菜2026年的营业额」，Lead 派活写成「品牌维度下「蔬菜」这个品牌」，
而 `品牌蔬菜` 是 d56(种类) 下真实存在的值 —— 原词没活到 resolver 面前。

与 language 同款：从 ctx **确定性注入**，不新增 Lead 参数、不依赖它的服从性。
"""

from __future__ import annotations

from bowhead_core.subagent import (
    SubagentResult,
    register_subagent_tool,
    render_user_verbatim_line,
)
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry


class _CapturingRunner:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def run(self, **kwargs):
        self.kwargs = kwargs
        return SubagentResult(
            profile=str(kwargs.get("profile_name") or "compute"),
            subagent_id="sub_test",
            ok=True,
            final_answer="42",
        )


class _FakeBus:
    async def emit(self, ev) -> None:  # noqa: ARG002
        return None


async def _spawn(user_text: str, args: dict | None = None) -> str:
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _CapturingRunner()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="m",
        user_id="u",
        turn_id="parent_t",
        datarc=None,
        subagent_runner=runner,
        parent_bus=_FakeBus(),
        user_text=user_text,
    )
    inv = await registry.execute(
        "subagent_spawn",
        {"profile": "compute", "task": "查询品牌维度下「蔬菜」这个品牌的营业额", **(args or {})},
        ctx,
    )
    assert inv.error is None, inv.error
    assert runner.kwargs, f"runner.run 未被调用: {inv.result}"
    return str(runner.kwargs.get("extra_instructions") or "")


# ── Positive cases ─────────────────────────────────────────────────────────────────


async def test_user_verbatim_reaches_the_peer() -> None:
    """★ 复现用例：Lead 把「品牌蔬菜」拆成了「品牌 + 蔬菜」，原词必须仍到得了 peer。"""
    extra = await _spawn("品牌蔬菜2026年的营业额")
    assert "user_verbatim" in extra
    assert "品牌蔬菜" in extra, "原词没下传 = 这次改动白做"


async def test_verbatim_line_precedes_leads_paraphrase() -> None:
    """原话是校对转述的基准，必须排在 rationale/data_hint 这些转述之前。"""
    extra = await _spawn("品牌蔬菜2026年的营业额", {"rationale": "用户要单一品类的数"})
    assert extra.index("user_verbatim") < extra.index("rationale")


async def test_scope_is_pinned_to_entities_not_the_whole_question() -> None:
    """多 peer 计划里每个 peer 都看得到同一份原话——用途不钉死会各自去答整题
    （这多半正是当初不下传的原因）。"""
    extra = await _spawn("品牌蔬菜2026年的营业额")
    assert "实体词" in extra
    assert "任务范围仍以上面的 task 为准" in extra


# ── Negative cases / boundaries ──────────────────────────────────────────────────────────


async def test_empty_user_text_injects_nothing() -> None:
    """★ 空值必须完全 no-op：没有原话时行为与改动前逐字一致。"""
    extra = await _spawn("")
    assert "user_verbatim" not in extra
    assert extra == "", f"空 user_text 不该产生任何 Sub-task context: {extra!r}"


async def test_long_text_reaches_the_peer_whole() -> None:
    """★ 长问题**整段**下传，不截断（上下文工程 sprint 批 1-d）。

    原先 300 字上限砍掉的正是长问题的后半段，而限定条件（「……，只看华东区」）通常就在那里。
    用户原话是全系统信息密度最高、体积最小的一段上下文；1M 窗口下为它设上限没有收益。
    """
    tail = "并且只看华东区、只算线上渠道"
    long_q = "品牌蔬菜" + "很长的补充说明" * 200 + tail
    extra = await _spawn(long_q)
    assert "品牌蔬菜" in extra, "开头的实体词丢了"
    assert tail in extra, "结尾的限定条件被截掉了——那正是判据本身"
    assert "已截断" not in extra


async def test_coexists_with_delivery_and_language_lines() -> None:
    """与既有 ctx 行共存，互不吞并。"""
    extra = await _spawn("品牌蔬菜的营业额，用英文回答", {"delivery": "chat", "language": "en-US"})
    assert "user_verbatim" in extra
    assert "delivery" in extra
    assert "language" in extra


# ── Pure functions ───────────────────────────────────────────────────────────────


def test_render_helper_is_pure_and_normalizes_whitespace() -> None:
    assert render_user_verbatim_line("") == ""
    assert render_user_verbatim_line("   ") == ""
    line = render_user_verbatim_line("  品牌蔬菜\n\n 2026 年  营业额 ")
    assert "品牌蔬菜 2026 年 营业额" in line


def test_render_helper_does_not_cap() -> None:
    """突变锚点：把 `text[:300]` 加回去 ⇒ 本条红。"""
    n = 5000
    line = render_user_verbatim_line("蔬" * n)
    assert line.count("蔬") == n
    assert "已截断" not in line


# ── The relaying agent does not choose calculation methods (116 t_45403a34f0c1 / t_e9b0fc0f90ef) ──────────
# User asked only for the three fastest-growing categories; Lead added first-vs-last-month or monthly compound-growth methods,
# which compute treated as user requirements, mistaking seasonality for trends and producing all-negative category growth in both runs.


async def test_peer_is_told_the_method_in_task_is_not_the_users() -> None:
    extra = await _spawn("按月拆开看 2026 年各品类大类的营业额，并指出增长最快的三个品类")
    assert "计算方法" in extra and "不是用户要求" in extra


def test_spawn_schema_tells_lead_not_to_pick_the_method() -> None:
    registry = build_default_registry()
    register_subagent_tool(registry)
    spec = registry.get("subagent_spawn")
    assert spec is not None
    desc = spec.schema.input_schema["properties"]["task"]["description"]
    assert "计算方法也别替用户选" in desc
