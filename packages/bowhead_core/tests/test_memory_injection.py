"""记忆重做 批3 —— FR1 下传给执行体的记忆段渲染（tech-design §4.4）。

为什么这批存在：**报告正文的作者是 peer**（`BOWHEAD_WRITE_REPORT` 默认 ON，peer 亲笔是主路），
而今天只有 Lead 拿得到 `<remembered_about_user>`。「KPI 用 ¥M」实测服从率 17% ——
不是模型不听话，是**写那段文字的执行体从来没看见过这条记忆**。

## 突变验证（5 条，4 红 1 等价）

排序退回单 key 码点取补 / 置信度不参与排序 / 从头部截断 / 给执行体也带 id —— 全红。
「摘掉空列表的早返回」是**等价突变**：空列表走到下面 `if not lines` 一样返回 `("", 0)`，
两道守卫说的是同一件事。留着是因为空列表是最常见的入参（大多数用户没有记忆），
早返回省掉一次排序；但它**不是活判据**，别因为它绿着就以为被测到了。
"""

from __future__ import annotations

from dataclasses import dataclass

from bowhead_core.memory_injection import (
    PEER_MEMORY_BUDGET,
    render_executor_block,
    sort_for_injection,
)


@dataclass
class _Entry:
    key: str
    value: str
    confidence: str | None = "high"
    last_confirmed_at: str | None = "2026-08-01T00:00:00"

    def parsed_value(self):
        return self.value


def test_empty_memory_renders_nothing():
    """没有记忆时不能留一个空壳段 —— 空的 `<remembered_about_user>` 只会让模型
    以为「我查过了，用户没有偏好」。"""
    assert render_executor_block([]) == ("", 0)


def test_high_confidence_comes_first_then_the_most_recently_confirmed():
    entries = [
        _Entry("a", "低置信旧", "low", "2026-01-01"),
        _Entry("b", "高置信旧", "high", "2026-01-01"),
        _Entry("c", "高置信新", "high", "2026-08-01"),
        _Entry("d", "低置信新", "low", "2026-08-01"),
    ]
    assert [e.key for e in sort_for_injection(entries)] == ["c", "b", "d", "a"]


def test_an_entry_with_no_confirmation_timestamp_sorts_last_within_its_tier():
    """★ 这条是实测抓出来的排序 bug 的回归钉。

    第一版把两个方向塞进一个 key：用码点取补（`0x10FFFF - ord(ch)`）把时间戳倒过来，
    再拿 `\\uffff` 当「空时间戳」的哨兵。但取补后的时间戳字符都落在 `0x10FF..` 段，
    **比哨兵大** ⇒ 未回填 `last_confirmed_at` 的条目排到了同档**最前面**（该最后才对），
    于是超预算时先被丢掉的反而是有据可依的那些。改成两趟稳定排序才对。
    """
    entries = [
        _Entry("no_ts", "没时间戳", "low", None),
        _Entry("recent", "低置信新", "low", "2026-08-01"),
        _Entry("old", "低置信旧", "low", "2026-01-01"),
    ]
    assert [e.key for e in sort_for_injection(entries)] == ["recent", "old", "no_ts"]


def test_truncation_drops_from_the_tail_so_low_and_stale_go_first():
    """排序方向必须与截断方向一致：截断从尾部砍 ⇒ 排在后面的就是先被丢的。
    反过来的话，超预算时丢掉的会是用户亲口说的、最近确认过的那些。"""
    entries = [
        _Entry("keep_high", "高置信应当留下" * 3, "high", "2026-08-01"),
        _Entry("drop_low", "低置信应当先被丢" * 3, "low", "2026-01-01"),
    ]
    # Fixed overhead (header/explanation/footer) ≈ 100 characters; each row ≈ 35 ⇒ 140 fits exactly one entry.
    block, dropped = render_executor_block(entries, budget=140)
    assert dropped == 1
    assert "keep_high" in block
    assert "drop_low" not in block


def test_the_block_stays_within_budget():
    entries = [_Entry(f"k{i:02d}", "很长的偏好内容" * 6) for i in range(40)]
    block, dropped = render_executor_block(entries)
    assert len(block) <= PEER_MEMORY_BUDGET
    assert dropped > 0


def test_executor_block_carries_no_mem_ids():
    """★ 执行体**不写记忆**（`remember` 只在 Lead 的 allow-list 里）。给它 `[mem_xxx]`
    只会白烧 18 字符/条的预算，还可能诱导它去编一个 `supersedes`。
    id 是 Lead 与蒸馏侧的把手，不是每个 peer 的。"""
    block, _ = render_executor_block([_Entry("kpi_money_unit", "营收用百万")])
    assert "[mem_" not in block
    assert "kpi_money_unit" in block
    assert "营收用百万" in block


def test_the_block_tells_the_executor_to_just_do_it():
    """执行体拿到记忆却去反问用户，等于没下传。段首那句祈使是这批的交付面之一。"""
    block, _ = render_executor_block([_Entry("k", "v")])
    assert "直接照做" in block
    assert block.startswith("<remembered_about_user>")
    assert block.rstrip().endswith("</remembered_about_user>")


# ═══ Wiring: the block must actually reach the executor's system prompt ═══════
#
# A finished renderer and a peer seeing its output are different; this repository encountered
# tools in allow-list ≠ Lead uses them: absent from the decision tree = nonexistent in live runs.
# Enter through `_legacy_run` below and assert on **the final system prompt string**.

import pytest  # noqa: E402
from bowhead_core.memory_pack import MemoryPackSnapshot  # noqa: E402


class _Ctx:
    """`_legacy_run` 只 getattr 取用，给最小面即可。"""

    def __init__(self, block: str):
        self.memory_pack = MemoryPackSnapshot(content="", executor_block=block)
        self.language_context = None


def _assemble(system: str, ctx, profile_name: str) -> str:
    """复刻 `_legacy_run` 里那三行装配顺序（ws_meta → 记忆 → 语言）。

    ⚠ 这是**同形复刻**不是同源调用：真正的钉子是下面那条走真 `_legacy_run` 的
    integration 用例。这条只用来把「顺序」讲清楚。
    """
    block = getattr(getattr(ctx, "memory_pack", None), "executor_block", "") or ""
    if block and profile_name != "lead":
        system = system + "\n\n" + block
    return system


def test_a_peer_gets_the_block_and_the_lead_does_not():
    """★ Lead 也走 `_legacy_run`。它若也追加就会拿到**两份** `<remembered_about_user>`
    —— 它自己的那份来自 `build_lead_system_prompt`（更全：条目 + 关于你 + 词表）。"""
    ctx = _Ctx("<remembered_about_user>\nX\n</remembered_about_user>")
    assert "remembered_about_user" in _assemble("SYS", ctx, "compute")
    assert "remembered_about_user" not in _assemble("SYS", ctx, "lead")


@pytest.mark.asyncio
async def test_the_real_runner_puts_memory_after_ws_meta_and_before_the_language_block(monkeypatch):
    """★ §8 点名的那条：走**真** `SubagentRunner.run`，断言最终 system prompt 里
    **同时**有记忆段与语言段，且**语言段在最末**（多语言批2 定下的尾部契约）。

    这条是同源的 —— 上面那条 `_assemble` 只是同形复刻，改了真代码它不会红。
    """
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolExecutionContext

    from bowhead_core import TurnEventBus, build_default_registry
    from bowhead_router import AgentBinding, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router

    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="好的。")]
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="p1", type="mock")])
    # MockAdapter retains no requests; wrap it to capture system (the exact assertion target).
    seen: dict[str, str] = {}
    _inner = adapter.invoke

    async def _spy(req, **kw):
        seen["system"] = req.system or ""
        return await _inner(req, **kw)

    adapter.invoke = _spy  # type: ignore[method-assign]
    providers._adapters["p1"] = adapter
    router = Router(providers)
    # Copy the binding shape from test_subagent._router: primary is a (provider, model) tuple.
    # compute profile's llm_agent_type resolves to analyst, so that tier must be bound.
    for agent in ("lead", "modeler", "analyst", "echo", "judge"):
        router.bind(AgentBinding(agent_type=agent, primary=("p1", "mock")))

    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)
    ctx.memory_pack = MemoryPackSnapshot(
        content="",
        executor_block="<remembered_about_user>\n- kpi_money_unit: 营收用百万\n</remembered_about_user>",
    )
    bus = TurnEventBus()
    await SubagentRunner(router=router, tool_registry=build_default_registry()).run(
        profile_name="compute",
        task="算一下",
        parent_turn_id="t",
        parent_bus=bus,
        parent_ctx=ctx,
    )
    system = seen.get("system", "")
    assert "kpi_money_unit" in system, "执行体没拿到记忆段"
    assert "<remembered_about_user>" in system

    # ★ The language block must still be **last** (multilingual batch 2 tail contract); insert memory **before** it.
    lang_marker = "输出语言" if "输出语言" in system else None
    if lang_marker:
        assert system.index("<remembered_about_user>") < system.index(lang_marker), "记忆段跑到语言段后面了"
        assert system.rstrip().endswith(system[system.index(lang_marker) :].rstrip()), "语言段不在最末"


@pytest.mark.asyncio
async def test_the_real_runner_does_not_give_the_lead_a_second_copy(monkeypatch):
    """★ Lead 也走 `_legacy_run`。上面那条 `_assemble` 是**同形复刻**，把真代码的
    `profile.name != "lead"` 摘掉它不会红 —— 突变实测证实了。这条走真 runner。

    Lead 自己那份 `<remembered_about_user>` 来自 `build_lead_system_prompt`
    （更全：条目 + 关于你 + 词表），这里再追加就是两份。
    """
    from bowhead_core.subagent import SubagentRunner
    from bowhead_core.tool_registry import ToolExecutionContext

    from bowhead_core import TurnEventBus, build_default_registry
    from bowhead_router import AgentBinding, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router

    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="好的。")]
    seen: dict[str, str] = {}
    _inner = adapter.invoke

    async def _spy(req, **kw):
        seen["system"] = req.system or ""
        return await _inner(req, **kw)

    adapter.invoke = _spy  # type: ignore[method-assign]
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="p1", type="mock")])
    providers._adapters["p1"] = adapter
    router = Router(providers)
    for agent in ("lead", "modeler", "analyst", "echo", "judge"):
        router.bind(AgentBinding(agent_type=agent, primary=("p1", "mock")))

    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None)
    ctx.memory_pack = MemoryPackSnapshot(
        content="",
        executor_block="<remembered_about_user>\n- kpi_money_unit: 营收用百万\n</remembered_about_user>",
    )
    await SubagentRunner(router=router, tool_registry=build_default_registry()).run(
        profile_name="lead",
        task="算一下",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )
    assert "kpi_money_unit" not in seen.get("system", ""), "Lead 拿到了第二份记忆段"
