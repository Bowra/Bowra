from __future__ import annotations

import json
from pathlib import Path

import pytest
from bowhead_gateway.results_contracts import (
    ResultItemDTOV1,
    ResultsCapabilitiesResponseV1,
)
from pydantic import ValidationError

_FIXTURE = Path(__file__).parent / "fixtures" / "results_scale_v1.json"


def _load_fixture() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_results_scale_fixture_is_versioned_and_covers_frozen_sizes() -> None:
    fixture = _load_fixture()

    assert fixture["fixture_version"] == 1
    assert [item["count"] for item in fixture["scales"]] == [0, 10, 30, 135, 500, 2000]
    assert fixture["benchmark"] == {
        "warmup_iterations": 20,
        "measured_iterations": 200,
        "page_size": 50,
    }


@pytest.mark.unit
def test_results_scale_generator_contract_is_deterministic() -> None:
    generator = _load_fixture()["generator"]

    first = generator["artifact_id_pattern"].format(index=1)
    last = generator["artifact_id_pattern"].format(index=2000)
    search_titles = [f"{generator['search_title_prefix']}-{index:04d}" for index in range(2000) if index % generator["search_title_every"] == 0]

    assert first == "art_results_0001"
    assert last == "art_results_2000"
    assert len(search_titles) == 118
    assert len(set(search_titles)) == len(search_titles)


@pytest.mark.unit
def test_result_item_dto_v1_accepts_all_versioned_samples() -> None:
    samples = _load_fixture()["dto_samples"]

    parsed = [ResultItemDTOV1.model_validate(sample) for sample in samples]

    assert [item.object_type for item in parsed] == ["report", "artifact", "code"]
    assert [item.position for item in parsed] == [1, 2, 3]
    assert parsed[1].route.type == "artifact"
    assert parsed[1].artifact_metadata is not None
    assert parsed[1].artifact_metadata.origin == "query"


@pytest.mark.unit
def test_result_item_dto_v1_rejects_contract_drift() -> None:
    sample = _load_fixture()["dto_samples"][0] | {"unknown_field": True}

    with pytest.raises(ValidationError):
        ResultItemDTOV1.model_validate(sample)


@pytest.mark.unit
def test_result_item_dto_v1_requires_metadata_only_for_artifacts() -> None:
    report, artifact, _ = _load_fixture()["dto_samples"]

    with pytest.raises(ValidationError):
        ResultItemDTOV1.model_validate(report | {"artifact_metadata": artifact["artifact_metadata"]})
    with pytest.raises(ValidationError):
        ResultItemDTOV1.model_validate(artifact | {"artifact_metadata": None})


@pytest.mark.unit
def test_capability_contract_is_fixed_to_v1_and_page_size_50() -> None:
    value = ResultsCapabilitiesResponseV1(
        workspace=True,
        complete_list=False,
        scale_nav=False,
    )

    assert value.model_dump() == {
        "dto_version": 1,
        "workspace": True,
        "complete_list": False,
        "scale_nav": False,
        "max_page_size": 50,
    }
    with pytest.raises(ValidationError):
        ResultsCapabilitiesResponseV1.model_validate(value.model_dump() | {"max_page_size": 100})
