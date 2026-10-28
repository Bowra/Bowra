"""ReDoS 防护锁（Sprint ι r2 P1 #2 / PRD §8 R-ι）—— gen3 形态。

原文件锁 gen2 的 `_NUM_MARKER_RE`（pre-bind `[num:V:U]` 匹配器）三件事：
形态 admit / safe-fail / 已绑跳过 / **对抗性回溯**。M3-4：该正则随 gen2 发射器
删除，前三项的语义归属如下（无覆盖真空）：

- 形态 admit + safe-fail canonicalize → `test_iota_parse_value.py`（`_parse_value_to_float`
  仍是 gen3 binder 的值解析器，一字未动）；
- 「已绑跳过」→ gen3 的 adjacent-`[N]` 跳过，锁在 `test_iota_autowrap_plain.py`
  与 `test_feishu0706_citation_precision.py`（真机事故锁）。

**本文件保留 ReDoS 防护的意图，把它移到今天真正跑在用户文本上的正则**：
`_AUTOWRAP_PLAIN_RAW`（binder 的匹配骨架，每条 narrative 散文行都过它）与
`citation_marks.strip_legacy` 的全部退役文法（永久兜底 seam，历史事件重放会喂它
任意脏文本）。10K 字符对抗输入 <100ms。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from bowhead_core.canvas_composers.answer_decomposer import _AUTOWRAP_PLAIN_RAW
from bowhead_core.citation_marks import strip_legacy

pytestmark = pytest.mark.no_cover

_ADVERSARIAL = [
    "1" * 9990,  # long digit run
    "1" * 9990 + "万",  # digit run + CN suffix
    "¥" + "1." * 2495 + "23",  # repeated decimal
    "abc" * 3333,  # bulk non-digit
    "[num:" + "1" * 9990,  # Partial unclosed marker: actual input to the fallback boundary.
    "[num:" + "1:" * 3330 + "]",  # Colon flood (worst input for the catch-all pass).
    ("9627万元 [1]，" * 800),  # Long body + many bound citations.
    # ↓ Actual impl review P0-1 culprit: **multiple `[num:` starts**. The seven cases above each have only one;
    # quadratic catch-all backtracking needs M starts × L length to explode. Old guards all passed while
    # `"[num:"*20000` consumed 4.5s pure CPU (this seam runs in async Gateway PATCH, halting all tenants).
    # Sampled adversarial corpora gave false assurance; these three target start density, not length.
    "[num:" * 20000,  # 20K starts, no closure (original PoC, 4.5s before fix).
    ("[num:" * 500 + "]") * 40,  # Start clusters + sparse closures (worst backtracking: traverse each cluster to its end).
    ("x [num:1.5:CNY] " * 20 + "[num:") * 500,  # Valid markers interleaved with partial starts (actual replay shape).
]

# Old length-based corpora still face the 100ms gate; separately require <30ms per 100KB for these three multi-start cases.
# The original 4.5s explosion is still ~150× slower than this throughput gate; the doubling-ratio gate below independently bounds complexity.
_MULTI_START_ADVERSARIAL = _ADVERSARIAL[7:]
_TIMING_REPEATS = 3


def _elapsed_seconds(operation: Callable[[], object]) -> float:
    """Return the best production-like CPU sample.

    A real algorithmic regression is slow on every sample.  Taking the minimum
    and measuring the current thread's CPU time keeps these ReDoS locks from
    charging OS scheduling stalls or unrelated worker threads to the regex while
    preserving the explicit throughput budgets and scaling ratios.
    """
    samples: list[float] = []
    for _ in range(_TIMING_REPEATS):
        t0 = time.thread_time()
        operation()
        samples.append(time.thread_time() - t0)
    return min(samples)


def _elapsed_ms(operation: Callable[[], object]) -> float:
    return _elapsed_seconds(operation) * 1000


@pytest.mark.parametrize("payload", _ADVERSARIAL)
def test_autowrap_regex_no_catastrophic_backtracking(payload: str) -> None:
    """binder 骨架正则：10K 级对抗输入 <100ms（典型 ~1ms）。"""
    elapsed_ms = _elapsed_ms(lambda: _AUTOWRAP_PLAIN_RAW.search(payload))
    assert elapsed_ms < 100, f"_AUTOWRAP_PLAIN_RAW 在 {len(payload)} 字符输入上耗时 {elapsed_ms:.2f}ms — 疑似回溯爆炸"


@pytest.mark.parametrize("payload", _ADVERSARIAL)
def test_strip_legacy_no_catastrophic_backtracking(payload: str) -> None:
    """永久兜底 seam：七道 pass 串跑，对抗输入同样 <100ms。

    这条比 gen2 时代更要紧——`strip_legacy` 现在是**唯一**还认退役语法的地方，
    且喂给它的是历史事件重放/外部导入的不受信文本。
    """
    elapsed_ms = _elapsed_ms(lambda: strip_legacy(payload))
    assert elapsed_ms < 100, f"strip_legacy 在 {len(payload)} 字符输入上耗时 {elapsed_ms:.2f}ms — 疑似回溯爆炸"


@pytest.mark.parametrize("payload", _MULTI_START_ADVERSARIAL)
def test_strip_legacy_multi_start_is_fast(payload: str) -> None:
    """多起点对抗输入（impl review P0-1 真凶形态）：每 100KB <30ms。"""
    elapsed_ms = _elapsed_ms(lambda: strip_legacy(payload))
    budget_ms = 30 * len(payload) / 100_000
    assert elapsed_ms < budget_ms, f"strip_legacy 在 {len(payload)} 字符多起点输入上耗时 {elapsed_ms:.2f}ms（预算 {budget_ms:.2f}ms）— 回溯爆炸复活"


@pytest.mark.parametrize("family", ("raw_starts", "clustered_closes", "mixed_replay"))
def test_strip_legacy_scales_linearly_not_quadratically(family: str) -> None:
    """**结构性**锁：三类多起点输入翻倍 → 耗时至多翻倍（线性），不得平方。

    为什么不只靠上面的绝对耗时门：那个门随 CI 机器快慢漂，且二次方只要退化到刚好
    卡线就能溜过去、再靠体积放大炸回来（P0-1 正是这样躲过既有 100ms 门的——旧锁
    封顶 10K 且单起点，而真实入口 `PATCH /canvas/{id}/sections/{id}` 的 body 无界）。
    增长比是算法复杂度本身，不随机器漂。改前实测 4x/step（二次方），改后 ~1x。
    """
    timings: list[float] = []
    for scale in (1, 2, 4):
        payload = _multi_start_payload(family, scale)
        timings.append(_elapsed_seconds(lambda payload=payload: strip_legacy(payload)))
    # Doubling each input gives ~4× for quadratic and ~2× for linear. A 3× gate tolerates timing noise/constants
    # while catching any actual quadratic regression.
    for i in (1, 2):
        # Small-sample timing floor: prevent ~0.0001s noise from inflating the ratio into false failures.
        ratio = timings[i] / max(timings[i - 1], 1e-4)
        assert ratio < 3.0, f"起点数翻倍耗时涨 {ratio:.1f}x（二次方回溯 ≈4x，线性 ≈2x）— 计时 {timings}"


def _multi_start_payload(family: str, scale: int) -> str:
    if family == "raw_starts":
        return "[num:" * (5000 * scale)
    if family == "clustered_closes":
        return (("[num:" * 500) + "]") * (10 * scale)
    if family == "mixed_replay":
        return ("x [num:1.5:CNY] " * 20 + "[num:") * (125 * scale)
    raise AssertionError(f"unknown multi-start family: {family}")
