from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.results_report_relations import project_report_relations


def _section(kind: str, payload: dict, **overrides: object) -> SimpleNamespace:
    values = {
        "kind": kind,
        "payload": payload,
        "hidden_for_chat_mode": False,
        "superseded": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.unit
def test_report_relation_projector_covers_typed_direct_and_closure_roles() -> None:
    relations = project_report_relations(
        [
            _section("chart", {"spec": {"data_artifact_id": "art_chart"}}),
            _section("data_table", {"meta": {"artifact_id": "art_table"}}),
            _section(
                "insight_card",
                {
                    "evidence": [
                        {"data_artifact_id": "art_legacy_chart"},
                        {"table_artifact_id": "art_detail"},
                        {"computation_artifact_id": "art_calc"},
                    ],
                },
            ),
            _section("html_artifact", {"artifact_id": "art_html"}),
            _section("image", {"artifact_id": "art_image"}),
            _section(
                "code_block",
                {
                    "input_artifact_ids": ["art_input"],
                    "output_artifacts": [{"artifact_id": "art_output"}],
                },
            ),
            _section(
                "design",
                {
                    "background": {"artifact_id": "art_background"},
                    "flattened_artifact_id": "art_flattened",
                },
            ),
        ],
        source_refs=[
            {
                "artifact_id": "art_source",
                "sub_sources": [{"artifact_id": "art_subsource"}],
            },
        ],
    )

    assert set(relations.closure_ids) == {
        "art_background",
        "art_calc",
        "art_chart",
        "art_detail",
        "art_flattened",
        "art_html",
        "art_image",
        "art_input",
        "art_legacy_chart",
        "art_output",
        "art_source",
        "art_subsource",
        "art_table",
    }
    assert set(relations.direct_ids) == {
        "art_calc",
        "art_chart",
        "art_detail",
        "art_html",
        "art_image",
        "art_legacy_chart",
        "art_output",
        "art_table",
    }
    assert relations.roles_for("art_chart") == ("report_chart_data",)
    assert relations.roles_for("art_legacy_chart") == ("report_chart_data",)
    assert relations.roles_for("art_output") == (
        "report_computation",
        "code_output",
    )
    assert relations.roles_for("art_input") == ("code_input",)
    assert relations.roles_for("art_source") == ("ordinary_source",)


@pytest.mark.unit
def test_report_relation_projector_keeps_superseded_in_closure_not_direct() -> None:
    relations = project_report_relations(
        [
            _section(
                "image",
                {"artifact_id": "art_old"},
                superseded=True,
            ),
            _section(
                "image",
                {"artifact_id": "art_hidden"},
                hidden_for_chat_mode=True,
            ),
        ],
    )

    assert relations.closure_ids == ("art_old",)
    assert relations.direct_ids == ()
    assert relations.roles_for("art_old") == ()


@pytest.mark.unit
def test_report_relation_projector_does_not_scan_unknown_payload_strings() -> None:
    relations = project_report_relations(
        [
            _section(
                "narrative",
                {
                    "artifact_id": "art_forged",
                    "nested": {"data_artifact_id": "art_nested"},
                    "markdown": "art_body",
                },
            ),
        ],
    )

    assert relations.closure_ids == ()
    assert relations.direct_ids == ()


@pytest.mark.unit
def test_report_relation_revision_is_order_independent_and_role_sensitive() -> None:
    first = project_report_relations(
        [
            _section("image", {"artifact_id": "art_image"}),
            _section("chart", {"spec": {"data_artifact_id": "art_data"}}),
        ],
    )
    reordered = project_report_relations(
        [
            _section("chart", {"spec": {"data_artifact_id": "art_data"}}),
            _section("image", {"artifact_id": "art_image"}),
        ],
    )
    superseded = project_report_relations(
        [
            _section(
                "chart",
                {"spec": {"data_artifact_id": "art_data"}},
                superseded=True,
            ),
            _section("image", {"artifact_id": "art_image"}),
        ],
    )

    assert first.projection_revision == reordered.projection_revision
    assert first.projection_revision != superseded.projection_revision
