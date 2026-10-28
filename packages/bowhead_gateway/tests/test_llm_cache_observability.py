"""prompt 缓存可观测面 —— 上下文工程 sprint 批 0-a / 0-e。

为什么这条指标值钱：`turn-latency` sprint 把真实命中率从 31.7% 做到 67.0%，靠的是**一次性手工 A/B**。
在此之前没有任何常驻指标 —— 也就是说，任何人往 system 前缀里加一段 per-turn 内容、
把命中率打回 30%，**没有人会知道**。这些用例 pin 的就是「掉回去时看得见」。

⚠ 口径是 `cached/(in+cached)` 不是 `cached/in`：这两家 provider 的 `input_tokens` 都是
**未命中部分**，用 `cached/in` 会算出 >1 的比率。
"""

from __future__ import annotations

import pytest
from bowhead_gateway.llm_usage_recorder import _NEVER_REPORTED_STREAK, LLMUsageRecorder
from bowhead_router.usage import LLMUsageRecord, UsageContext

from bowhead_core import metrics


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.get_registry().reset()
    yield
    metrics.get_registry().reset()


def _rec(*, provider: str = "p1", agent: str = "lead", tokens_in: int = 100, cached: int = 0) -> LLMUsageRecord:
    return LLMUsageRecord(
        agent_type=agent,
        provider=provider,
        model="m1",
        tokens_input=tokens_in,
        tokens_output=10,
        tokens_cached=cached,
        streamed=False,
        context=UsageContext(reason="turn"),
    )


def _recorder() -> LLMUsageRecorder:
    # sink never touches store (only appends in memory), so no real DB needed here.
    return LLMUsageRecorder(store=None)  # type: ignore[arg-type]


def _never_reported() -> dict[str, float]:
    return {lbl["provider"]: v for lbl, v in metrics.counter_items("bowhead_llm_cache_never_reported_total")}


def test_hit_ratio_uses_in_plus_cached_as_denominator() -> None:
    """★ 分母口径。in=100 cached=300 ⇒ 0.75，不是 3.0。

    突变验证锚点：把分母写成 `record.tokens_input` 会让 ratio=3.0 落进 +Inf 桶，
    下面对 `le="0.8"` 的断言就会掉。
    """
    r = _recorder()
    r.sink(_rec(tokens_in=100, cached=300))
    rendered = metrics.get_registry().render_prometheus()
    assert "bowhead_llm_cache_hit_ratio" in rendered
    # 0.75 must enter cumulative bucket le="0.8".
    lines = [ln for ln in rendered.splitlines() if "bowhead_llm_cache_hit_ratio_bucket" in ln and 'le="0.8"' in ln]
    assert lines, rendered
    assert lines[0].strip().endswith(" 1"), lines[0]


def test_ratio_is_labelled_by_provider_and_agent() -> None:
    """按 provider × agent_type 分开 —— 「Lead 掉了还是 compute 掉了」是不同的诊断。"""
    r = _recorder()
    r.sink(_rec(provider="deepseek_anthropic", agent="lead", cached=50))
    r.sink(_rec(provider="aliyun_qwen", agent="compute", cached=50))
    rendered = metrics.get_registry().render_prometheus()
    assert 'provider="deepseek_anthropic"' in rendered
    assert 'agent_type="compute"' in rendered


def test_zero_denominator_records_nothing() -> None:
    """in=0 且 cached=0（mock adapter / 不回 usage 的 provider）不该污染分布。"""
    r = _recorder()
    r.sink(_rec(tokens_in=0, cached=0))
    assert "bowhead_llm_cache_hit_ratio" not in metrics.get_registry().render_prometheus()


def test_never_reported_fires_only_after_the_streak() -> None:
    """0-e：连续 N 次零命中才算线索；第 N-1 次不报。"""
    r = _recorder()
    for _ in range(_NEVER_REPORTED_STREAK - 1):
        r.sink(_rec(cached=0))
    assert _never_reported() == {}
    r.sink(_rec(cached=0))
    assert _never_reported() == {"p1": 1.0}


def test_any_cache_hit_resets_the_streak() -> None:
    """★ 一次命中就重置 —— 否则「偶尔没命中」会被累计成「从不命中」。"""
    r = _recorder()
    for _ in range(_NEVER_REPORTED_STREAK - 1):
        r.sink(_rec(cached=0))
    r.sink(_rec(cached=1))  # One hit.
    for _ in range(_NEVER_REPORTED_STREAK - 1):
        r.sink(_rec(cached=0))
    assert _never_reported() == {}, "streak 没被重置"


def test_streak_is_tracked_per_provider() -> None:
    """两家 provider 交替调用时，各自的 streak 不能互相干扰。"""
    r = _recorder()
    for _ in range(_NEVER_REPORTED_STREAK):
        r.sink(_rec(provider="a", cached=0))
        r.sink(_rec(provider="b", cached=5))
    assert _never_reported() == {"a": 1.0}


def test_one_off_agents_do_not_feed_the_never_reported_signal() -> None:
    """title / distill 这类一次性调用本就没前缀可复用，算进去会把判别指标稀释成噪声。

    但它们的**命中率**照记（上面 hit_ratio 不做 agent 过滤）。
    """
    r = _recorder()
    for _ in range(_NEVER_REPORTED_STREAK * 2):
        r.sink(_rec(agent="echo", cached=0))
    assert _never_reported() == {}
    assert "bowhead_llm_cache_hit_ratio" in metrics.get_registry().render_prometheus()


def test_sink_still_buffers_when_metrics_backend_is_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 观测挂了不能波及记账 —— sink 跑在 LLM 调用返回路径上。

    这是 `_observe_cache` 被 `contextlib.suppress` 包住的理由，用例把它 pin 死。
    """
    r = _recorder()

    def _boom(self, record):  # noqa: ANN001, ARG001
        raise RuntimeError("metrics down")

    monkeypatch.setattr(LLMUsageRecorder, "_observe_cache", _boom)
    r.sink(_rec(cached=10))
    assert r.stats["buffered"] == 1, "观测异常把用量记录吞了"
