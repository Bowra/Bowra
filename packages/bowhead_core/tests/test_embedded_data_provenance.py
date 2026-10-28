from __future__ import annotations

import json
from pathlib import Path

from bowhead_core.embedded_data_provenance import project_embedded_data_provenance

_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/report_trust/b1a_embedded_data_provenance_v1.json"


def test_embedded_data_provenance_fixture() -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["contract"] == "embedded-data-provenance/B1a-v1"
    for case in fixture["cases"]:
        presentation = project_embedded_data_provenance(case["input"])
        assert {
            "status": presentation.status,
            "source_indexes": list(presentation.source_indexes),
            "has_sources": presentation.has_sources,
        } == case["expected"], case["name"]


def test_embedded_data_rejects_cycles_and_non_json_containers() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    assert project_embedded_data_provenance({"data": cyclic}).status == "computed_unchecked"
    assert project_embedded_data_provenance({"data": (1, 2)}).status == "computed_unchecked"
    assert project_embedded_data_provenance({"data": {"x": float("nan")}}).status == "computed_unchecked"
    assert project_embedded_data_provenance({"data": {1: "not a JSON key"}}).status == "computed_unchecked"


def test_embedded_data_rejects_depth_and_node_overflow() -> None:
    nested: list[object] = []
    cursor = nested
    for _ in range(65):
        child: list[object] = []
        cursor.append(child)
        cursor = child
    assert project_embedded_data_provenance({"data": nested}).status == "computed_unchecked"
    assert project_embedded_data_provenance({"data": [None] * 100_001}).status == "computed_unchecked"
