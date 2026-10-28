"""Private draft compilation through actual packages and provenance checks."""

from __future__ import annotations

import hashlib
import json

import pytest
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.pptx_draft import compile_pptx_draft
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, prepare_planning_context
from bowhead_core.documents.pptx_text import inventory_pptx_text
from test_document_pptx_plan import case as case
from test_document_pptx_text import _deck, _parts, _rewrite

pytestmark = pytest.mark.unit


def _compile(case, **overrides):
    return compile_pptx_draft(
        overrides.get("source", case[0]),
        overrides.get("instruction", case[1]),
        overrides.get("data", case[2]),
        overrides.get("response", json.dumps(case[4])),
        expected_context_digest=overrides.get("digest", case[3].digest),
    )


def test_compiler_changes_only_declared_text_and_exposes_source_evidence(case):
    before = _parts(case[0])
    result = _compile(case)
    manifest = result.manifest
    assert hashlib.sha256(result.candidate).hexdigest() == manifest.edit_receipt.candidate_sha256
    assert hashlib.sha256(case[0]).hexdigest() == manifest.edit_receipt.source_sha256
    assert manifest.state == "unrendered" and manifest.semantic_status == "unapproved"
    assert manifest.edit_receipt.visual_verdict == "not_run"
    assert {change.before: change.after for change in manifest.changes} == {"Quarter one": "Quarter two", "42": "53", "17": "21"}
    count = next(change for change in manifest.changes if change.before == "42")
    assert count.slide == 2 and count.cell == (1, 1)
    assert count.evidence[0].source_name == "counts.json"
    assert count.evidence[0].source_sha256 == hashlib.sha256(case[2][0].content).hexdigest()
    assert count.evidence[0].pointer == "/values/0" and count.evidence[0].value == "53"
    after = _parts(result.candidate)
    assert before.keys() == after.keys()
    assert {name for name in before if before[name] != after[name]} == set(manifest.edit_receipt.changed_parts)
    assert _parts(case[0]) == before
    assert _compile(case) == result
    assert manifest.digest == hashlib.sha256(manifest.to_json().encode()).hexdigest()


@pytest.mark.parametrize("changed", ["source", "instruction", "data", "digest"])
def test_stale_inputs_cannot_compile_under_an_earlier_context(case, changed):
    alternatives = {
        "source": _deck(mixed=True),
        "instruction": case[1] + " Keep all numbers unchanged.",
        "data": (JsonEvidenceSource("counts.json", b'{"values":[99,88]}'),),
        "digest": "0" * 64,
    }
    with pytest.raises(OfficePackageError, match="planning_context_changed"):
        _compile(case, **{changed: alternatives[changed]})


def test_model_approval_flag_never_becomes_permission(case):
    with pytest.raises(OfficePackageError, match="planning_invalid_response"):
        _compile(case, response=json.dumps({**case[4], "approved": True}))


def test_question_is_not_executed_as_a_candidate(case):
    question = {"action": "ask", "reason": "comparison", "slots": ["s0"], "question": "Which comparison period?"}
    with pytest.raises(OfficePackageError, match="draft_requires_proposal"):
        _compile(case, response=json.dumps(question))


def test_no_changes_are_not_presented_as_a_new_candidate(case):
    empty = {"action": "propose", "updates": [], "keep": [slot["id"] for slot in json.loads(case[3].payload)["slots"]]}
    with pytest.raises(OfficePackageError, match="draft_has_no_changes"):
        _compile(case, response=json.dumps(empty))


def test_same_number_wrong_metric_stays_unapproved_and_visible_in_summary(case):
    case[4]["updates"][1]["parts"] = [{"ref": case[6]["/other_metric"]}]
    result = _compile(case)
    assert result.manifest.changes[1].after == "53"
    assert result.manifest.changes[1].evidence[0].pointer == "/other_metric"
    assert result.manifest.semantic_status == "unapproved"


def test_quote_evidence_is_reread_from_canonical_instruction(case):
    case[4]["updates"][0]["parts"] = [{"quote": "Quarter two"}]
    evidence = _compile(case).manifest.changes[0].evidence[0]
    assert evidence.kind == "instruction" and evidence.pointer is None
    assert evidence.quote_start == case[1].index("Quarter two")
    assert evidence.source_sha256 == hashlib.sha256(case[1].encode()).hexdigest()
    assert evidence.value == "Quarter two"


def test_sum_summary_includes_all_operand_sources(case):
    case[4]["updates"][1]["parts"] = [{"sum": [case[6]["/values/0"], case[6]["/values/1"]]}]
    change = _compile(case).manifest.changes[1]
    assert change.after == "74"
    assert [(item.pointer, item.value) for item in change.evidence] == [("/values/0", "53"), ("/values/1", "21")]


def test_uneditable_retained_text_is_reported_without_reformatting(case):
    source = _deck(mixed=True)
    context = prepare_planning_context(source, case[1], case[2])
    proposal = case[4]
    proposal["keep"].append(proposal["updates"].pop(0)["slot"])
    result = _compile(case, source=source, digest=context.digest)
    assert result.manifest.retained_unsupported_text[0].text == "Quarter one"
    assert result.manifest.retained_unsupported_text[0].reason == "mixed_run_styles"
    assert _parts(source)["ppt/slides/slide1.xml"] == _parts(result.candidate)["ppt/slides/slide1.xml"]


def test_complex_chart_is_not_silently_preserved_in_an_ordinary_draft(case):
    with pytest.raises(OfficePackageError, match="draft_template_unsupported"):
        _compile(case, source=_deck(chart=True))


def test_external_relationship_does_not_enter_private_draft_renderer(case):
    part = "ppt/slides/_rels/slide1.xml.rels"
    relations = _parts(case[0])[part].replace(
        b"</Relationships>",
        b'<Relationship Id="external1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        b'Target="https://example.test/picture.png" TargetMode="External"/></Relationships>',
    )
    source = _rewrite(case[0], {part: relations})
    with pytest.raises(OfficePackageError, match="draft_template_unsupported"):
        _compile(case, source=source)


def test_candidate_cannot_change_blocked_text(case):
    source = _deck(mixed=True)
    context = prepare_planning_context(source, case[1], case[2])
    with pytest.raises(OfficePackageError, match="planning_invalid_update"):
        _compile(case, source=source, digest=context.digest)


def test_candidate_retains_all_other_values(case):
    result = _compile(case)
    original = {slot.slot_id: slot.text for slot in inventory_pptx_text(case[0]).slots}
    final = {slot.slot_id: slot.text for slot in inventory_pptx_text(result.candidate).slots}
    for slot_id in result.manifest.retained_slot_ids:
        assert final[slot_id] == original[slot_id]
