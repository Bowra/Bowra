"""plan_verify unit tests (graph-mode 批 1, tech-design §1.3/§1.7)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from bowhead_core.plan_verify import (
    ITEM_CANNOT_TELL,
    ITEM_MET,
    ITEM_UNMET,
    VERDICT_FAIL,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    VERDICT_SKIPPED,
    VerifyItem,
    VerifyOutcome,
    build_verify_digest,
    outcome_from_json,
    reduce_verdict,
    verify_step_output,
)


@dataclass
class _Resp:
    content: str


class _FakeRouter:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list = []

    async def invoke(self, agent_type, req):
        self.calls.append((agent_type, req))
        return _Resp(self._content)


class _BoomRouter:
    async def invoke(self, *a, **k):
        raise RuntimeError("router down")


def _items(*statuses: str) -> list[VerifyItem]:
    return [VerifyItem(text=f"c{i}", status=s) for i, s in enumerate(statuses)]


# ── Reduction rules (table-driven) ──────────────────────────────


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((ITEM_MET, ITEM_MET), VERDICT_PASS),
        ((ITEM_MET,), VERDICT_PASS),
        ((ITEM_MET, ITEM_UNMET), VERDICT_PARTIAL),
        ((ITEM_MET, ITEM_CANNOT_TELL), VERDICT_PARTIAL),
        ((ITEM_UNMET,), VERDICT_FAIL),
        ((ITEM_UNMET, ITEM_UNMET), VERDICT_FAIL),
        # ★2026-08-16 tightening: any cannot_tell prevents clean fail. Old review A4/A7 rule
        # predated **enforceable fail**, merely a shadow label. Under enforce,
        # fail blocks downstream; tighten to every criterion evaluated and unmet.
        ((ITEM_UNMET, ITEM_UNMET, ITEM_CANNOT_TELL), VERDICT_PARTIAL),
        ((ITEM_UNMET, ITEM_CANNOT_TELL), VERDICT_PARTIAL),
        # cannot_tell alone is no fail (review A4/A7).
        ((ITEM_CANNOT_TELL, ITEM_CANNOT_TELL), VERDICT_SKIPPED),
        ((), VERDICT_SKIPPED),
    ],
)
def test_reduce_verdict_table(statuses, expected) -> None:
    assert reduce_verdict(_items(*statuses)) == expected


# ── Evidence-oriented digest (review A1/A3) ──────────────────────


def _rich_payload() -> dict:
    return {
        "title": "华东区 Q2 经营分析",
        "exec_summary": "营收同比+12%",
        "kpis": [{"label": "营收", "value": "91亿", "delta": "+12%"}, {"label": "客流", "value": "3.2M"}],
        "ground_facts": ["2025Q2 营收 91 亿(bi_query)"],
        "sources": [{"name": "商务部306号文", "kind": "web"}, {"name": "datarc", "kind": "bi"}],
        "insights": [{"title": "会员转化", "thesis": "漏斗收窄", "narrative": "x" * 500}],
        "recommendations": [{"title": "扩会员"}],
        "caveats": ["无 LTV 字段"],
        "insight_section_ids": [],
    }


def test_digest_keeps_evidence_keys() -> None:
    d = json.loads(build_verify_digest(_rich_payload()))
    # Evidence for criteria like numeric inclusion / ≥N cited sources must be present.
    assert any("91亿" in row for row in d["kpis"])
    assert d["ground_facts"]
    assert any("商务部" in row for row in d["sources"])
    # Batch 1-c: no field-level narrative truncation; verifier needs full text to judge sufficiency.
    # Overall budget bounds size (test_digest_respects_budget), **recording** _truncated_keys.
    assert d["insights"][0]["narrative"] == "x" * 500
    assert d["counts"]["kpis"] == 2


def test_digest_fallback_markdown_not_hollowed() -> None:
    # Review A1 case②: web-only payload has just fallback_markdown; never hollow it out.
    d = json.loads(build_verify_digest({"fallback_markdown": "行业新闻综述 " * 300}))
    assert d["fallback_markdown"].startswith("行业新闻综述")


def test_digest_thin_path_backfill() -> None:
    # Review A3: thin-path real content backfilled through extra_sections.
    payload = {"exec_summary": "s", "insights": [], "insight_section_ids": ["sec_1", "sec_2"]}
    d = json.loads(build_verify_digest(payload, extra_sections=["图表: 逐季营收对比", "结论段落"]))
    assert d["canvas_sections_text"] == ["图表: 逐季营收对比", "结论段落"]
    assert d["counts"]["canvas_sections"] == 2


def test_digest_respects_budget() -> None:
    payload = _rich_payload()
    payload["fallback_markdown"] = "长" * 5000
    out = build_verify_digest(payload, budget_chars=1500)
    assert len(out) <= 1500
    json.loads(out)  # Never produce truncated JSON.


def test_digest_over_budget_falls_back_to_parseable_core() -> None:
    # Implementation review B6: still over after removing optional keys →minimal core, never raw JSON truncation.
    payload = _rich_payload()
    payload["ground_facts"] = [f"事实{i}-" + "字" * 150 for i in range(16)]
    out = build_verify_digest(payload, budget_chars=800)
    d = json.loads(out)  # Must parse.
    assert d.get("_truncated") is True
    assert len(d.get("ground_facts", [])) <= 8


# ── verify_step_output ───────────────────────────────────────


async def test_verify_happy_path_pass() -> None:
    router = _FakeRouter(json.dumps({"items": [{"status": "met"}, {"status": "met"}, {"status": "met"}]}))
    out = await verify_step_output(router, objective="o", deliverable="表", criteria=["c0", "c1"], digest="{}")
    assert out.verdict == VERDICT_PASS
    assert [i.status for i in out.items] == [ITEM_MET, ITEM_MET, ITEM_MET]
    # Prompt includes objective/deliverable/list/digest (A12 adds deliverable).
    sent = router.calls[0][1].messages[0].content
    assert "期望产出形态: 表" in sent
    assert "1. c0" in sent


@pytest.mark.parametrize("requirement_status", [ITEM_UNMET, ITEM_CANNOT_TELL])
async def test_verify_does_not_pass_when_generated_criteria_omit_required_exchange(requirement_status: str) -> None:
    objective = "Calculate net revenue and verify the refund definition through a direct exchange with the policy expert."
    router = _FakeRouter(
        json.dumps(
            {
                "items": [
                    {"status": "met"},
                    {"status": "met"},
                    {"status": requirement_status, "note": "The required exchange is not established."},
                ]
            }
        )
    )
    outcome = await verify_step_output(
        router,
        objective=objective,
        deliverable="answer",
        criteria=["Includes net revenue of 1080 USD", "Names refund-v7 and 2026-Q3"],
        digest='{"exec_summary":"Net revenue is 1080 USD under refund-v7 for 2026-Q3; peer verification remains pending."}',
    )
    assert outcome.verdict == VERDICT_PARTIAL
    assert outcome.items[-1].text == objective
    assert outcome.items[-1].status == requirement_status
    assert f"3. {objective}" in router.calls[0][1].messages[0].content


async def test_verify_fail_only_when_unmet() -> None:
    router = _FakeRouter(json.dumps({"items": [{"status": "unmet", "note": "Quarterly figures are missing."}, {"status": "unmet"}]}))
    out = await verify_step_output(router, objective="o", deliverable="", criteria=["c0"], digest="{}")
    assert out.verdict == VERDICT_FAIL
    assert out.unmet_texts() == ("c0", "o")


async def test_verify_no_criteria_skipped() -> None:
    out = await verify_step_output(_FakeRouter("{}"), objective="o", deliverable="", criteria=["", "  "], digest="{}")
    assert out.verdict == VERDICT_SKIPPED
    assert out.note == "no_criteria"


async def test_verify_router_error_skipped() -> None:
    out = await verify_step_output(_BoomRouter(), objective="o", deliverable="", criteria=["c0"], digest="{}")
    assert out.verdict == VERDICT_SKIPPED
    assert out.note.startswith("llm_error:")


async def test_verify_unparseable_skipped() -> None:
    out = await verify_step_output(_FakeRouter("not json at all"), objective="o", deliverable="", criteria=["c0"], digest="{}")
    assert out.verdict == VERDICT_SKIPPED
    assert out.note == "unparseable"


async def test_verify_missing_items_align_cannot_tell() -> None:
    # Missing LLM item →aligned cannot_tell, never shifted or crashing.
    router = _FakeRouter(json.dumps({"items": [{"text": "c0", "status": "met"}]}))
    out = await verify_step_output(router, objective="o", deliverable="", criteria=["c0", "c1"], digest="{}")
    assert [i.status for i in out.items] == [ITEM_MET, ITEM_CANNOT_TELL, ITEM_CANNOT_TELL]
    assert out.verdict == VERDICT_PARTIAL


async def test_verify_bad_status_coerced() -> None:
    router = _FakeRouter(json.dumps({"items": [{"text": "c0", "status": "maybe?"}]}))
    out = await verify_step_output(router, objective="o", deliverable="", criteria=["c0"], digest="{}")
    assert out.items[0].status == ITEM_CANNOT_TELL
    assert out.verdict == VERDICT_SKIPPED


# ── Serialization round trip ───────────────────────────────────


def test_outcome_json_roundtrip() -> None:
    out = VerifyOutcome(
        VERDICT_PARTIAL,
        items=(VerifyItem("含季度数字", ITEM_MET), VerifyItem("引用≥2信源", ITEM_UNMET, note="仅1个")),
    )
    back = outcome_from_json(out.to_json())
    assert back is not None
    assert back.verdict == VERDICT_PARTIAL
    assert back.unmet_texts() == ("引用≥2信源",)
    assert outcome_from_json("") is None
    assert outcome_from_json("{broken") is None


# ── Enforce precondition: honest out-of-domain refusal is no failure (2026-08-16) ──
# Actual 116 calibration false negative: coldchain-temp-audit produced exemplary refusal
# ("当前工作区…没有冷链温控相关的数据字段" + missing list + suggested next steps),
# all three unmet →fail. Enforce marks FAILED/isolates downstream,
# user reason acceptance-unmet: **clearest explanation misclassified as failure**.


def test_all_not_applicable_is_out_of_domain_not_fail() -> None:
    """★正身：全 not_applicable → out_of_domain，**不是 fail**。

    driver 的 enforce 分支只认 `VERDICT_FAIL`，所以这一档天然被豁免。
    突变：把 na 那条归约删掉 → 落回 skipped，本条红。
    """
    from bowhead_core.plan_verify import ITEM_NOT_APPLICABLE, VERDICT_OUT_OF_DOMAIN

    assert reduce_verdict(_items(ITEM_NOT_APPLICABLE, ITEM_NOT_APPLICABLE, ITEM_NOT_APPLICABLE)) == VERDICT_OUT_OF_DOMAIN


def test_not_applicable_does_not_whitewash_real_gaps() -> None:
    """★负对照：混着 unmet 时仍是 fail —— 豁免不能变成逃生口。

    只要还有真实缺口，一个 N/A 项不许把它洗白。
    """
    from bowhead_core.plan_verify import ITEM_NOT_APPLICABLE, VERDICT_PARTIAL

    # N/A + unmet: neither fully out-of-domain (partly performed) nor clean fail (one unjudgeable) →partial.
    # Crucially **never** out_of_domain/pass; card still exposes gap, exception creates no escape hatch.
    assert reduce_verdict(_items(ITEM_NOT_APPLICABLE, "unmet")) == VERDICT_PARTIAL
    # Any met means partial work, old partial rule.
    assert reduce_verdict(_items(ITEM_NOT_APPLICABLE, "met")) == VERDICT_PARTIAL


def test_legacy_verdicts_unchanged() -> None:
    """回归：老三档一个字节不变（新 item 状态不许改既有归约）。"""
    from bowhead_core.plan_verify import VERDICT_FAIL, VERDICT_PARTIAL, VERDICT_PASS

    assert reduce_verdict(_items("met", "met")) == VERDICT_PASS
    assert reduce_verdict(_items("met", "unmet")) == VERDICT_PARTIAL
    assert reduce_verdict(_items("unmet", "unmet")) == VERDICT_FAIL


def test_undecidable_item_blocks_clean_fail() -> None:
    """真机回归: 116 语料 ps_c911c707d1fa(小程序 DAU)。

    产出如实写明「本 workspace 无渠道维度, BI 把『小程序 客流量』静默降级成全量」,
    两条判据一条 unmet 一条 cannot_tell。老归约把它算成 fail —— 翻 enforce 后
    这一步会被标 FAILED 并对下游屏蔽, 而它其实交了 29 天的完整日趋势。
    """
    assert reduce_verdict(_items("unmet", ITEM_CANNOT_TELL)) == VERDICT_PARTIAL
    # Positive control: all criteria evaluated, all unmet; exactly what enforce should block.
    assert reduce_verdict(_items("unmet", "unmet")) == VERDICT_FAIL
    assert reduce_verdict(_items("cannot_tell", "cannot_tell")) == VERDICT_SKIPPED


def test_parser_accepts_not_applicable() -> None:
    """解析层要认这个状态，否则 LLM 给了也会被打回 cannot_tell（=静默失效）。"""
    import json as _json

    from bowhead_core.plan_verify import ITEM_NOT_APPLICABLE, _parse_items

    raw = _json.dumps({"items": [{"status": "not_applicable"}, {"status": "not_applicable"}]})
    items = _parse_items(raw, ["含超温时段起止时间", "含承运商名称"])
    assert items is not None
    assert [i.status for i in items] == [ITEM_NOT_APPLICABLE, ITEM_NOT_APPLICABLE]


def test_prompt_carries_both_calibration_rules() -> None:
    """两条判据规则必须真在 prompt 里 —— 它们是 enforce 两个假阴家族各自的解药。"""
    from bowhead_core.plan_verify import _SYSTEM_PROMPT

    # Family A: honest out-of-domain refusal.
    assert "not_applicable" in _SYSTEM_PROMPT
    assert "诚实告知" in _SYSTEM_PROMPT
    # Family B: digest compressed; incomplete enumeration is not absence.
    assert "压缩视图" in _SYSTEM_PROMPT
    assert "逐项列举" in _SYSTEM_PROMPT
    # Also state when still unmet, preventing rule overapplication.
    assert "小基数" in _SYSTEM_PROMPT
