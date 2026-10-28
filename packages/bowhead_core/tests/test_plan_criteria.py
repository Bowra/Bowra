"""author_step_criteria + 退化 lint 测试 (graph-mode 批 1, tech-design §1.2)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from bowhead_core.plan_authoring import (
    SYSTEM_PROMPT,
    _is_degenerate_criterion,
    _render_plan_for_amend,
    author_plan,
    author_step_criteria,
    lint_criteria,
)
from bowhead_core.plan_store import new_step


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _Resp:
    content: str
    usage: _Usage = field(default_factory=_Usage)


class _FakeRouter:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list = []

    async def invoke(self, agent_type, req):
        self.calls.append((agent_type, req))
        return _Resp(self._content, usage=_Usage(input_tokens=200, output_tokens=80))


class _BoomRouter:
    async def invoke(self, *a, **k):
        raise RuntimeError("down")


def _steps():
    a = new_step(plan_id="p1", seq=0, agent_name="compute", objective="联网检索行业新闻并汇总趋势", deliverable="insight")
    b = new_step(plan_id="p1", seq=1, agent_name="financial-analyst", objective="分析本季度营收结构", deliverable="表")
    return [a, b]


# ── Degenerate-criterion lint (review A7) ───────────────────────


@pytest.mark.parametrize(
    ("objective", "criterion", "degenerate"),
    [
        # Objective substring = mere restatement.
        ("产出竞品分析", "竞品分析", True),
        # No number and almost no new information (paraphrase).
        ("分析本季度营收结构", "含营收结构分析", True),
        # Specific number → retain.
        ("分析本季度营收结构", "含 2025 各季度营收数字", False),
        # No number but substantive new dimension (source type) → retain.
        ("联网检索行业新闻", "引用政府或行业协会信源", False),
        # Too short.
        ("目标", "有产出", True),
    ],
)
def test_degenerate_criterion_table(objective, criterion, degenerate) -> None:
    assert _is_degenerate_criterion(objective, criterion) is degenerate


def test_lint_caps_and_drops() -> None:
    obj = "分析营收"
    kept = lint_criteria(obj, ["含 2024/2025 对比数字", "x" * 100, "营收", "覆盖全部 5 个大区", "引用 ≥2 个官方信源"])
    assert len(kept) <= 3
    assert all(len(c) <= 60 for c in kept)
    assert "营收" not in kept  # Restatement dropped.


# ── author_step_criteria ─────────────────────────────────────


async def test_criteria_happy_path_and_lint() -> None:
    steps = _steps()
    payload = {
        "criteria": {
            steps[0].id: ["引用 ≥2 个官方信源", "联网检索行业新闻"],  # Second = restatement → lint drops.
            steps[1].id: ["含 2025 各季度营收数字"],
        }
    }
    router = _FakeRouter(json.dumps(payload, ensure_ascii=False))
    usage: list[tuple[int, int]] = []
    out = await author_step_criteria(router, steps=steps, trace_id="t1", usage_out=usage)
    assert out[steps[0].id] == ("引用 ≥2 个官方信源",)
    assert out[steps[1].id] == ("含 2025 各季度营收数字",)
    assert usage == [(200, 80)]  # Bill criteria tokens (same as A3).
    # Request has each step id/objective; budget decoupled from graph generation.
    _, req = router.calls[0]
    assert steps[0].id in req.messages[0].content
    assert req.max_output_tokens >= 600


async def test_criteria_unknown_id_and_bad_shape_ignored() -> None:
    steps = _steps()
    router = _FakeRouter(json.dumps({"criteria": {"ps_nonexistent": ["含数字 1"], steps[0].id: "not-a-list"}}))
    assert await author_step_criteria(router, steps=steps) == {}


async def test_criteria_llm_failure_returns_empty() -> None:
    assert await author_step_criteria(_BoomRouter(), steps=_steps()) == {}
    assert await author_step_criteria(_FakeRouter("not json"), steps=_steps()) == {}
    assert await author_step_criteria(_FakeRouter("{}"), steps=[]) == {}


# ── flag-off byte pin (review C6): criteria fully decoupled from graph generation ──


def test_author_prompt_untouched_by_criteria_feature() -> None:
    # Graph SYSTEM_PROMPT has no criteria terms: second-pass generation is sole injection point.
    assert "success_criteria" not in SYSTEM_PROMPT
    assert "验收" not in SYSTEM_PROMPT


async def test_author_plan_request_identical_regardless_of_flag(monkeypatch) -> None:
    captured: list = []

    class _Cap:
        async def invoke(self, agent_type, req):
            captured.append((req.system, req.messages[0].content, req.max_output_tokens))
            return _Resp('{"steps": []}')

    for flag in ("1", "0"):
        monkeypatch.setenv("BOWHEAD_PLANNING_VERIFY", flag)
        await author_plan(_Cap(), user_text="做尽调", agent_roster=[("kb-analyst", "读文档")])
    assert captured[0] == captured[1]


# ── Amend rendering includes acceptance row ─────────────────────


def test_render_plan_for_amend_includes_criteria() -> None:
    s = new_step(plan_id="p1", seq=0, agent_name="compute", objective="联网检索", success_criteria=("引用 ≥2 信源",))
    txt = _render_plan_for_amend([s])
    assert "验收: 引用 ≥2 信源" in txt


# ── ★116 dogfood 2026-08-11: "各" was main missed enumeration form ──
#
# Actual 08-04..08-11 criteria mostly used unbounded "含各品牌退款率数值",
# but `_EXHAUSTIVE_WORDS` only had "每个/所有/全部/逐个/逐一": code gate entirely missed
# main flaw, leaving prompt alone as guard; prompt drifts (same step may emit
# "含利润 Top3 品牌名称" then "含各品牌对应的利润数值").
#
# Test both directions (memory: assertions of absence need positive controls):
# Failure to block = leak; blocking benign "各自/各环节" also drops valid criteria, worse than leak.
_REAL_EXHAUSTIVE = (
    "含各品牌对应的利润数值",  # Actual 12:00 turn, beside Top3.
    "含各品类近30天营业额数值",  # "近30天" is no count bound.
    "含各品牌退款率数值",  # ❌ Negative example in prompt.
    "含各省名称及其对应的营业额合计数值",
    "含各门店名称、租金金额、租期起止或时长",
    "表格包含每个门店的名称",
)
_REAL_BOUNDED_OR_BENIGN = (
    "含利润 Top3 品牌名称",
    "含退款率 Top10 品牌及其退款率数值",
    "含至少 3 个门店的会员渗透率数值",
    "含品牌-门店对应关系及各自退款率、退款额数值",  # "各自" = respective, benign.
    "标注了转化漏斗各环节的定义或计算公式",  # "各环节" is no dimension term.
    # ⚠ Positive control needs a **real dimension term**: initial "覆盖全部 5 个竞品" used "竞品", absent
    # from _DIMENSION_WORDS; never reached branch, green for wrong reason, failing to catch
    # bounded-rule tightening wrongly rejecting "全部 5 个省" (existing test caught it).
    "覆盖全部 5 个省的营业额",  # Bounded full coverage valid; "省" is dimension.
    "含 10 家门店的退款额数值",  # Bounded quantifier form.
)


@pytest.mark.parametrize("criterion", _REAL_EXHAUSTIVE)
def test_unverifiable_catches_real_exhaustive_criteria(criterion: str) -> None:
    from bowhead_core.plan_authoring import _is_unverifiable_criterion

    assert _is_unverifiable_criterion(criterion), f"应判为不可核验: {criterion}"


@pytest.mark.parametrize("criterion", _REAL_BOUNDED_OR_BENIGN)
def test_unverifiable_keeps_bounded_and_benign_criteria(criterion: str) -> None:
    from bowhead_core.plan_authoring import _is_unverifiable_criterion

    assert not _is_unverifiable_criterion(criterion), f"不该被丢: {criterion}"


def test_lint_criteria_drops_exhaustive_keeps_bounded() -> None:
    """闸的实效在 lint_criteria 上 —— 它是 `continue` 丢弃, 不是告警。"""
    from bowhead_core.plan_authoring import lint_criteria

    got = lint_criteria("按品牌拆解退款率", ["含各品牌退款率数值", "含退款率 Top10 品牌及其退款率数值"])
    assert got == ("含退款率 Top10 品牌及其退款率数值",)


# ── ★Adversarial review 2026-08-12: cardinality must constrain dimension, not merely occur ──
#
# First tightening (number + quantifier required) still bypassed: any incidental count exempted checks,
# unrelated to how many brands to list. 4 of 7 adversarial cases leaked.
# **Same class** as replaced allow-any-number rule; record here to prevent a third recurrence.
_ADVERSARIAL_STILL_EXHAUSTIVE = (
    "含各品牌的 3 个核心指标数值",  # 3 core metrics ≠3 brands.
    "含各门店近30天的 5 个指标",
    "列出每个品类的营业额, 保留 2 位小数",  # "2 位小数" is precision, not count.
    "含所有门店的客流量, 分 4 个时段",
    "各省营业额, 精确到 1 元",
    "含各品牌数值，至少 3 位小数",  # "至少 N" also must bind dimension.
)


@pytest.mark.parametrize("criterion", _ADVERSARIAL_STILL_EXHAUSTIVE)
def test_unrelated_number_does_not_buy_an_exemption(criterion: str) -> None:
    from bowhead_core.plan_authoring import _is_unverifiable_criterion

    assert _is_unverifiable_criterion(criterion), f"数字没管住维度, 不该算有界: {criterion}"


@pytest.mark.parametrize(
    "criterion",
    (
        "含利润最高的 3 个品牌名称",  # 3 brands = binds dimension.
        "含 10 家门店的退款额数值",
        "覆盖全部 5 个省的营业额数值",
        "含至少 3 个门店的会员渗透率数值",
        "含营业额 Top10 品类及对应数值",  # Ranking bound works independently.
    ),
)
def test_cardinality_bound_to_dimension_still_passes(criterion: str) -> None:
    """★正对照：真正管住维度的基数必须继续放行，否则上一条就是把闸焊死。"""
    from bowhead_core.plan_authoring import _is_unverifiable_criterion

    assert not _is_unverifiable_criterion(criterion), f"不该被丢: {criterion}"


# ── ★116 evidence 2026-08-12: criteria path fails to strip fence, dropping whole plan checklist ──
#
# Original production log (1.0.91, t_075f9c2b4731, finish=stop means model ended normally):
#   plan_criteria.unparseable raw='```json\n{\n  "criteria": {\n    "ps_...": [...
# Graph path always stripped fences; criteria used bare json.loads. Any model wrapper
# empties all plan criteria → verification degrades to skipped for whole plan.
# Production confirms **whole-set failure**, not item drops: since 08-04, 4 of 44 plans (9%) had no criteria,
# while 0 had incomplete criteria: all or none.
_FENCED = '```json\n{"criteria": {"ps_a": ["含利润最高的 3 个品牌名称"]}}\n```'


def test_criteria_parser_strips_code_fence() -> None:
    from bowhead_core.plan_authoring import _loads_lenient

    got = _loads_lenient(_FENCED)
    assert got == {"criteria": {"ps_a": ["含利润最高的 3 个品牌名称"]}}


def test_plain_json_still_parses() -> None:
    """★正对照: 不带 fence 的正常响应不能因为这次兜底而走偏。"""
    from bowhead_core.plan_authoring import _loads_lenient

    assert _loads_lenient('{"criteria": {"ps_a": ["x"]}}') == {"criteria": {"ps_a": ["x"]}}


def test_真正的坏json仍然返回None() -> None:
    """兜底不能变成「什么都吞」——真解析不出必须仍是 None, 否则错误被静默。"""
    from bowhead_core.plan_authoring import _loads_lenient

    assert _loads_lenient("这不是 JSON") is None
    assert _loads_lenient('{"criteria": {') is None


@pytest.mark.asyncio
async def test_author_step_criteria_survives_fenced_response() -> None:
    """★穿闸: 不测 helper 自己对, 要测 author_step_criteria 真能吃下 fence 响应。"""
    from bowhead_core.plan_authoring import author_step_criteria
    from bowhead_core.plan_store import new_step

    step = new_step(plan_id="p1", seq=0, agent_name="business-analyst", objective="按品牌拆解利润")
    fenced = f'```json\n{{"criteria": {{"{step.id}": ["含利润最高的 3 个品牌名称"]}}}}\n```'

    class _R:
        async def invoke(self, _agent, _req):
            return SimpleNamespace(content=fenced, usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    got = await author_step_criteria(_R(), steps=[step])
    assert got.get(step.id) == ("含利润最高的 3 个品牌名称",), got


# ── All criteria linted away silently disables step verification; must be observable (116 dogfood 2026-08-14) ──


async def test_all_criteria_linted_out_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """★真机来的：08-05..08-14 的 166 个真实步里 19 步(≈11%)没有判据，
    而日志里既无 `llm_failed` 也无 `unparseable` —— 有一条**无声**的路。
    这里钉住它：模型给了条目、lint 全丢光时必须留下痕迹，
    否则 verifier 判 `skipped/no_criteria` 而没人知道核验被关掉了。

    突变：把 `elif items: logger.info(...)` 删掉 → 本条红。
    """
    steps = _steps()
    # Both dropped by unverifiable lint (chart shape + unbounded enumeration).
    payload = {"criteria": {steps[0].id: ["柱状图按客单价降序排列", "含各品牌的退款率数值"]}}
    with caplog.at_level("INFO", logger="bowhead_core.plan_authoring"):
        out = await author_step_criteria(_FakeRouter(json.dumps(payload, ensure_ascii=False)), steps=steps, trace_id="t_lint")
    assert steps[0].id not in out, "前提：这两条判据必须真被 lint 丢掉，否则本测试测不到那条路"
    assert any("plan_criteria.all_linted_out" in r.message for r in caplog.records)


async def test_steps_without_criteria_summary_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """分子/分母同一行 —— 事后不必翻库就能算「核验静默关闭」的比例。

    覆盖另一条路：模型压根没给这个 step id（不是被 lint 丢的）。
    """
    steps = _steps()
    payload = {"criteria": {steps[0].id: ["含 2025 各季度营收数字"]}}  # steps[1] absent.
    with caplog.at_level("INFO", logger="bowhead_core.plan_authoring"):
        out = await author_step_criteria(_FakeRouter(json.dumps(payload, ensure_ascii=False)), steps=steps, trace_id="t_miss")
    assert steps[1].id not in out
    rec = [r for r in caplog.records if "plan_criteria.steps_without_criteria" in r.message]
    assert rec, "缺判据的步必须留下汇总行"
    assert "missing=1" in rec[0].getMessage() and "of=2" in rec[0].getMessage()


async def test_full_coverage_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """正对照：每步都有判据时不许刷这两条日志（否则告警就成了噪声）。"""
    steps = _steps()
    payload = {"criteria": {steps[0].id: ["引用 ≥2 个官方信源"], steps[1].id: ["含 2025 各季度营收数字"]}}
    with caplog.at_level("INFO", logger="bowhead_core.plan_authoring"):
        out = await author_step_criteria(_FakeRouter(json.dumps(payload, ensure_ascii=False)), steps=steps)
    assert len(out) == 2
    assert not [r for r in caplog.records if "plan_criteria." in r.message]
