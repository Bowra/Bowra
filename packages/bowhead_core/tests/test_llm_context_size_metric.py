"""上下文体积直方图 —— 上下文工程 sprint 批 0-c。

它取代 `LEAD_PROMPT_TOKEN_CAP`（批 1-b 删）的全部正当用途：「prompt 又长了多少」
从一个每隔几个月手工上调、且生产路径显式 `enforce_token_budget=False` 不校验的常量，
变成一条可查曲线。

★ 本文件最要紧的一条是 `test_metric_survives_shape_log_flag_off`：
指标**不能**长在 `BOWHEAD_LLM_SHAPE_LOG` 的 if 里面——那个 flag 管的是「刷不刷日志」，
关掉它顺手把常驻观测面也关掉，正是本 sprint 反复在治的病
（[[feedback_pct_gated_flag_is_dead_at_zero]] 同族）。
"""

from __future__ import annotations

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry

from bowhead_core import metrics
from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)

_METRIC = "bowhead_llm_context_chars"


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.get_registry().reset()
    yield
    metrics.get_registry().reset()


def _router() -> Router:
    a = MockAdapter(provider_name="p1")
    a.scripted_responses = [LLMResponse(content="直答", usage=TokenUsage(input_tokens=100, output_tokens=50, cached_tokens=30))]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = a  # type: ignore[attr-defined]
    r = Router(reg)
    for t in ("lead", "analyst", "echo"):
        r.bind(AgentBinding(agent_type=t, primary=("p1", "mock")))
    return r


async def _run_one_turn(monkeypatch, *, shape_log: str) -> str:
    monkeypatch.setenv("BOWHEAD_LLM_SHAPE_LOG", shape_log)
    runner = SubagentRunner(router=_router(), tool_registry=build_default_registry(), backend="legacy")
    ctx = ToolExecutionContext(
        workspace_id="w",
        member_uid="m",
        user_id="u",
        turn_id="t",
        datarc=None,  # type: ignore[arg-type]
    )
    await runner.run(
        profile_name="compute",
        task="上下文体积指标契约",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )
    return metrics.get_registry().render_prometheus()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_size_is_recorded_with_profile_label(monkeypatch) -> None:
    rendered = await _run_one_turn(monkeypatch, shape_log="1")
    assert _METRIC in rendered, rendered
    assert 'profile="compute"' in rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_metric_survives_shape_log_flag_off(monkeypatch) -> None:
    """★ 关掉形状日志，指标照记。

    突变验证锚点：把 `_observe_ctx(...)` 挪回 `if _llm_shape_log_active:` 里 ⇒ 本条红。
    """
    rendered = await _run_one_turn(monkeypatch, shape_log="0")
    assert _METRIC in rendered, "shape-log flag 关掉时指标也没了——它被错误地长在那个 if 里"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recorded_size_covers_system_plus_messages(monkeypatch) -> None:
    """量的是 system + messages 合计，不是只有其中一半。

    compute 的 system prompt 本身就有 2 万余字符（`agents/prompts/system/compute.md`），
    所以合计必然落在 >10k 的桶里；若只量了 messages（任务串几十字符）会落进最小桶。
    """
    rendered = await _run_one_turn(monkeypatch, shape_log="1")
    over_10k = [ln for ln in rendered.splitlines() if f"{_METRIC}_bucket" in ln and 'le="10000.0"' in ln]
    assert over_10k, rendered
    assert over_10k[0].strip().endswith(" 0"), f"合计落进了 <=10k 的桶，像是只量了 messages：{over_10k[0]}"
