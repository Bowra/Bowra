from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.results_read_model import (
    RESULT_SEARCH_ALIASES_V1,
    artifact_sort_tuple,
    compare_sort_tuples,
    derive_artifact_viewer_profile,
    normalize_results_query,
    project_artifact_result,
    project_code_result,
    project_report_result,
    sort_projected_results,
)


def _artifact(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": "art_data",
        "filename": "季度 数据.csv",
        "mime": "text/csv",
        "size": 2048,
        "source": "bi_fetch_rows",
        "created_at": "2026-08-24T12:00:00+00:00",
        "kind": "tool_data",
        "trust": "trusted",
        "producer_kind": "bi_fetch_rows",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.unit
def test_results_query_normalization_and_public_alias_contract() -> None:
    assert normalize_results_query("  ＱＵＡＲＴＥＲ 数据  ") == "quarter 数据"
    assert all(aliases for aliases in RESULT_SEARCH_ALIASES_V1.values())
    assert {key.split(":", 1)[0] for key in RESULT_SEARCH_ALIASES_V1} == {
        "type",
        "source",
        "role",
    }


@pytest.mark.unit
def test_artifact_projection_matches_frozen_group_viewer_and_capability_semantics() -> None:
    data = project_artifact_result(_artifact(), "")
    upload = project_artifact_result(
        _artifact(id="art_upload", kind="user_upload", source="upload", mime="image/png"),
        "",
    )
    svg = project_artifact_result(
        _artifact(id="art_svg", filename="active.svg", mime="image/svg+xml"),
        "",
    )
    pptx = project_artifact_result(
        _artifact(
            id="art_pptx",
            filename="deck.pptx",
            mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        "",
    )

    assert (data.primary_group, data.secondary_group, data.viewer_profile) == (
        "evidence",
        "data",
        "table",
    )
    assert "reference_in_chat" in data.capabilities
    assert data.artifact_metadata is not None
    assert data.artifact_metadata.mime == "text/csv"
    assert data.artifact_metadata.size == 2048
    assert data.artifact_metadata.producer_kind == "bi_fetch_rows"
    assert (
        "reference_in_chat"
        not in project_artifact_result(
            _artifact(),
            "",
            writable=False,
        ).capabilities
    )
    assert upload.secondary_group == "input"
    assert "manage_in_files" in upload.capabilities
    assert svg.viewer_profile == "unsupported"
    assert svg.route.default_mode == "details"
    report_only_svg = project_artifact_result(
        _artifact(
            id="art_shared_svg",
            filename="active.svg",
            mime="image/svg+xml",
        ),
        "",
        writable=False,
        report_only=True,
    )
    assert "feedback_unsupported" not in report_only_svg.capabilities
    assert "manage_in_files" not in report_only_svg.capabilities
    assert pptx.viewer_profile == "document"
    assert "view_raw" not in pptx.capabilities
    for mime in (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ):
        binary = project_artifact_result(_artifact(mime=mime), "")
        assert "view_raw" not in binary.capabilities
        assert {"view_details", "download"} <= set(binary.capabilities)
    for mime in ("application/xml", "application/yaml", "application/javascript"):
        text = project_artifact_result(_artifact(mime=mime), "")
        assert "view_raw" in text.capabilities
    assert derive_artifact_viewer_profile("application/octet-stream", "safe.md") == "text"
    assert derive_artifact_viewer_profile("application/vnd.ms-powerpoint", "deck.ppt") == "document"
    assert (
        len(
            project_artifact_result(
                _artifact(filename="x" * 600),
                "",
            ).title,
        )
        == 500
    )


@pytest.mark.unit
def test_report_artifact_code_share_one_stable_sort() -> None:
    report = project_report_result(
        canvas_id="canvas_1",
        title="季度报告",
        occurred_at="2026-08-24T10:00:00+00:00",
        editable=True,
        query="",
    )
    deliverable = project_artifact_result(
        _artifact(
            id="art_deck",
            filename="deck.pptx",
            mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            kind="deliverable",
            created_at="2026-08-24T12:00:00+00:00",
        ),
        "",
    )
    data = project_artifact_result(_artifact(), "")
    code = project_code_result(
        session_id="session_1",
        occurred_at="2026-08-24T11:00:00+00:00",
        running=False,
        query="",
    )

    assert report is not None
    assert code is not None
    assert report.artifact_metadata is None
    assert code.artifact_metadata is None
    assert "reference_in_chat" in code.capabilities
    read_only_code = project_code_result(
        session_id="session_1",
        occurred_at="2026-08-24T11:00:00+00:00",
        running=False,
        query="",
        writable=False,
    )
    assert read_only_code is not None
    assert "reference_in_chat" not in read_only_code.capabilities
    ordered = sort_projected_results([data, code, deliverable, report])
    assert [item.object_type for item in ordered] == ["report", "artifact", "artifact", "code"]
    assert compare_sort_tuples(artifact_sort_tuple(_artifact(), ""), data.sort_tuple) == 0


@pytest.mark.unit
def test_search_is_title_then_fixed_alias_and_never_internal_producer() -> None:
    artifact = _artifact(producer_kind="secret_internal_tool")

    assert project_artifact_result(artifact, "季度").sort_tuple[0] == 1
    assert project_artifact_result(artifact, "数据").sort_tuple[0] in {2, 4}
    assert artifact_sort_tuple(artifact, "secret_internal_tool")[0] == 99
