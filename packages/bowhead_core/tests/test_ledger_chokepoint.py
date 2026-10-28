"""report-consistency-gate v3 Phase 3b-wire — chokepoint 错误文案。

完整 gate 行为(shadow/enforce/retry)走真 LLM dogfood 验(LLM 行为改动)；此处只锁
`_ledger_error_text` 产出对 LLM 可执行的、指名道姓的清单。
"""

from bowhead_core.report_ledger import ReconcileResult
from bowhead_core.subagent_return_tool import _ledger_error_text


def test_error_text_names_rejected_and_unauthorized():
    r = ReconcileResult(
        rejected_facts=[
            {"key": "avg", "label": "户均", "value": 163.0, "formula": "3.57*10000/8438", "reason": "recompute_mismatch(formula→4.23 vs value 163)"}
        ],
        unauthorized=[{"field": "exec_summary", "value": 43.9, "raw": "43.9%"}],
    )
    txt = _ledger_error_text(r)
    assert "reconcile_failed" in txt
    assert "户均" in txt and "recompute_mismatch" in txt  # Derived-value verification failed; give the reason.
    assert "43.9%" in txt and "exec_summary" in txt  # Unauthorized number; identify its field.
    assert "重新 subagent_return" in txt  # Give the next action.


def test_error_text_empty_clean():
    txt = _ledger_error_text(ReconcileResult())
    assert "reconcile_failed" in txt and "重新 subagent_return" in txt
