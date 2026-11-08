"""Reader boundaries identify their units without changing source evidence."""

import csv
import io
import json

import pytest
from bowhead_core.documents.evidence import MAX_DATA_BYTES, inspect_evidence, prepare_evidence
from bowhead_core.documents.package_io import OfficePackageError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("records,returned", [(512, 512), (513, 21)])
def test_csv_coverage_distinguishes_complete_evidence_from_prefix(records, returned):
    source = b"001\n" * records
    observed = inspect_evidence("data.csv", source)
    info = observed.read_info
    assert info["coverage"] == {"scope": "file", "unit": "csv_records", "returned": returned, "total": records}
    assert info["view"] == ("complete" if records == returned else "partial")
    assert info["full_input_available"] is None
    value = json.loads(observed.content)
    if records == returned:
        assert value == [["001"]] * records
        assert observed.content == prepare_evidence("data.csv", source).content
        assert info["limit"] is None
    else:
        assert value["records"] == [["001"]] * returned
        assert value["coverage"]["complete"] is False
        assert info["limit"] == {"scope": "model_preview", "kind": "preview_records", "actual": 22, "maximum": 21, "unit": "csv_records"}
        with pytest.raises(OfficePackageError, match="document_data_limit"):
            prepare_evidence("data.csv", source)


@pytest.mark.parametrize(
    "case,kind,unit,maximum",
    [
        ("input", "input_bytes", "bytes", MAX_DATA_BYTES),
        ("filename", "filename_characters", "characters", 256),
        ("wide", "preview_cells", "cells", 512),
        ("field", "preview_record_bytes", "bytes", 64_000),
        ("json", "normalized_bytes", "bytes", 128_000),
        ("text", "normalized_bytes", "bytes", 128_000),
    ],
)
def test_limit_facts_describe_the_guard_that_refused_the_read(case, kind, unit, maximum):
    name, source = {
        "input": ("data.csv", b"x" * (MAX_DATA_BYTES + 1)),
        "filename": ("x" * 257, b"1"),
        "wide": ("data.csv", b",".join([b"x"] * 513)),
        "field": ("data.csv", b"x" * 128_000),
        "json": ("data.json", b" " * 128_001),
        "text": ("data.txt", b"x" * 128_000),
    }[case]
    with pytest.raises(OfficePackageError, match="document_data_limit") as caught:
        inspect_evidence(name, source)
    info = caught.value.read_info
    assert info["view"] == "none"
    assert info["limit"]["kind"] == kind
    assert info["limit"]["unit"] == unit
    assert info["limit"]["maximum"] == maximum
    assert info["limit"]["actual"] > maximum


def test_csv_field_limit_is_not_mislabeled_as_malformed_syntax():
    maximum = csv.field_size_limit()
    with pytest.raises(OfficePackageError, match="document_data_invalid") as caught:
        inspect_evidence("data.csv", b"header\n" + b"x" * (maximum + 1))
    assert caught.value.read_info["limit"] == {
        "scope": "parser",
        "kind": "csv_field_characters",
        "actual": None,
        "maximum": maximum,
        "unit": "characters",
    }
    assert csv.field_size_limit() == maximum
    with pytest.raises(OfficePackageError, match="document_data_invalid") as malformed:
        inspect_evidence("data.csv", b'header\n"unterminated')
    assert malformed.value.read_info["limit"] is None


def test_prefix_counts_multiline_records_and_does_not_hide_an_invalid_tail():
    source = b"\xef\xbb\xbfkey,value\n" + b'"two\nlines",001\n' * 300
    observed = inspect_evidence("data.csv", source)
    value = json.loads(observed.content)
    assert observed.read_info["coverage"]["total"] == 301
    assert value["records"][1] == ["two\nlines", "001"]
    with pytest.raises(OfficePackageError, match="document_data_invalid"):
        inspect_evidence("data.csv", source + b'"unfinished')


def test_formatted_empty_worksheet_extent_is_distinct_from_populated_cells():
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    workbook.active["A1"] = "one value"
    workbook.active["Z100"].font = Font(bold=True)
    output = io.BytesIO()
    workbook.save(output)
    with pytest.raises(OfficePackageError, match="document_data_limit") as caught:
        inspect_evidence("data.xlsx", output.getvalue())
    assert caught.value.read_info["limit"] == {
        "scope": "model_preview",
        "kind": "worksheet_extent",
        "actual": 2600,
        "maximum": 512,
        "unit": "extent_cells",
    }


@pytest.mark.parametrize("change", ["none", "legacy", "source", "metadata"])
def test_error_transport_validates_identity_and_bounded_metadata(change):
    from bowhead_core.documents.authoring_executor import _raise_fixed_error
    from bowhead_core.documents.authoring_protocol import PROFILE
    from bowhead_core.read_receipts import ReadLimit, read_info

    metadata = read_info(
        view="none",
        unit="bytes",
        returned=0,
        limit=ReadLimit(scope="parser", kind="input_bytes", actual=5, maximum=4, unit="bytes"),
    )
    record = {
        "kind": "authoring_error",
        "profile": PROFILE,
        "control_sha256": "a" * 64,
        "source_sha256": "b" * 64,
        "error_code": "document_data_limit",
        "read_info": metadata,
    }
    if change == "legacy":
        del record["read_info"]
    elif change == "source":
        record["source_sha256"] = "c" * 64
    elif change == "metadata":
        record["read_info"] = {**metadata, "unexpected_host_path": "/private/example"}
    expected = "document_authoring_output_invalid" if change in {"source", "metadata"} else "document_data_limit"
    with pytest.raises(OfficePackageError, match=expected) as caught:
        _raise_fixed_error(record, control_sha256="a" * 64, source_sha256="b" * 64)
    if change == "none":
        assert caught.value.read_info == metadata
    elif change == "legacy":
        assert caught.value.read_info is None
