"""Resume evidence equals normal compiler input and preserves original slots."""

import json

import pytest
from bowhead_core.documents.package_io import OfficePackageError
from bowhead_core.documents.planning_resume import prepare_answer_context
from bowhead_core.documents.pptx_plan import JsonEvidenceSource, PlanningContext, prepare_planning_context, review_planning_response
from test_document_pptx_text import _deck

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("with_data", [False, True])
def test_saved_context_with_answer_equals_real_compiler_context(with_data):
    source = _deck()
    instruction = "Choose Quarter two or Quarter three for the title; keep the figures unchanged."
    data = (
        (JsonEvidenceSource("figures.json", b'{"value":42}'),)
        if with_data
        else (JsonEvidenceSource("User request", json.dumps({"request": instruction}, ensure_ascii=False).encode()),)
    )
    original = prepare_planning_context(source, instruction, data)
    resumed = prepare_answer_context(original, question="Which title should be used?", answer="Quarter two", external_input_count=int(with_data))
    derived = JsonEvidenceSource(resumed.evidence.name, resumed.evidence.content)
    resumed_data = (*data, derived) if with_data else (derived,)
    assert resumed.context == prepare_planning_context(source, resumed.instruction, resumed_data)
    before, after = json.loads(original.payload), json.loads(resumed.context.payload)
    for key in ("slots", "template_sha256", "unsupported_objects"):
        assert after[key] == before[key]
    assert after["facts"][: len(before["facts"])] == before["facts"]
    title = next(slot["id"] for slot in after["slots"] if slot["text"] == "Quarter one")
    fact = next(fact["id"] for fact in after["facts"] if fact["path"] == "/clarification/answer")
    proposal = {
        "action": "propose",
        "updates": [{"slot": title, "parts": [{"ref": fact}]}],
        "keep": [slot["id"] for slot in after["slots"] if slot["id"] != title],
    }
    review = review_planning_response(source, resumed.instruction, resumed_data, json.dumps(proposal), expected_context_digest=resumed.context.digest)
    assert review.changes[0].after == "Quarter two"
    assert review.changes[0].evidence_refs == (fact,)
    proposal["updates"][0]["parts"] = [{"quote": "Quarter two"}]
    with pytest.raises(OfficePackageError, match="planning_quote_mismatch"):
        review_planning_response(source, resumed.instruction, resumed_data, json.dumps(proposal), expected_context_digest=resumed.context.digest)


def test_source_name_cannot_impersonate_instruction_fallback():
    source = _deck()
    original_data = (JsonEvidenceSource("User request", b'{"private_value":123}'),)
    original = prepare_planning_context(source, "Use the provided value.", original_data)
    resumed = prepare_answer_context(original, question="Which title?", answer="Quarter two", external_input_count=1)
    assert resumed.context == prepare_planning_context(
        source, resumed.instruction, (*original_data, JsonEvidenceSource(resumed.evidence.name, resumed.evidence.content))
    )
    with pytest.raises(OfficePackageError, match="planning_context_changed"):
        prepare_answer_context(original, question="Which title?", answer="Quarter two", external_input_count=0)


@pytest.mark.parametrize("change", ["digest", "answer_limit", "question_limit", "input_limit", "instruction_limit"])
def test_answer_context_fails_without_truncating_or_rediscovering_sources(change):
    instruction = "x" * 7999 if change == "instruction_limit" else "Use a suitable title."
    original = prepare_planning_context(_deck(), instruction, (JsonEvidenceSource("facts.json", b'{"value":42}'),))
    if change == "digest":
        original = PlanningContext(original.payload, "0" * 64)
    with pytest.raises(OfficePackageError):
        prepare_answer_context(
            original,
            question="q" * 201 if change == "question_limit" else "Which title?",
            answer="a" * 257 if change == "answer_limit" else "Quarter two",
            external_input_count=7 if change == "input_limit" else 1,
        )
