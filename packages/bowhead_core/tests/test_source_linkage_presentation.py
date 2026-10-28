from __future__ import annotations

import json
from pathlib import Path

from bowhead_core.source_linkage_presentation import (
    MAX_JSON_SAFE_INTEGER,
    normalize_nonnegative_count,
    project_source_linkage,
)

_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/report_trust/b1a_source_linkage_counts_v1.json"


def test_source_linkage_fixture() -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["contract"] == "source-linkage-presentation/B1a-v1"
    for case in fixture["cases"]:
        presentation = project_source_linkage(case["input"])
        assert {
            "citation_count": presentation.citation_count,
            "figure_review_count": presentation.figure_review_count,
            "source_count": presentation.source_count,
            "visible": presentation.visible,
        } == case["expected"], case["name"]


def test_normalize_count_rejects_python_only_non_json_shapes() -> None:
    assert normalize_nonnegative_count(False) is None
    assert normalize_nonnegative_count(float("nan")) is None
    assert normalize_nonnegative_count(float("inf")) is None
    assert normalize_nonnegative_count(object()) is None
    assert normalize_nonnegative_count(MAX_JSON_SAFE_INTEGER) == MAX_JSON_SAFE_INTEGER
    assert normalize_nonnegative_count(MAX_JSON_SAFE_INTEGER + 1) is None
