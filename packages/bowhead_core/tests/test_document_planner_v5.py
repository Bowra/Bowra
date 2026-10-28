"""Versioned exact evidence selection, not semantic or visual acceptance."""

import hashlib
import json

import pytest
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.planning_resume import prepare_answer_context
from bowhead_core.documents.pptx_draft import compile_pptx_draft
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, prepare_planning_context, review_planning_response
from test_document_pptx_text import _deck

pytestmark = pytest.mark.unit
V5 = "pptx-grounded-plan-v5"


def test_retained_v4_prompt_bytes_are_immutable():
    from bowhead_core.documents.pptx_plan import SYSTEM_PROMPT, planning_system_prompt

    assert hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest() == "64904ee37fc51809db064d0ef33e707809401b106a1bfd7f950c13d8c6f50238"
    assert planning_system_prompt("pptx-grounded-plan-v4") == SYSTEM_PROMPT


def test_v5_exact_noop_retains_slot_without_inventing_an_edit():
    case = material(answer="Keep the North value at 42.")
    result = review(case, "42", [{"ref": "f0", "quote": "42"}])
    assert result.changes == ()
    assert len(result.retained_slot_ids) == len(case[4]["slots"])


def material(instruction="Set the title to Quarter two and the North value to 150.", answer="Use Quarter two; North is 150; keep South unchanged."):
    source = _deck()
    data = (JsonEvidenceSource("answer.json", json.dumps({"answer": answer}, ensure_ascii=False).encode()),)
    context = prepare_planning_context(source, instruction, data, profile=V5)
    payload = json.loads(context.payload)
    return source, instruction, data, context, payload


def proposal(payload, text, parts):
    slot = next(item["id"] for item in payload["slots"] if item["text"] == text)
    return json.dumps(
        {"action": "propose", "updates": [{"slot": slot, "parts": parts}], "keep": [s["id"] for s in payload["slots"] if s["id"] != slot]}
    )


def review(case, text, parts):
    source, instruction, data, context, payload = case
    return review_planning_response(source, instruction, data, proposal(payload, text, parts), expected_context_digest=context.digest, profile=V5)


def test_repeated_instruction_quote_is_not_missing_business_information():
    case = material(instruction="Set the title to Quarter two. Confirmed: Quarter two.")
    result = review(case, "Quarter one", [{"quote": "Quarter two"}])
    assert result.changes[0].after == "Quarter two"
    assert result.changes[0].evidence_refs == ("instruction:17:11",)
    source, instruction, data, _, payload = case
    old = prepare_planning_context(source, instruction, data)
    with pytest.raises(OfficePackageError, match="planning_quote_mismatch"):
        review_planning_response(
            source, instruction, data, proposal(payload, "Quarter one", [{"quote": "Quarter two"}]), expected_context_digest=old.digest
        )


@pytest.mark.parametrize("text,quote", [("Quarter one", "Quarter two"), ("42", "150")])
def test_long_answer_selects_only_exact_fragment_and_preserves_source(text, quote):
    case = material(answer="Confirmed: Quarter two; North is 150; Quarter two is the title.")
    source, instruction, data, context, payload = case
    result = compile_pptx_draft(
        source, instruction, data, proposal(payload, text, [{"ref": "f0", "quote": quote}]), expected_context_digest=context.digest, profile=V5
    )
    change = result.manifest.changes[0]
    assert change.after == quote
    evidence = change.evidence[0]
    assert evidence.kind == "data"
    assert evidence.source_name == "answer.json"
    assert evidence.source_sha256 == hashlib.sha256(data[0].content).hexdigest()
    assert evidence.pointer == "/answer"
    assert evidence.quote_start == json.loads(data[0].content)["answer"].index(quote)
    assert evidence.value == quote
    assert result.manifest.semantic_status == "unapproved"


@pytest.mark.parametrize(
    "part",
    [
        {"ref": "f0", "quote": "Quarter three"},
        {"ref": "f9", "quote": "150"},
        {"ref": "f0", "quote": ""},
        {"ref": "f0", "quote": 150},
        {"ref": "f0", "quote": "50"},
    ],
)
def test_fabricated_or_partial_evidence_is_rejected(part):
    with pytest.raises(OfficePackageError):
        review(material(), "42", [part])


@pytest.mark.parametrize(
    "parts", [[{"ref": "f0"}], [{"ref": "f0", "quote": "North is 150"}], [{"ref": "f0", "quote": "150"}, {"ref": "f0", "quote": "150"}]]
)
def test_numeric_slot_rejects_whole_answer_and_composed_number(parts):
    with pytest.raises(OfficePackageError, match="planning_numeric_replacement"):
        review(material(), "42", parts)


@pytest.mark.parametrize("with_data", [True, False])
def test_resumed_v5_context_equals_worker_rebuild(with_data):
    source = _deck()
    instruction = "Change the title to Quarter two; preserve all figures."
    original_data = (
        (JsonEvidenceSource("facts.json", b'{"value":42}'),)
        if with_data
        else (JsonEvidenceSource("User request", json.dumps({"request": instruction}, ensure_ascii=False).encode()),)
    )
    original = prepare_planning_context(source, instruction, original_data, profile=V5)
    resumed = prepare_answer_context(
        original,
        question="Only the title or also a caption?",
        answer="Only the title: Quarter two; preserve captions.",
        external_input_count=int(with_data),
    )
    added = JsonEvidenceSource(resumed.evidence.name, resumed.evidence.content)
    data = (*original_data, added) if with_data else (added,)
    assert resumed.context == prepare_planning_context(source, resumed.instruction, data, profile=V5)
    payload = json.loads(resumed.context.payload)
    fact = next(item["id"] for item in payload["facts"] if item["path"] == "/clarification/answer")
    case = source, resumed.instruction, data, resumed.context, payload
    assert review(case, "Quarter one", [{"ref": fact, "quote": "Quarter two"}]).changes[0].after == "Quarter two"


def test_profile_cannot_be_changed_while_reusing_context_digest():
    source, instruction, data, context, payload = material()
    with pytest.raises(OfficePackageError, match="planning_context_changed"):
        review_planning_response(source, instruction, data, proposal(payload, "Quarter one", [{"ref": "f0"}]), expected_context_digest=context.digest)


@pytest.mark.parametrize("value,quote", [("150", "50"), ("1.50", "50"), ("-150", "150"), ("1,500", "500"), ("1e3", "3"), ("150%", "150")])
def test_quote_cannot_change_numeric_magnitude_or_unit(value, quote):
    with pytest.raises(OfficePackageError, match="planning_quote_mismatch"):
        review(material(answer=f"The selected value is {value}."), "42", [{"ref": "f0", "quote": quote}])


@pytest.mark.parametrize("value", ["-150", "0.50", ".5", "+150", "150"])
def test_exact_complete_decimal_is_preserved(value):
    assert review(material(answer=f"The selected value is {value}."), "42", [{"ref": "f0", "quote": value}]).changes[0].after == value


def test_quote_selects_first_complete_occurrence_after_partial_number():
    case = material(answer="Not 150. Use 50 instead, and keep 50 as selected.")
    result = review(case, "42", [{"ref": "f0", "quote": "50"}])
    assert result.changes[0].after == "50"
    assert result.changes[0].evidence_refs == ("fact:f0:13:2",)


@pytest.mark.parametrize("value", [150, True, None, [150]])
def test_fragment_selection_requires_the_exact_text_fact(value):
    source, instruction, _, _, _ = material()
    data = (JsonEvidenceSource("answer.json", json.dumps({"answer": value}).encode()),)
    context = prepare_planning_context(source, instruction, data, profile=V5)
    case = source, instruction, data, context, json.loads(context.payload)
    with pytest.raises(OfficePackageError, match="planning_unusable_value"):
        review(case, "42", [{"ref": "f0", "quote": "150"}])


async def test_protocol_repair_selects_answer_without_another_business_question():
    from bowhead_core.documents.pptx_planning import PlanningReply, plan_pptx_text

    source, instruction, data, _, payload = material()
    replies = [proposal(payload, "42", [{"ref": "f0"}]), proposal(payload, "42", [{"ref": "f0", "quote": "150"}])]
    calls = []

    async def invoke(system, prompt):
        calls.append(json.loads(prompt))
        return PlanningReply(replies[len(calls) - 1], "stop")

    result = await plan_pptx_text(source, instruction, data, invoke=invoke, profile=V5)
    assert len(calls) == 2
    assert result.attempts[0].error == "planning_numeric_replacement"
    assert calls[1]["repair"]["error"] == "planning_numeric_replacement"
    assert result.review.changes[0].after == "150"
    assert result.review.question is None
