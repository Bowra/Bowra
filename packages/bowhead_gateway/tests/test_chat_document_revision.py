"""Exact document revision intent in the ordinary Chat submission contract."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.document_revision_target import DocumentRevisionTarget
from bowhead_core.turn_runner import _restore_document_revision_target
from bowhead_core.types import TurnRequest
from bowhead_gateway.routes.chat import ChatSendRequest, ChatSendResponse
from pydantic import ValidationError
from test_chat_reference_submission import fingerprint  # noqa: F401

pytestmark = pytest.mark.unit
TARGET = {"kind": "document_revision", "version": 1, "session_id": "s_review", "job_id": "djob_review", "draft_digest": "a" * 64}


def payload():
    return {
        "session_id": "s_review",
        "workspace_id": "ws_test",
        "message": "Shorten the title.",
        "document_revision_target": TARGET,
        "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
    }


def test_revision_uses_ordinary_contract_and_echoes_exact_target(fingerprint):  # noqa: F811
    request = ChatSendRequest.model_validate(payload())
    assert request.document_revision_target.model_dump() == TARGET
    response = ChatSendResponse(
        session_id="s_review", turn_id="t_review", stream_url="/stream", document_revision_target=request.document_revision_target
    )
    assert response.model_dump()["document_revision_target"] == TARGET
    ordinary = payload()
    del ordinary["document_revision_target"]
    assert fingerprint(ordinary) != fingerprint(payload())
    assert "document_revision_target" not in ChatSendResponse(session_id="s_review", turn_id="t_old", stream_url="/stream").model_dump()


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": None},
        {"session_id": "s_other"},
        {"client_submission_id": None},
        {"submission_kind": "steer", "expected_parent_turn_id": "t_prior", "expected_execution_generation": 1},
        {"submission_kind": "replay", "replay_source_turn_id": "t_prior"},
        {"input_document": None},
        {"attachments": ["art_new"]},
        {"plan_execution_id": "plan_other"},
        {"planning_mode": "planning"},
        {"client_info": {"agent_draft_id": "sad_other"}},
        {"document_revision_target": {**TARGET, "version": True}},
        {"document_revision_target": {**TARGET, "job_id": "djob_other", "authority": "owner"}},
    ],
)
def test_revision_rejects_competing_or_ambiguous_request_modes(change):
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate({**payload(), **change})


@pytest.mark.parametrize("field,value", [("job_id", "djob_other"), ("draft_digest", "b" * 64)])
def test_revision_retry_fingerprint_detects_target_changes(fingerprint, field, value):  # noqa: F811
    assert fingerprint(payload()) != fingerprint({**payload(), "document_revision_target": {**TARGET, field: value}})


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [None, "absent", "session", "target", "text", "attachments"])
async def test_worker_rehydrates_only_the_exact_canonical_source(monkeypatch, corruption):
    target = DocumentRevisionTarget.model_validate(TARGET)
    request = TurnRequest(
        member_uid=None,
        session_id=target.session_id,
        turn_id="t_review",
        user_id="u_owner",
        workspace_id="ws_test",
        text="Shorten the title.",
        document_revision_target=target,
    )
    source = ChatTurnUserSource(
        turn_id=request.turn_id,
        session_id=request.session_id,
        user_id=request.user_id,
        workspace_id=request.workspace_id,
        user_text=request.text,
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        document_revision_target=target,
    )
    if corruption == "session":
        source = replace(source, session_id="s_other", document_revision_target=target.model_copy(update={"session_id": "s_other"}))
    elif corruption == "target":
        source = replace(source, document_revision_target=target.model_copy(update={"draft_digest": "b" * 64}))
    elif corruption == "text":
        source = replace(source, user_text="Different instruction")
    elif corruption == "attachments":
        source = replace(source, attachment_ids=("art_new",))
    elif corruption == "absent":
        source = None

    async def read(**scope):
        assert scope == {"turn_id": request.turn_id, "user_id": request.user_id, "workspace_id": request.workspace_id}
        return source

    if corruption:
        with pytest.raises(ValueError, match="document_revision_source_conflict"):
            await _restore_document_revision_target(request, SimpleNamespace(get_chat_turn_user_source=read))
    else:
        await _restore_document_revision_target(request, SimpleNamespace(get_chat_turn_user_source=read))
        assert request.document_revision_target == target
