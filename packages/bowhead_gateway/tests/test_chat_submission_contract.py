from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_gateway.dtos import TurnAttachmentDTO
from bowhead_gateway.routes import chat as chat_routes
from bowhead_gateway.routes.chat import (
    ChatAttachmentRef,
    ChatSendRequest,
    ChatSendResponse,
    _record_composer_send_result,
    _validate_chat_attachment_refs,
    send,
)
from fastapi import HTTPException, Response
from pydantic import ValidationError


def test_turn_attachment_reference_kind_is_additive_on_the_wire() -> None:
    base = {
        "file_id": "art_1",
        "filename": "evidence.csv",
        "mime": "text/csv",
        "size": 42,
    }

    assert "reference_kind" not in TurnAttachmentDTO(**base).model_dump()
    assert (
        TurnAttachmentDTO(
            **base,
            reference_kind="result",
        ).model_dump()["reference_kind"]
        == "result"
    )


def test_batch_a_start_contract_is_strict_and_uuid4() -> None:
    parsed = ChatSendRequest.model_validate(
        {
            "workspace_id": "ws_1",
            "message": "hello",
            "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
            "submission_kind": "start",
            "submission_contract_version": "chat-submit.v1",
            "model_control_batch": "a",
        }
    )
    assert parsed.client_submission_id is not None
    assert parsed.client_submission_id.version == 4


def test_batch_a_steer_contract_is_kind_exclusive() -> None:
    parsed = ChatSendRequest.model_validate(
        {
            "workspace_id": "ws_1",
            "session_id": "s_1",
            "message": "补充西区",
            "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
            "submission_kind": "steer",
            "submission_contract_version": "chat-submit.v1",
            "model_control_batch": "a",
            "expected_parent_turn_id": "t_1",
            "expected_execution_generation": 1,
        }
    )
    assert parsed.submission_kind == "steer"


def test_chat_action_identity_cannot_mix_with_submission_contract() -> None:
    with pytest.raises(ValidationError, match="cannot be mixed with a submission id"):
        ChatSendRequest.model_validate(
            {
                "workspace_id": "ws_1",
                "message": "hello",
                "client_info": {
                    "action_kind": "start",
                    "request_id": "act_start_0123456789abcdef0123456789abcdef",
                },
                "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
                "submission_kind": "start",
                "submission_contract_version": "chat-submit.v1",
                "model_control_batch": "a",
            }
        )


def test_batch_a_typed_bundle_is_lossless_and_flat_ids_are_unambiguous() -> None:
    payload = {
        "workspace_id": "ws_1",
        "message": "",
        "attachments": ["art_child_a", "art_child_b"],
        "attachment_refs": [
            {
                "fileId": "art_bundle",
                "filename": "data.zip",
                "size": 42,
                "mime": "application/zip",
                "bundleArtifactIds": ["art_child_a", "art_child_b"],
            }
        ],
        "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
        "submission_kind": "start",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
    }
    parsed = ChatSendRequest.model_validate(payload)
    assert parsed.attachment_refs is not None
    assert (
        parsed.attachment_refs[0].model_dump(
            by_alias=True,
            exclude_none=True,
        )
        == payload["attachment_refs"][0]
    )

    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate({**payload, "attachments": ["art_bundle"]})


def test_terminal_start_action_accepts_typed_attachment_refs_without_submission_id() -> None:
    client_info = {
        "action_kind": "start",
        "request_id": "act_start_0123456789abcdef0123456789abcdef",
        "user_msg_id": "u_s_0123456789abcdef0123456789abcdef",
    }
    payload = {
        "workspace_id": "ws_1",
        "message": "read this",
        "attachments": ["art_1"],
        "attachment_refs": [
            {
                "fileId": "art_1",
                "filename": "evidence.pdf",
                "size": 42,
                "mime": "application/pdf",
            }
        ],
        "client_info": client_info,
    }

    parsed = ChatSendRequest.model_validate(payload)

    assert parsed.attachments == ["art_1"]
    assert parsed.attachment_refs is not None
    with pytest.raises(ValidationError, match="ambiguous attachments"):
        ChatSendRequest.model_validate({**payload, "attachments": ["art_other"]})


def test_ordinary_typed_attachments_preserve_terminal_action_boundaries() -> None:
    ref = {
        "fileId": "art_1",
        "filename": "evidence.pdf",
        "size": 42,
        "mime": "application/pdf",
    }
    ordinary = ChatSendRequest.model_validate(
        {"workspace_id": "ws_1", "message": "Use this template.", "attachments": ["art_1"], "attachment_refs": [ref]}
    )
    assert ordinary.client_submission_id is None
    assert ordinary.attachment_refs[0].file_id == "art_1"
    with pytest.raises(
        ValidationError,
        match="Terminal start attachments require typed refs",
    ):
        ChatSendRequest.model_validate(
            {
                "workspace_id": "ws_1",
                "message": "start",
                "attachments": ["art_foreign"],
                "client_info": {
                    "action_kind": "start",
                    "request_id": "act_start_0123456789abcdef0123456789abcdef",
                },
            }
        )
    with pytest.raises(
        ValidationError,
        match="Terminal steer attachments are not supported",
    ):
        ChatSendRequest.model_validate(
            {
                "workspace_id": "ws_1",
                "session_id": "s_1",
                "message": "steer",
                "attachments": ["art_1"],
                "attachment_refs": [ref],
                "client_info": {
                    "action_kind": "steer",
                    "request_id": "act_steer_0123456789abcdef0123456789abcdef",
                    "expected_turn_id": "t_parent",
                    "expected_execution_generation": "1",
                },
            }
        )


@pytest.mark.asyncio
async def test_results_reference_uses_session_fence_and_is_lossless() -> None:
    result = SimpleNamespace(
        id="art_result",
        filename="analysis.csv",
        mime="text/csv",
        size=42,
    )

    class ArtifactStore:
        async def list_for_session(self, **kwargs):
            assert kwargs == {
                "session_id": "s_1",
                "user_id": "u_1",
                "limit": 500,
            }
            return [result]

    ref = ChatAttachmentRef.model_validate(
        {
            "fileId": result.id,
            "filename": result.filename,
            "mime": result.mime,
            "size": result.size,
            "referenceKind": "result",
        }
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(artifact_store=ArtifactStore())),
    )

    validated = await _validate_chat_attachment_refs(
        request,
        refs=[ref],
        runtime_ids=[result.id],
        user_id="u_1",
        workspace_id="ws_1",
        session_id="s_1",
    )

    assert validated == [
        {
            "fileId": result.id,
            "filename": result.filename,
            "size": result.size,
            "mime": result.mime,
            "referenceKind": "result",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["upload", "result"])
async def test_empty_bundle_list_keeps_single_file_attachment_semantics(reference_kind: str) -> None:
    artifact = SimpleNamespace(
        id="art_single",
        filename="evidence.csv",
        mime="text/csv",
        size=42,
    )

    class ArtifactStore:
        async def get_ready_uploads_for_chat(self, ids, **kwargs):
            assert ids == [artifact.id]
            assert kwargs == {"user_id": "u_1", "workspace_id": "ws_1"}
            return {artifact.id: artifact}

        async def list_for_session(self, **kwargs):
            assert kwargs == {
                "session_id": "s_1",
                "user_id": "u_1",
                "limit": 500,
            }
            return [artifact]

    ref = ChatAttachmentRef.model_validate(
        {
            "fileId": artifact.id,
            "filename": artifact.filename,
            "mime": artifact.mime,
            "size": artifact.size,
            "referenceKind": reference_kind,
            "bundleArtifactIds": [],
        }
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(artifact_store=ArtifactStore())),
    )

    validated = await _validate_chat_attachment_refs(
        request,
        refs=[ref],
        runtime_ids=[artifact.id],
        user_id="u_1",
        workspace_id="ws_1",
        session_id="s_1",
    )

    assert validated[0]["bundleArtifactIds"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"session_id": None},
        {"runtime_ids": ["art_other"]},
        {"filename": "spoofed.csv"},
        {"bundle_artifact_ids": ["art_child"]},
    ],
)
async def test_results_reference_fails_closed(override: dict[str, object]) -> None:
    result = SimpleNamespace(
        id="art_result",
        filename="analysis.csv",
        mime="text/csv",
        size=42,
    )

    class ArtifactStore:
        async def list_for_session(self, **_kwargs):
            return [] if override.get("runtime_ids") else [result]

    ref = ChatAttachmentRef(
        fileId=result.id,
        filename=str(override.get("filename", result.filename)),
        mime=result.mime,
        size=result.size,
        referenceKind="result",
        bundleArtifactIds=override.get("bundle_artifact_ids"),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(artifact_store=ArtifactStore())),
    )

    with pytest.raises(HTTPException) as caught:
        await _validate_chat_attachment_refs(
            request,
            refs=[ref],
            runtime_ids=override.get("runtime_ids", [result.id]),
            user_id="u_1",
            workspace_id="ws_1",
            session_id=override.get("session_id", "s_1"),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "error": {"code": "result_reference_unavailable"},
    }


def test_batch_a_replay_wire_contains_only_source_identity() -> None:
    base = {
        "workspace_id": "ws_1",
        "message": "",
        "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
        "submission_kind": "replay",
        "submission_contract_version": "chat-submit.v1",
        "model_control_batch": "a",
        "replay_source_turn_id": "t_source",
    }
    assert ChatSendRequest.model_validate(base).submission_kind == "replay"
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate({**base, "message": "client copy"})
    with pytest.raises(ValidationError, match="invalid replay submission contract"):
        ChatSendRequest.model_validate(
            {
                **base,
                "attachments": ["art_1"],
                "attachment_refs": [
                    {
                        "fileId": "art_1",
                        "filename": "evidence.pdf",
                        "size": 42,
                        "mime": "application/pdf",
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "patch",
    (
        {"client_submission_id": "0da31a6b-2c6e-1fb3-8c80-f034b37d840f"},
        {"submission_kind": "start"},
        {
            "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
            "submission_kind": "start",
        },
        {
            "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
            "submission_kind": "start",
            "submission_contract_version": "chat-model.v2",
            "model_control_batch": "a",
        },
        {
            "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
            "submission_kind": "replay",
            "submission_contract_version": "chat-submit.v1",
            "model_control_batch": "a",
        },
        {
            "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
            "submission_kind": "steer",
            "submission_contract_version": "chat-submit.v1",
            "model_control_batch": "a",
            "session_id": "s_1",
            "expected_parent_turn_id": "t_1",
        },
    ),
)
def test_invalid_or_partial_submission_contract_is_422_shape(
    patch: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate({"workspace_id": "ws_1", "message": "hello", **patch})


def test_send_quality_metric_uses_only_server_bounded_labels(monkeypatch) -> None:
    from bowhead_core import metrics

    observed: list[tuple[str, dict[str, str]]] = []

    def record_metric(name: str, **kwargs) -> None:
        observed.append((name, kwargs["labels"]))

    monkeypatch.setattr(
        metrics,
        "inc",
        record_metric,
    )
    body = ChatSendRequest.model_validate(
        {
            "workspace_id": "ws_secret",
            "message": "private prompt",
            "attachments": ["artifact_secret"],
            "client_info": {"composer_surface": "home"},
            "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
            "submission_kind": "start",
            "submission_contract_version": "chat-submit.v1",
            "model_control_batch": "a",
        }
    )

    _record_composer_send_result(
        body=body,
        attachment_count=1,
        result="server_error",
        request=SimpleNamespace(state=SimpleNamespace()),
    )

    assert [name for name, _labels in observed] == [
        "composer_send_result_total",
        "composer_attachment_action_total",
    ]
    flattened = repr(observed)
    assert "private prompt" not in flattened
    assert "artifact_secret" not in flattened
    assert "team_secret" not in flattened
    assert observed[0][1]["surface"] == "home"
    assert observed[0][1]["result"] == "server_error"
    assert observed[1][1]["result"] == "server_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_result"),
    ((409, "rejected"), (503, "server_error")),
)
async def test_send_wrapper_records_pre_dispatch_rejections_once(
    monkeypatch,
    status_code: int,
    expected_result: str,
) -> None:
    observed: list[dict[str, str]] = []

    async def rejected(*_args, **_kwargs):
        raise HTTPException(status_code=status_code, detail="rejected before dispatch")

    def record_metric(name: str, **kwargs) -> None:
        if name == "composer_send_result_total":
            observed.append(kwargs["labels"])

    monkeypatch.setattr(chat_routes, "_send_impl", rejected)
    from bowhead_core import metrics

    monkeypatch.setattr(metrics, "inc", record_metric)
    body = ChatSendRequest.model_validate(
        {
            "workspace_id": "ws_1",
            "message": "hello",
            "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
            "submission_kind": "start",
            "submission_contract_version": "chat-submit.v1",
            "model_control_batch": "a",
        }
    )
    request = SimpleNamespace(state=SimpleNamespace())

    with pytest.raises(HTTPException):
        await send(body, request, Response())  # type: ignore[arg-type]

    assert [labels["result"] for labels in observed] == [expected_result]


@pytest.mark.asyncio
async def test_send_wrapper_does_not_double_count_receipt_replay(monkeypatch) -> None:
    observed: list[str] = []

    async def replay(_body, request, *_args):
        request.state.composer_send_result_recorded = True
        return ChatSendResponse(
            session_id="s_1",
            turn_id="t_1",
            stream_url="/api/v1/chat/stream/t_1",
        )

    monkeypatch.setattr(chat_routes, "_send_impl", replay)
    from bowhead_core import metrics

    monkeypatch.setattr(metrics, "inc", lambda name, **_kwargs: observed.append(name))
    body = ChatSendRequest.model_validate(
        {
            "workspace_id": "ws_1",
            "message": "hello",
            "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
            "submission_kind": "start",
            "submission_contract_version": "chat-submit.v1",
            "model_control_batch": "a",
        }
    )
    request = SimpleNamespace(state=SimpleNamespace())

    result = await send(body, request, Response())  # type: ignore[arg-type]

    assert result.turn_id == "t_1"
    assert observed == []
