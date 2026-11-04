"""Finite Word run controls bind collection to the real original source."""

import pytest
from bowhead_core.documents.authoring_formats import WORD_PROFILE
from bowhead_core.documents.authoring_protocol import PROFILE, control, digest, read_control
from bowhead_core.documents.package_io import OfficePackageError

pytestmark = pytest.mark.unit


def test_word_render_binds_both_original_and_candidate_without_changing_old_profile(tmp_path):
    original, candidate = b"original-docx", b"candidate-docx"
    encoded = control("render", (candidate, original), parent_sha256=digest(original), profile=WORD_PROFILE)
    (tmp_path / "control.json").write_bytes(encoded)
    (tmp_path / "input-0").write_bytes(candidate)
    (tmp_path / "input-1").write_bytes(original)
    record, material = read_control(tmp_path, digest(encoded))
    assert record["profile"] == WORD_PROFILE and material == (candidate, original)
    assert PROFILE == "pptx-script-worker-v1"
    (tmp_path / "input-1").write_bytes(b"different-source")
    with pytest.raises(OfficePackageError):
        read_control(tmp_path, digest(encoded))


@pytest.mark.parametrize(
    "materials,parent",
    [((b"candidate",), b"original"), ((b"candidate", b"original"), b"different"), ((b"candidate", b"original", b"extra"), b"original")],
)
def test_word_collection_cannot_omit_replace_or_expand_the_source(materials, parent):
    with pytest.raises(OfficePackageError):
        control("render", materials, parent_sha256=digest(parent), profile=WORD_PROFILE)


def test_word_reader_has_its_actual_block_selector_not_slide_parameters():
    source = b"source"
    assert control("inspect", (source, b'{"offset":0,"limit":20,"detail":"content"}'), parent_sha256=digest(source), profile=WORD_PROFILE)
    with pytest.raises(OfficePackageError):
        control("inspect", (source, b'{"slides":[1],"detail":"objects"}'), parent_sha256=digest(source), profile=WORD_PROFILE)
