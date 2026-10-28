"""W1-C T1 — 确定性评测层(无 LLM, 进 make test-unit).

护两样东西: ① evalkit scorer/管线本身(为 T2/T3 提供地基) ② fixture 契约.
NoFabrication 是信任管线的回归守卫: 录制 turn 的授权数字 ∪ output → 断言零编造.
起步用 synthetic fixture(标 synthetic:true), 只护管线契约不做质量声明; 真录制
fixture 由 scripts/eval_export_fixture.py 从 turn_log 导出后替换.

trace 契约(review P1-4): NoFabricationScorer 读 trace["authorized_numbers"](非
invocations), replay agent 用 number_verifier.build_authorized_numbers 从 fixture
invocations 构造.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.evalkit.cases import EvalCase
from bowhead_core.evalkit.runner import EvalRunner
from bowhead_core.evalkit.scorers import (
    JSONFieldScorer,
    NoFabricationScorer,
    NumberToleranceScorer,
)
from bowhead_core.number_verifier import build_authorized_numbers

_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "eval" / "fixtures"


def _load_fixtures() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(_FIXTURE_DIR.glob("*.json"))]


def _invocations(fx: dict[str, Any]) -> list[SimpleNamespace]:
    """fixture invocation dict → number_verifier 可读的 invocation 对象."""
    return [
        SimpleNamespace(
            call_id=i.get("call_id"),
            result=i.get("result"),
            error=i.get("error"),
            nested=[],
        )
        for i in fx.get("invocations", [])
    ]


def _replay_agent(by_id: dict[str, dict[str, Any]]):
    async def _agent(case: EvalCase) -> tuple[Any, dict[str, Any]]:
        fx = by_id[case.id]
        auth = build_authorized_numbers(_invocations(fx))  # type: ignore[arg-type]
        return fx["output"], {"authorized_numbers": sorted(auth)}

    return _agent


@pytest.mark.unit
async def test_t1_fixtures_no_fabrication() -> None:
    """录制 fixture 经真 EvalRunner + NoFabrication: 全部 fabrication-clean."""
    fixtures = _load_fixtures()
    assert fixtures, f"no fixtures in {_FIXTURE_DIR}"
    cases = [EvalCase(id=fx["id"], input=fx.get("input"), tags=["t1", fx.get("numbers_basis", "?")], meta=fx.get("meta", {})) for fx in fixtures]
    by_id = {fx["id"]: fx for fx in fixtures}
    runner = EvalRunner(scorers=[NoFabricationScorer()], dataset_name="t1_fixtures")
    report = await runner.run(cases, _replay_agent(by_id))
    failures = [(cr.case_id, [s.detail for s in cr.scores if not s.passed]) for cr in report.case_results if not cr.passed()]
    assert report.pass_rate() == 1.0, f"fabrication in fixtures: {failures}"


@pytest.mark.unit
async def test_t1_fixture_contract_fields() -> None:
    """fixture 契约: 必备字段齐全(id/input/output/invocations/numbers_basis)."""
    for fx in _load_fixtures():
        for key in ("id", "input", "output", "invocations", "numbers_basis"):
            assert key in fx, f"fixture {fx.get('id')} missing '{key}'"
        assert fx["numbers_basis"] in ("full", "preview")


@pytest.mark.unit
async def test_t1_scorers_wired() -> None:
    """NumberTolerance + JSONField scorer 可用(管线覆盖 3 scorer 契约)."""
    nt = NumberToleranceScorer(rel_tolerance=0.01)
    r = nt.score(EvalCase(id="x", input="", expected="3.2"), "GMV 为 3.2 亿元")
    assert r.passed, r.detail
    jf = JSONFieldScorer(path="kpi.value")
    r2 = jf.score(
        EvalCase(id="y", input="", expected={"kpi": {"value": 11.39}}),
        {"kpi": {"value": 11.39}},
    )
    assert r2.passed, r2.detail


@pytest.mark.unit
async def test_t1_no_fabrication_negative() -> None:
    """负向闭合(review P2-2): output 含 invocations 里没有的数字 → 经同一 replay trace
    构造路径 NoFabrication 必判 fail —— 证 `pass_rate==1.0` 不是因为 scorer 退化成永远授权."""
    invs = [SimpleNamespace(call_id="c1", result=[{"gmv": 3.2}], error=None, nested=[])]
    auth = build_authorized_numbers(invs)  # type: ignore[arg-type]
    r = NoFabricationScorer().score(
        EvalCase(id="neg", input="x"),
        "华东 GMV 3.2 亿元，华南 999.7 亿元",  # 999.7 is unauthorized.
        {"authorized_numbers": sorted(auth)},
    )
    assert not r.passed
    assert any(abs(v - 999.7) < 0.1 for v in r.data.get("violations", []))
