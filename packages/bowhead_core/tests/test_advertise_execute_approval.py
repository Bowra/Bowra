"""广告面 vs 执行面：需要审批但今天执行不了的工具不该出现在模型面前。

设计见 `docs/sprints/hitl-audit-2026-09-04/tech-design-batch4.md` §1。

要点不是「把工具删掉」，是「按当前可用性现算」——所以这里除了断言「现在不广告」，
还必须断言**上线可行**（纳入支持集合 ⇒ 它回来）与**下线可行**（翻开关 ⇒ 它消失）。
只断言「不在名单里」是不够的：那可能只是因为那个工具压根没注册。
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.capability_resolver import advertised_tool_schemas
from bowhead_core.tool_registry import ToolRegistry, ToolSpec
from bowhead_router.types import ToolSchema

from bowhead_core import approval_gate

WRITE_TOOL = "teammcp.src1.create_issue"
READ_TOOL = "teammcp.src1.search_issues"


def _spec(name: str, *, needs_approval: bool) -> ToolSpec:
    async def _handler(_args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
        return {"ok": True}

    return ToolSpec(
        schema=ToolSchema(name=name, description="d", input_schema={"type": "object"}),
        handler=_handler,
        needs_approval=needs_approval,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_spec(READ_TOOL, needs_approval=False))
    reg.register(_spec(WRITE_TOOL, needs_approval=True))
    return reg


def _advertised(registry: ToolRegistry) -> set[str]:
    """LLM 广告面（`subagent.py` 的调用点传 `drop_unapprovable=True`）。"""
    return {
        s.name
        for s in advertised_tool_schemas(
            registry,
            (READ_TOOL, WRITE_TOOL),
            profile_name="analyst",
            role=None,
            drop_unapprovable=True,
        )
    }


def _capability_refs(registry: ToolRegistry) -> set[str]:
    """能力读模型面（`CapabilityResolver` 的调用点**不传**）——缺省即此语义。"""
    return {
        s.name
        for s in advertised_tool_schemas(
            registry,
            (READ_TOOL, WRITE_TOOL),
            profile_name="analyst",
            role=None,
        )
    }


# ═══════════ Surface 1: model-visible tool schema ═══════════


def test_write_tool_not_advertised_when_executor_cannot_run_it(registry, monkeypatch):
    """今天的状态：审批执行器接不住 ⇒ 需审批的工具不进广告面；只读的照常在。"""
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset())
    names = _advertised(registry)
    assert READ_TOOL in names
    assert WRITE_TOOL not in names


def test_write_tool_comes_back_when_executor_supports_it(registry, monkeypatch):
    """★ 上线可行性证明。

    「不广告」是当前可用性的投影，不是删除。工具一旦被纳入支持面，**下一次求值
    就回到模型面前，广告面代码零改动**。这条测试就是那句承诺的凭据；
    没有它，上面那条只能证明「它现在不在」，证明不了「它还能回来」。
    """
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset({WRITE_TOOL}))
    assert WRITE_TOOL in _advertised(registry)


def test_kill_switch_hides_even_supported_tools(registry, monkeypatch):
    """★ 下线可行性证明：开关关掉 ⇒ 即便在支持集合里也不广告。"""
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset({WRITE_TOOL}))
    monkeypatch.setenv("BOWHEAD_APPROVAL_WRITE_ENABLED", "0")
    assert WRITE_TOOL not in _advertised(registry)


def test_flag_is_read_per_call_not_at_import(monkeypatch):
    """开关不在 import 期定型——同进程内改了 env 立刻生效。"""
    monkeypatch.setenv("BOWHEAD_APPROVAL_WRITE_ENABLED", "0")
    assert approval_gate.approval_write_enabled() is False
    monkeypatch.setenv("BOWHEAD_APPROVAL_WRITE_ENABLED", "1")
    assert approval_gate.approval_write_enabled() is True


def test_capability_refs_are_not_filtered(registry, monkeypatch):
    """★ 剔除只作用于 LLM 广告面，**不能**动能力读模型。

    `advertised_tool_schemas` 有两个语义不同的调用方：喂 LLM 的那个要剔，
    算 capability refs / `capability_fingerprint` 的那个不能剔——那是「这个 agent
    拥有什么能力」，剔了会改指纹，而指纹参与 agent revision/release 的一致性校验。

    设计初稿论证「判据不依赖 ctx ⇒ 函数内部无条件判」，那个论证是错的；
    既有的 `test_resolver_does_not_treat_risk_level_as_write_semantics` 逮到了它。
    这条把不变量正面钉住，免得下次又被合并回去。
    """
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset())
    assert WRITE_TOOL not in _advertised(registry)  # Exclude from advertising.
    assert WRITE_TOOL in _capability_refs(registry)  # Retain in the read model.


# ═══════════ Surface 1 and the gate: one criterion ═══════════


@pytest.mark.asyncio
async def test_gate_and_advertising_share_one_predicate(registry, monkeypatch):
    """闸与广告面必须同源：改支持集合，两边同步变。

    两份判据 = Lead 会点名一个必被拒的工具（本 sprint 要消除的形状本身）。
    """
    ctx = SimpleNamespace(turn_id="t1", session_id="s1", workspace_id="ws", member_uid="", user_id="u1")
    gate = approval_gate.ApprovalGate(approval_store=None)  # type: ignore[arg-type]

    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset())
    blocked = await gate.maybe_gate(registry=registry, tool_name=WRITE_TOOL, arguments={}, ctx=ctx)
    assert blocked is not None
    assert blocked.error == "approval_unsupported"
    assert WRITE_TOOL not in _advertised(registry)  # Both sides reject.

    # One change allows both sides (the gate reaches enqueue; store=None raises, proving it passed the criterion).
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset({WRITE_TOOL}))
    assert WRITE_TOOL in _advertised(registry)
    with pytest.raises(AttributeError):
        await gate.maybe_gate(registry=registry, tool_name=WRITE_TOOL, arguments={}, ctx=ctx)


@pytest.mark.asyncio
async def test_gate_honours_kill_switch_not_just_the_name_set(registry, monkeypatch):
    """★ 判别器：**集合非空 + 开关关掉**——只有这组取值能把两版判据分开。

    上面那条「同源」测试其实判别不了：集合空/非空时，`approval_executable` 与
    「`tool_name not in SUPPORTED_NATIVE_WRITE_TOOLS`」给出**同值**，把闸改回硬编码
    也不会红（实测突变不变红才发现）。同值的判别器等于没有判别器 —— 必须找一组
    两版异值的输入。这里就是那一组：硬编码版会放行去入队，共用判据版必须拒。
    """
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset({WRITE_TOOL}))
    monkeypatch.setenv("BOWHEAD_APPROVAL_WRITE_ENABLED", "0")
    ctx = SimpleNamespace(turn_id="t1", session_id="s1", workspace_id="ws", member_uid="", user_id="u1")
    gate = approval_gate.ApprovalGate(approval_store=None)  # type: ignore[arg-type]

    inv = await gate.maybe_gate(registry=registry, tool_name=WRITE_TOOL, arguments={}, ctx=ctx)
    assert inv is not None, "开关关掉时闸必须拒；放行说明它读的不是共用判据"
    assert inv.error == "approval_unsupported"
    assert WRITE_TOOL not in _advertised(registry)  # Advertising surface stays synchronized.


@pytest.mark.asyncio
async def test_refusal_message_states_configuration_not_transient(registry, monkeypatch):
    """给模型的话必须说清是配置态、禁重试；旧文案的「yet」暗示等等就有。"""
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset())
    ctx = SimpleNamespace(turn_id="t1", session_id="s1", workspace_id="ws", member_uid="", user_id="u1")
    gate = approval_gate.ApprovalGate(approval_store=None)  # type: ignore[arg-type]
    inv = await gate.maybe_gate(registry=registry, tool_name=WRITE_TOOL, arguments={}, ctx=ctx)
    assert inv is not None
    msg = str(inv.result["message"])
    assert "do not retry" in msg
    assert "configuration state" in msg
    assert "yet" not in msg.lower()


# ═══════════ Surface 2: <team_tools> prompt block ═══════════


def _entries(registry: ToolRegistry, overlay: tuple[str, ...]) -> Any:
    from bowhead_core.agents.lead_orchestrator import _gather_team_tool_entries

    view = SimpleNamespace(overlay_names=overlay, get=registry.get)
    return _gather_team_tool_entries(view)


def test_team_tools_prompt_block_drops_unexecutable_write_tool(registry, monkeypatch):
    """第二个广告面：只过滤 schema 不过滤提示词，等于把差挪进散文。"""
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset())
    names = [e.name for e in _entries(registry, (READ_TOOL, WRITE_TOOL)) or []]
    assert names == [READ_TOOL]


def test_team_tools_prompt_block_returns_none_when_all_dropped(registry, monkeypatch):
    """全被过滤 ⇒ 必须返 None 而不是 []。

    caller 用 None 判「省略整段」；返 [] 会渲一个空的 <team_tools> 块。
    """
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset())
    assert _entries(registry, (WRITE_TOOL,)) is None


def test_team_tools_prompt_block_restores_when_supported(registry, monkeypatch):
    """★ 提示词块的上线可行性证明（与面 1 对称）。"""
    monkeypatch.setattr(approval_gate, "SUPPORTED_NATIVE_WRITE_TOOLS", frozenset({WRITE_TOOL}))
    names = [e.name for e in _entries(registry, (READ_TOOL, WRITE_TOOL)) or []]
    # The function sorts the overlay, so assert the set rather than order.
    assert set(names) == {READ_TOOL, WRITE_TOOL}


# ═══════════ Wiring guards ═══════════


def test_llm_advertising_call_site_passes_drop_unapprovable() -> None:
    """★ 接线守卫：`subagent.py` 那个**喂 LLM** 的调用点必须传 `drop_unapprovable=True`。

    为什么需要它：上面所有测试都是直接调 `advertised_tool_schemas` 并自己传参——
    测的是**函数**，不是**接线**。实测把调用点的 `drop_unapprovable=True` 删掉，
    整个测试文件一条都不红（突变不红才发现）。函数对了而调用点没接，等于没做。

    为什么用源码结构断言而不是驱动 `_legacy_run`：那条路要真造 runner/profile/ctx，
    成本远超收益，而这里要守的恰好就是「那一行参数还在不在」。同仓已有源码级守卫
    先例（`lib/random-id.source.test.ts`）。用 AST 而非字符串 grep，格式化不会误伤。
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "bowhead_core" / "subagent.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "advertised_tool_schemas"]
    assert calls, "subagent.py 不再调 advertised_tool_schemas？广告面接线已变，请重看本测试"
    for call in calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        assert "drop_unapprovable" in kwargs, "LLM 广告面调用点漏了 drop_unapprovable —— 剔除不会生效"
        value = kwargs["drop_unapprovable"]
        assert isinstance(value, ast.Constant) and value.value is True, "drop_unapprovable 必须恒为 True（广告面语义）"
