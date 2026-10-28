"""credential-readiness D1 + 批 2-a §3.2 —— 装配 Router 的契约.

**D1 原判据（保留）**：装配 Router 只是「缺少团队上下文时的内部装配占位」，被真正
调用即拒绝并可归因；两种变体绑**同一组** agent_type（V7：少绑一个，失败形态就退化
成裸 KeyError，归因丢失）。prod 态不走全 lifespan（会连环触发 auth/Fernet/sandbox
三道 prod 闸），直接单测装配函数。

**批 2-a 新增两条**：

1. **选型与 `BOWHEAD_ENV` 解耦，默认 refusal**。此前 `env != "dev"` 才 refusal；
   116 实测 `BOWHEAD_ENV=dev`（箱 compose 是独立 fork，那行 `prod` 属于
   sandbox-executor 服务），于是「prod 装 RefusalAdapter」这个所有人都以为成立的
   前提在真机上不成立——所有旁路静默吃 echo，refusal 指标恒 0。安全语义不该挂在
   一个各箱各写的变量上。

2. **catch-all binding**。基类 `_resolve_key` 对未绑 agent_type 抛裸 `KeyError`，
   发生在 adapter 之前 ⇒ `record_refusal()` 从未触发（站点 7 的 `home_proposal`
   就是这个形状）。catch-all 之后未绑的 agent_type 也落到装配 adapter，refusal
   得到记账。

   ★ 拒绝**没有**上移到 `resolve()`：`Router.invoke`/`stream` 内部都调
   `self.resolve(...)`，在那里抛会绕过 fallback 机器，异常从
   `InvocationTargetsExhaustedError` 变成裸 `TeamModelUnconfiguredError`，
   `project_llm_failure` 不认 → chat 的 reason_code 退化成 `lead_internal_error`。
   下面 `test_refusal_keeps_invocation_exhausted_envelope` 守的就是这条。
"""

from __future__ import annotations

import pytest
from bowhead_gateway.main import _build_assembly_router
from bowhead_router.types import LLMRequest, Message

from bowhead_router import InvocationTargetsExhaustedError, MockAdapter, RefusalAdapter, TeamModelUnconfiguredError

_EXPECTED_AGENT_TYPES = ("lead", "modeler", "analyst", "echo", "judge")


@pytest.mark.parametrize("env", ["dev", "prod", "staging", ""])
def test_default_is_refusal_regardless_of_env(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    """不显式声明 mock ⇒ 一律 refusal，**与 BOWHEAD_ENV 无关**。"""
    monkeypatch.delenv("BOWHEAD_ASSEMBLY_ROUTER", raising=False)
    monkeypatch.setenv("BOWHEAD_ENV", env)
    _router, registry = _build_assembly_router()
    assert isinstance(registry.get("llm_refusal"), RefusalAdapter), f"env={env!r} 不应影响选型"


def test_explicit_mock_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """离线开发显式声明才给 echo。"""
    monkeypatch.setenv("BOWHEAD_ASSEMBLY_ROUTER", "mock")
    monkeypatch.setenv("BOWHEAD_ENV", "prod")  # Deliberately opposite env, proving env is irrelevant.
    router, registry = _build_assembly_router()
    assert isinstance(registry.get("llm_mock"), MockAdapter)
    for agent_type in _EXPECTED_AGENT_TYPES:
        assert router.resolve(agent_type).primary == ("llm_mock", "mock-model")


@pytest.mark.parametrize("variant", ["refusal", "mock"])
def test_both_variants_bind_the_same_agent_types(monkeypatch: pytest.MonkeyPatch, variant: str) -> None:
    """V7：两种变体绑同一组——少绑一个，失败形态就退化成裸 KeyError，归因丢失。"""
    if variant == "mock":
        monkeypatch.setenv("BOWHEAD_ASSEMBLY_ROUTER", "mock")
    else:
        monkeypatch.delenv("BOWHEAD_ASSEMBLY_ROUTER", raising=False)
    provider = "llm_mock" if variant == "mock" else "llm_refusal"
    router, _registry = _build_assembly_router()
    for agent_type in _EXPECTED_AGENT_TYPES:
        assert router.resolve(agent_type).primary == (provider, "mock-model")


def test_unbound_agent_type_resolves_via_catch_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """批 2-a：未绑的 agent_type 不再抛裸 KeyError，而是落到装配 binding。

    否则拒绝发生在 adapter 之前，`record_refusal()` 永远不触发——`home_proposal`
    正是这个形状，这就是 refusal 指标「零值有两种含义」的第一个来源。
    """
    monkeypatch.delenv("BOWHEAD_ASSEMBLY_ROUTER", raising=False)
    router, _registry = _build_assembly_router()
    # Actual unbound agent_type used by home_proposals.py.
    binding = router.resolve("home_proposal")
    assert binding.primary == ("llm_refusal", "mock-model")


async def test_refusal_keeps_invocation_exhausted_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 拒绝必须仍以 `InvocationTargetsExhaustedError` 形态出栈。

    这是 chat 侧 LLM 失败投影的入口条件：`project_llm_failure` 只认
    `RouteTargetsExhaustedError` 家族；换成裸 `TeamModelUnconfiguredError` 会让
    reason_code 从 `llm_provider_unavailable` 退化成 `lead_internal_error`，并丢掉
    provider/kind/retryable/occurrence_id 全套结构化归因。
    """
    monkeypatch.delenv("BOWHEAD_ASSEMBLY_ROUTER", raising=False)
    router, _ = _build_assembly_router()
    with pytest.raises(InvocationTargetsExhaustedError) as ei:
        await router.invoke("echo", LLMRequest(model="", messages=[Message(role="user", content="hi")]))
    assert isinstance(ei.value.__cause__, TeamModelUnconfiguredError)


async def test_unbound_agent_type_also_refuses_through_the_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """catch-all 的收益落点：未绑 agent_type 现在也走到 adapter → 会被记账。"""
    monkeypatch.delenv("BOWHEAD_ASSEMBLY_ROUTER", raising=False)
    router, _ = _build_assembly_router()
    with pytest.raises(InvocationTargetsExhaustedError) as ei:
        await router.invoke("home_proposal", LLMRequest(model="", messages=[Message(role="user", content="hi")]))
    assert isinstance(ei.value.__cause__, TeamModelUnconfiguredError)
