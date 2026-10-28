"""Answer criteria cannot certify execution or turn absent grading into success."""

import pytest
from bowhead_core.agent_eval_quality import answer_quality, quality_summary

pytestmark = pytest.mark.unit

POINTS = {
    "points": [
        {"id": "total", "kind": "judgment", "text": "The total is 300", "state": "confirmed"},
        {"id": "source", "kind": "data_fact", "text": "Name the data source", "state": "confirmed"},
        {"id": "query", "kind": "required_action", "state": "confirmed"},
    ]
}


@pytest.mark.parametrize("judge", [None, "null", "invalid", {}, {"passed": True}])
def test_an_execution_pass_without_point_grades_remains_ungraded(judge):
    quality = answer_quality(POINTS, judge)
    assert quality["state"] == "ungraded"
    assert quality["met"] == 0 and quality["ungraded"] == 2
    assert all(point["met"] is None and point["advisory"] for point in quality["points"])


def test_wrong_total_is_advisory_and_partial_grades_do_not_cover_missing_criteria():
    quality = answer_quality(POINTS, {"points": [{"id": "total", "met": False, "reason": "wrong_total"}]})
    assert quality["state"] == "partial"
    assert quality["unmet"] == 1 and quality["ungraded"] == 1
    assert quality["points"][0]["advisory"] is True
    assert quality["points"][0]["calibrated"] is None
    assert quality["points"][0]["reason"] == "wrong_total"


def test_unknown_judge_ids_and_legacy_cases_do_not_create_answer_evidence():
    assert answer_quality({}, {"points": [{"id": "unknown", "met": True}]})["state"] == "not_configured"
    quality = answer_quality(POINTS, {"points": [{"id": "unknown", "met": True}, {"id": "total", "met": True}, {"id": "source", "met": False}]})
    assert quality["state"] == "graded" and quality["met"] == quality["unmet"] == 1
    assert quality_summary(quality["points"])["point_assessments"] == 2
