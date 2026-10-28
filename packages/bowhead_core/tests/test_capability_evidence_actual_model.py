"""`actual_model_id` 必须从探针一路活到读取面。

116 实测：三条失败全是 HTTP 200，其中两条 `model_identity_mismatch`。探针**当场就拿到了**
provider 自报的 model id（`payload["model"]`），比对不上就判失败，然后把它丢了 —— 于是
运维只看到「身份不匹配」，既不知道对面返回了什么，也就无法判断是配置写错了还是我们的
严格相等判据太死（厂商普遍返回带版本号的 id）。

这条钉住写入→投影的整条路。只测「表里有这列」是不够的：列在、但读取路径没 SELECT 它，
或投影没带出来，症状与完全没做一模一样。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from bowhead_core.team_llm_evidence import EvidenceRecord, project_latest_evidence

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _record(**overrides) -> EvidenceRecord:
    base = {
        "evidence_id": "lve_1",
        "config_signature": "sig-1",
        "outcome": "failed",
        "safe_code": "model_identity_mismatch",
        "observed_at": NOW,
        "fresh_until": NOW + timedelta(days=1),
        "actual_model_id": "Doubao-Seed-2.0-pro-250115",
    }
    return EvidenceRecord(**{**base, **overrides})


def test_projection_carries_the_observed_model_id() -> None:
    projection = project_latest_evidence(
        [_record()],
        current_signature="sig-1",
        now=NOW,
    )

    assert projection.fact_state == "failed"
    assert projection.safe_code == "model_identity_mismatch"
    assert projection.actual_model_id == "Doubao-Seed-2.0-pro-250115"


def test_missing_value_reads_as_not_recorded_not_as_match() -> None:
    """0018 之前落的行没有这个值 —— 必须是 None，不能被当成「一致」。"""
    projection = project_latest_evidence(
        [_record(actual_model_id=None, outcome="verified", safe_code="ok")],
        current_signature="sig-1",
        now=NOW,
    )

    assert projection.fact_state == "verified"
    assert projection.actual_model_id is None


@pytest.mark.parametrize("signature", ["sig-other", "sig-1"])
def test_value_follows_the_selected_evidence_row(signature: str) -> None:
    """投影选的是哪一行，`actual_model_id` 就得是哪一行的 —— 不能串行。"""
    rows = [
        _record(evidence_id="lve_old", config_signature="sig-other", actual_model_id="old-model"),
        _record(evidence_id="lve_new", config_signature="sig-1", actual_model_id="new-model"),
    ]

    projection = project_latest_evidence(rows, current_signature=signature, now=NOW)

    expected = "old-model" if signature == "sig-other" else "new-model"
    assert projection.actual_model_id == expected
