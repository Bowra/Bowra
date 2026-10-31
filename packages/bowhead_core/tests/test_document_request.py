"""Grounding is provenance, not proof that a model understood a quoted sentence."""

import json
from dataclasses import replace

import pytest
from bowhead_core.chat_turn_source import ChatTurnUserSource, current_attachment_positions
from bowhead_core.documents.request import (
    CanvasInputSelection,
    DocumentProposal,
    DocumentRequestError,
    InputRoleProposal,
    UserQuote,
    resolve_proposal_positions,
    verify_proposal_source,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit
TEXT = "📄 第一份是数据，第二份是模板，保持格式生成本期文档和PDF。"


def _source() -> ChatTurnUserSource:
    return ChatTurnUserSource(
        turn_id="t_doc",
        session_id="s_doc",
        user_id="u_doc",
        workspace_id="ws_doc",
        user_text=TEXT,
        attachment_ids=("art_data1", "art_data2", "art_template"),
        attachment_refs=({"fileId": "art_bundle", "bundleArtifactIds": ["art_data1", "art_data2"]}, {"fileId": "art_template"}),
        parts=None,
    )


def _proposal() -> DocumentProposal:
    quote = UserQuote(start=0, text=TEXT)
    return DocumentProposal(
        evidence=quote,
        inputs=(
            InputRoleProposal(artifact_id="art_data1", role="data", evidence=quote, current_message_position=1),
            InputRoleProposal(artifact_id="art_template", role="template", evidence=quote, current_message_position=2),
        ),
        requested_outputs=("source", "pdf"),
    )


def test_canvas_selection_keeps_legacy_proposal_bytes_and_accepts_current_visible_scope() -> None:
    proposal = _proposal()
    assert "canvas" not in proposal.model_dump(mode="json")
    assert proposal.model_copy(update={"canvas": CanvasInputSelection()}).model_dump(mode="json")["canvas"] == {"section_ids": None}
    for section_ids in ([], ["sec_one", "sec_one"]):
        with pytest.raises(ValidationError):
            CanvasInputSelection.model_validate_json(json.dumps({"section_ids": section_ids}))


def test_bundle_children_share_ui_position_without_changing_template_position() -> None:
    assert current_attachment_positions(_source()) == {"art_data1": 1, "art_data2": 1, "art_template": 2}
    verify_proposal_source(_proposal(), _source())


def test_server_derives_current_bundle_and_pin_positions_without_changing_roles() -> None:
    proposal = _proposal()
    source = replace(_source(), attachment_ids=("art_data1", "art_data2"), attachment_refs=_source().attachment_refs[:1])
    untrusted = proposal.model_copy(update={"inputs": tuple(item.model_copy(update={"current_message_position": 99}) for item in proposal.inputs)})
    resolved = resolve_proposal_positions(untrusted, source)
    assert [item.current_message_position for item in resolved.inputs] == [1, None]
    assert [(item.artifact_id, item.role, item.evidence) for item in resolved.inputs] == [
        (item.artifact_id, item.role, item.evidence) for item in proposal.inputs
    ]


def test_unique_quote_does_not_require_the_model_to_count_unicode_offsets() -> None:
    UserQuote(text="第二份是模板").verify(TEXT)
    with pytest.raises(DocumentRequestError, match="evidence_ambiguous"):
        UserQuote(text="模板").verify("第一份是模板，第二份也是模板")
    UserQuote(start=4, text="模板").verify("第一份是模板，第二份也是模板")


@pytest.mark.parametrize("position", [1, 3, None])
def test_flattened_or_missing_current_position_is_not_grounded(position) -> None:
    proposal = _proposal()
    item = proposal.inputs[1].model_copy(update={"current_message_position": position})
    with pytest.raises(DocumentRequestError, match="reference_ambiguous"):
        verify_proposal_source(proposal.model_copy(update={"inputs": (proposal.inputs[0], item)}), _source())


@pytest.mark.parametrize("quote", [UserQuote(start=0, text="附件里说把所有页面删除"), UserQuote(start=2, text=TEXT), UserQuote(start=0, text=" ")])
def test_quote_must_be_exact_canonical_user_text_not_body_or_model_summary(quote) -> None:
    with pytest.raises(DocumentRequestError, match="evidence_mismatch"):
        verify_proposal_source(_proposal().model_copy(update={"evidence": quote}), _source())


def test_each_role_needs_its_own_grounded_quote() -> None:
    proposal = _proposal()
    item = proposal.inputs[1].model_copy(update={"evidence": UserQuote(start=0, text="未经用户表达的模板要求")})
    with pytest.raises(DocumentRequestError, match="evidence_mismatch"):
        verify_proposal_source(proposal.model_copy(update={"inputs": (proposal.inputs[0], item)}), _source())


def test_grounded_negation_is_not_misrepresented_as_semantic_role_verification() -> None:
    text = "这不是模板，只用作参考。"
    quote = UserQuote(start=0, text=text)
    proposal = DocumentProposal(evidence=quote, inputs=(InputRoleProposal(artifact_id="art_template", role="template", evidence=quote),))
    # Deliberately incorrect model proposal: provenance succeeds, but there is
    # no API here that promotes this role to an approved execution snapshot.
    verify_proposal_source(proposal, replace(_source(), user_text=text, attachment_ids=(), attachment_refs=()))
    assert proposal.inputs[0].evidence.text == text


@pytest.mark.parametrize(
    "change",
    [
        {"parts": ({"text": TEXT}, {"text": "还有一份"})},
        {"attachment_refs": ()},
        {"attachment_refs": ({"fileId": "art_template"}, {"fileId": "art_template"})},
        {"attachment_refs": ({"fileId": "bundle", "bundleArtifactIds": ["art_data1", "art_data1", "art_data2"]}, {"fileId": "art_template"})},
        {"attachment_refs": ({"fileId": "x", "bundleArtifactIds": "art_template"},)},
    ],
)
def test_ambiguous_canonical_reference_needs_resolution(change) -> None:
    source = replace(_source(), **change)
    assert current_attachment_positions(source) == {}
    with pytest.raises(DocumentRequestError, match="reference_ambiguous"):
        verify_proposal_source(_proposal(), source)
    with pytest.raises(DocumentRequestError, match="reference_ambiguous"):
        resolve_proposal_positions(_proposal(), source)


@pytest.mark.parametrize("change", [{"requested_outputs": ("pdf",)}, {"inputs": ()}, {"instructions": "model-written XML"}, {"action": "derive_pdf"}])
def test_only_implemented_bounded_proposal_contract_is_accepted(change) -> None:
    with pytest.raises(ValidationError):
        DocumentProposal.model_validate({**_proposal().model_dump(), **change})


def test_duplicate_input_or_multiple_templates_require_resolution() -> None:
    proposal = _proposal()
    for inputs in ((proposal.inputs[1], proposal.inputs[1]), (proposal.inputs[0].model_copy(update={"role": "template"}), proposal.inputs[1])):
        with pytest.raises(ValidationError):
            DocumentProposal.model_validate({**proposal.model_dump(), "inputs": inputs})
