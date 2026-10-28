"""Provenance and proposal boundaries; semantic correctness is not inferred."""

from __future__ import annotations

import json

import pytest
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, prepare_planning_context, review_planning_response
from test_document_pptx_text import _deck, _parts, _rewrite

pytestmark = pytest.mark.unit


@pytest.fixture
def case():
    source = _deck()
    instruction = "Use Quarter two and the new open-case figures. Keep the region labels."
    data = (JsonEvidenceSource("counts.json", b'{"period":"Quarter two","values":[53,21],"other_metric":53,"empty":null,"zero":0}'),)
    context = prepare_planning_context(source, instruction, data)
    payload = json.loads(context.payload)
    slots = {slot["text"]: slot["id"] for slot in payload["slots"]}
    facts = {fact["path"]: fact["id"] for fact in payload["facts"]}
    changes = {
        slots["Quarter one"]: [{"ref": facts["/period"]}],
        slots["42"]: [{"ref": facts["/values/0"]}],
        slots["17"]: [{"ref": facts["/values/1"]}],
    }
    proposal = {
        "action": "propose",
        "updates": [{"slot": key, "parts": value} for key, value in changes.items()],
        "keep": [key for key in slots.values() if key not in changes],
    }
    return source, instruction, data, context, proposal, slots, facts


def _review(case, proposal=None, **overrides):
    source, instruction, data, context, value, _, _ = case
    return review_planning_response(
        overrides.get("source", source),
        overrides.get("instruction", instruction),
        overrides.get("data", data),
        json.dumps(value if proposal is None else proposal),
        expected_context_digest=context.digest,
    )


def test_copy_proposal_reconstructs_values_and_never_self_approves(case):
    review = _review(case)
    assert {item.before: item.after for item in review.changes} == {"Quarter one": "Quarter two", "42": "53", "17": "21"}
    assert all(item.evidence_refs for item in review.changes)
    assert review.semantic_status == "unapproved"
    assert review.action == "propose" and review.question is None


def test_numerically_equal_wrong_metric_is_not_semantically_certified(case):
    proposal = case[4]
    proposal["updates"][1]["parts"] = [{"ref": case[6]["/other_metric"]}]
    review = _review(case)
    assert review.changes[1].after == "53"
    assert review.changes[1].evidence_refs == (case[6]["/other_metric"],)
    assert review.semantic_status == "unapproved"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b'{"a":0.1,"b":0.2}', "0.3"),
        (b'{"a":1e100,"b":1.' + b"1" * 99 + b'e-100,"c":-1e100}', "0." + "0" * 99 + "1" * 100),
    ],
)
def test_sum_uses_exact_decimal_arithmetic(case, content, expected):
    source, instruction = case[:2]
    data = (JsonEvidenceSource("fraction.json", content),)
    context = prepare_planning_context(source, instruction, data)
    altered = (*case[:2], data, context, *case[4:])
    proposal = case[4]
    proposal["updates"] = [{"slot": case[5]["42"], "parts": [{"sum": [fact["id"] for fact in json.loads(context.payload)["facts"]]}]}]
    proposal["keep"] = [slot["id"] for slot in json.loads(context.payload)["slots"] if slot["id"] != case[5]["42"]]
    assert _review(altered).changes[0].after == expected


@pytest.mark.parametrize("changed", ["source", "instruction", "data"])
def test_replay_is_bound_to_all_exact_inputs(case, changed):
    values = {
        "source": _deck(mixed=True),
        "instruction": case[1] + " Also update owners.",
        "data": (JsonEvidenceSource("counts.json", b'{"values":[54,21]}'),),
    }
    with pytest.raises(OfficePackageError, match="planning_context_changed"):
        _review(case, **{changed: values[changed]})


@pytest.mark.parametrize("kind", ["missing", "overlap", "duplicate", "unknown", "blocked"])
def test_every_slot_is_accounted_for_without_widening_edit_support(case, kind):
    proposal = case[4]
    if kind == "missing":
        proposal["keep"].pop()
    elif kind == "overlap":
        proposal["keep"].append(proposal["updates"][0]["slot"])
    elif kind == "duplicate":
        proposal["updates"].append(proposal["updates"][0])
    elif kind == "unknown":
        proposal["updates"][0]["slot"] = "s999"
    else:
        source = _deck(mixed=True)
        case = (source, case[1], case[2], prepare_planning_context(source, case[1], case[2]), *case[4:])
    with pytest.raises(OfficePackageError, match="planning_invalid_update|planning_incomplete_scope"):
        _review(case)


@pytest.mark.parametrize(
    "part",
    [
        {"ref": "absent"},
        {"sum": []},
        {"sum": ["f1", "f1"]},
        {"ref": "f4"},
        {"sum": ["f0", "f1"]},
        {"quote": "Not user text"},
        "99",
        {"xpath": "//t"},
        {"ref": "f1", "approve": True},
        {"literal": "99"},
        {"literal": "fabricated text"},
        {"literal": "Quarter one", "approved": True},
        {"literal": None},
        {"literal": {"ref": "f1"}},
    ],
)
def test_unknown_null_fabricated_or_executable_content_is_rejected(case, part):
    case[4]["updates"][1]["parts"] = [part]
    with pytest.raises(OfficePackageError):
        _review(case)


def test_instruction_quotes_are_reconstructed_but_not_role_authority(case):
    case[4]["updates"][0]["parts"] = [{"quote": "Quarter two"}]
    review = _review(case)
    assert review.changes[0].after == "Quarter two"
    assert review.changes[0].evidence_refs[0].startswith("instruction:")
    assert review.semantic_status == "unapproved"


@pytest.mark.parametrize("literal", ["Quarter ", {"literal": "Quarter "}])
def test_retained_literal_has_the_same_provenance_check_in_both_encodings(case, literal):
    case[4]["updates"][0]["parts"] = [literal, {"quote": "two"}]
    review = _review(case)
    assert review.changes[0].after == "Quarter two"
    assert review.semantic_status == "unapproved"


def test_overlapping_instruction_quote_occurrences_are_ambiguous(case):
    instruction = "Replace the title using aaaa."
    context = prepare_planning_context(case[0], instruction, case[2])
    case[4]["updates"][0]["parts"] = [{"quote": "aaa"}]
    with pytest.raises(OfficePackageError, match="planning_quote_mismatch"):
        review_planning_response(case[0], instruction, case[2], json.dumps(case[4]), expected_context_digest=context.digest)


def test_missing_basis_question_is_not_falsely_converted_into_an_edit(case):
    proposal = {
        "action": "ask",
        "reason": "comparison",
        "slots": [case[5]["42"]],
        "question": "Should growth compare with the prior quarter or the same quarter last year?",
    }
    review = _review(case, proposal)
    assert not review.changes and not review.retained_slot_ids
    assert review.reason == "comparison" and review.semantic_status == "unapproved"


@pytest.mark.parametrize(
    "payload",
    [
        '{"action":"ask","action":"propose"}',
        '{"action":"propose","updates":[],"keep":[],"approved":true}',
        '{"action":"ask","reason":"source","slots":[],"question":"Which source?"}',
        '{"action":"ask","reason":"source","slots":["s0"],"question":"Which\\u0000source?"}',
        '{"action":"ask","reason":"source","slots":["s0"],"question":"Which\\ud800source?"}',
        "[]",
        "NaN",
    ],
)
def test_response_protocol_rejects_duplicate_keys_and_extra_authority(case, payload):
    with pytest.raises(OfficePackageError):
        review_planning_response(*case[:3], payload, expected_context_digest=case[3].digest)


@pytest.mark.parametrize(
    "content", [b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":1e999}', b'{"x":"\\ud800"}', b'{"x":' + b"[" * 14 + b"0" + b"]" * 14 + b"}"]
)
def test_evidence_rejects_ambiguous_or_unbounded_json(case, content):
    with pytest.raises(OfficePackageError):
        prepare_planning_context(case[0], case[1], (JsonEvidenceSource("data.json", content),))


def test_arbitrary_labels_json_pointer_escaping_and_zero_survive_without_aliases(case):
    data = (JsonEvidenceSource("sensors.json", b'{"m/~":{"opaque-label":0,"other":false}}'),)
    payload = json.loads(prepare_planning_context(case[0], case[1], data).payload)
    assert payload["facts"][0] == {"id": "f0", "source": 0, "path": "/m~1~0/opaque-label", "type": "number", "value": "0"}
    assert payload["facts"][1]["type"] == "boolean"


def test_large_evidence_is_not_silently_truncated(case):
    data = (JsonEvidenceSource("large.json", json.dumps(list(range(513))).encode()),)
    with pytest.raises(OfficePackageError, match="planning_input_limit"):
        prepare_planning_context(case[0], case[1], data)


@pytest.mark.parametrize("prefix", ["Downtime: ", "Load: ", "Latency: "])
def test_numeric_caption_cannot_lose_its_fixed_label(case, prefix):
    source = case[0]
    part = "ppt/slides/slide1.xml"
    xml = _parts(source)[part].replace(b"Stable grouped caption", (prefix + "17").encode())
    source = _rewrite(source, {part: xml})
    context = prepare_planning_context(source, case[1], case[2])
    slots = json.loads(context.payload)["slots"]
    target = next(slot["id"] for slot in slots if slot["text"] == prefix + "17")
    proposal = {
        "action": "propose",
        "updates": [{"slot": target, "parts": [{"ref": "f1"}]}],
        "keep": [slot["id"] for slot in slots if slot["id"] != target],
    }
    review = review_planning_response(source, case[1], case[2], json.dumps(proposal), expected_context_digest=context.digest)
    assert review.changes[0].after == prefix + "53"
    target_slot = next(slot for slot in slots if slot["id"] == target)
    assert target_slot["fixed_prefix"] == prefix and target_slot["editable_text"] == "17"
    proposal["updates"][0]["parts"].insert(0, "Replacement label: ")
    with pytest.raises(OfficePackageError, match="planning_ungrounded_literal"):
        review_planning_response(source, case[1], case[2], json.dumps(proposal), expected_context_digest=context.digest)
