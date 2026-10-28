"""EvalKit — 端到端最小测试."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from bowhead_core.evalkit import (
    ContainsScorer,
    EvalCase,
    EvalRunner,
    ExactMatchScorer,
    JSONFieldScorer,
    NoFabricationScorer,
    NumberToleranceScorer,
    load_cases_jsonl,
    write_cases_jsonl,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_case_jsonl_roundtrip(tmp_path: Path) -> None:
    cases = [
        EvalCase(id="a", input="q1", expected="ans1", tags=["t1"]),
        EvalCase(id="b", input="q2", expected={"field": 10}, tags=["t2"], meta={"source": "bug-123"}),
    ]
    p = tmp_path / "cases.jsonl"
    write_cases_jsonl(p, cases)
    loaded = load_cases_jsonl(p)
    assert len(loaded) == 2
    assert loaded[0].id == "a" and loaded[0].expected == "ans1"
    assert loaded[1].expected == {"field": 10}
    assert loaded[1].meta == {"source": "bug-123"}


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    p = tmp_path / "dup.jsonl"
    p.write_text(
        '{"id": "x", "input": "a"}\n{"id": "x", "input": "b"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases_jsonl(p)


def test_exact_match_scorer() -> None:
    s = ExactMatchScorer()
    c = EvalCase(id="a", input="q", expected="hello")
    r = s.score(c, "hello")
    assert r.passed and r.score == 1.0
    r2 = s.score(c, "HELLO")
    assert not r2.passed
    r3 = ExactMatchScorer(case_insensitive=True).score(c, "HELLO")
    assert r3.passed


def test_contains_scorer_partial() -> None:
    s = ContainsScorer()
    c = EvalCase(id="a", input="q", expected=["营业额", "同比", "下滑"])
    out = "营业额同比下滑"
    r = s.score(c, out)
    assert r.passed and r.score == 1.0

    out2 = "营业额同比"  # Missing the required keyword "下滑".
    r2 = s.score(c, out2)
    assert not r2.passed
    assert r2.score == pytest.approx(2 / 3)
    assert "下滑" in r2.detail


def test_json_field_scorer() -> None:
    s = JSONFieldScorer(path="result.kind")
    c = EvalCase(id="a", input="q", expected={"result": {"kind": "metric"}})
    r = s.score(c, {"result": {"kind": "metric", "val": 42}})
    assert r.passed
    r2 = s.score(c, {"result": {"kind": "image"}})
    assert not r2.passed


def test_number_tolerance() -> None:
    s = NumberToleranceScorer(rel_tolerance=0.05)
    c = EvalCase(id="a", input="q", expected="29.3亿")
    r = s.score(c, "营业额大概是 29.5 亿")  # 0.68% rel diff
    assert r.passed
    r2 = s.score(c, "营业额大概是 35 亿")
    assert not r2.passed


def test_no_fabrication_basic() -> None:
    s = NoFabricationScorer()
    c = EvalCase(id="a", input="q", expected=None)
    # The trace authorizes 29.3 and 12.5.
    trace = {"authorized_numbers": [29.3, 12.5]}
    # Output uses only authorized numbers + small integers + years → pass.
    out_ok = "营业额 29.3 亿，同比 12.5%（vs 2024 年），步骤 3 条。"
    assert s.score(c, out_ok, trace).passed

    # 42.7 appears in output → violation.
    out_bad = "营业额 29.3 亿，但品类 B 涨了 42.7 亿"
    r = s.score(c, out_bad, trace)
    assert not r.passed
    assert 42.7 in r.data["violations"]


def test_no_fabrication_no_trace_allows_everything() -> None:
    """没 trace → 没 authorized → 所有非白名单数字都 unauthorized → 失败."""
    s = NoFabricationScorer()
    c = EvalCase(id="a", input="q", expected=None)
    out = "营业额 29.3 亿"
    r = s.score(c, out, trace=None)
    assert not r.passed  # 29.3 is not whitelisted, so it is a violation.


async def test_runner_happy_path() -> None:
    cases = [
        EvalCase(id="c1", input="x", expected="hello"),
        EvalCase(id="c2", input="y", expected="world"),
    ]

    async def agent(case):  # type: ignore[no-untyped-def]
        mapping = {"x": "hello", "y": "wrong"}
        return mapping[case.input], None

    runner = EvalRunner(
        scorers=[ExactMatchScorer()],
        dataset_name="demo",
    )
    report = await runner.run(cases, agent)
    assert len(report.case_results) == 2
    assert report.pass_rate() == 0.5
    # by_scorer aggregation
    m = report.by_scorer()
    assert "exact_match" in m
    assert m["exact_match"]["n"] == 2
    assert m["exact_match"]["pass_rate"] == 0.5


async def test_runner_case_timeout() -> None:
    import asyncio

    cases = [EvalCase(id="slow", input="x", expected="h")]

    async def agent(case):  # type: ignore[no-untyped-def]
        await asyncio.sleep(5)
        return "h", None

    runner = EvalRunner(
        scorers=[ExactMatchScorer()],
        per_case_timeout_sec=0.05,
    )
    report = await runner.run(cases, agent)
    assert report.case_results[0].error
    assert "timed out" in report.case_results[0].error


async def test_runner_writes_json_report(tmp_path: Path) -> None:
    cases = [EvalCase(id="c1", input="x", expected="a")]

    async def agent(case):  # type: ignore[no-untyped-def]
        return "a", None

    runner = EvalRunner(scorers=[ExactMatchScorer()], dataset_name="d")
    report = await runner.run(cases, agent)
    out = tmp_path / "report.json"
    report.write_json(out)
    loaded = json.loads(out.read_text())
    assert loaded["dataset"] == "d"
    assert loaded["n_cases"] == 1
    assert loaded["pass_rate"] == 1.0
    assert loaded["cases"][0]["case_id"] == "c1"


# ─────────────────────────────────────────────────────────────
# Retrieval ranking scorers (kb-optimization evaluation gate MVP-2)
# ─────────────────────────────────────────────────────────────
def test_recall_at_k_full_partial_none() -> None:
    from bowhead_core.evalkit import RecallAtKScorer

    s = RecallAtKScorer(k=3)
    case = EvalCase(id="q", input="q", expected=["a.pdf", "b.pdf"])
    full = s.score(case, ["a.pdf", "x.pdf", "b.pdf", "c.pdf"])
    assert full.passed and full.score == 1.0
    partial = s.score(case, ["a.pdf", "x.pdf", "y.pdf", "b.pdf"])  # b is at rank 4 > k.
    assert not partial.passed and partial.score == 0.5
    assert "b.pdf" in partial.detail
    none = s.score(case, ["x.pdf", "y.pdf"])
    assert not none.passed and none.score == 0.0


def test_recall_at_k_dedups_ranked_output() -> None:
    from bowhead_core.evalkit import RecallAtKScorer

    s = RecallAtKScorer(k=2)
    case = EvalCase(id="q", input="q", expected=["b.pdf"])
    # Multiple chunks from one file must not push b out of top-k: after deduplication b is second.
    r = s.score(case, ["a.pdf", "a.pdf", "a.pdf", "b.pdf"])
    assert r.passed and r.data["top"] == ["a.pdf", "b.pdf"]


def test_mrr_rank_and_k_cutoff() -> None:
    from bowhead_core.evalkit import MRRScorer

    case = EvalCase(id="q", input="q", expected=["t.pdf"])
    assert MRRScorer().score(case, ["t.pdf", "x"]).score == 1.0
    r2 = MRRScorer().score(case, ["x", "t.pdf"])
    assert r2.passed and r2.score == 0.5 and r2.data["rank"] == 2
    miss = MRRScorer().score(case, ["x", "y"])
    assert not miss.passed and miss.score == 0.0
    cutoff = MRRScorer(k=1).score(case, ["x", "t.pdf"])  # rank2 > k=1 → 0
    assert not cutoff.passed and cutoff.score == 0.0


def test_retrieval_scorers_edge_inputs() -> None:
    from bowhead_core.evalkit import MRRScorer, RecallAtKScorer

    empty = EvalCase(id="q", input="q", expected=[])
    assert not RecallAtKScorer().score(empty, ["a"]).passed
    assert not MRRScorer().score(empty, ["a"]).passed
    scalar_exp = EvalCase(id="q", input="q", expected="a.pdf")  # Accept a scalar for a single target.
    assert RecallAtKScorer().score(scalar_exp, ["a.pdf"]).passed
    assert MRRScorer().score(scalar_exp, None).score == 0.0  # Tolerate None output.
