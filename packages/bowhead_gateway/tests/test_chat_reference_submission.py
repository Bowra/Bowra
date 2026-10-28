"""Strict reference wire and semantic retry identities at the HTTP boundary."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx2
import pytest
from bowhead_core.chat_references import InputDocument
from bowhead_gateway.chat_reference_request import (
    MAX_REFERENCE_REQUEST_BYTES,
    REFERENCE_MEDIA_TYPE,
    ChatReferenceRoute,
)
from bowhead_gateway.routes.chat import ChatSendRequest, _submission_fingerprint
from fastapi import APIRouter, FastAPI
from pydantic import ValidationError

pytestmark = pytest.mark.unit
_CASES = json.loads((Path(__file__).resolve().parents[3] / "contracts/chat-references/input-document-v1.json").read_text())


def reference_payload() -> dict:
    document = copy.deepcopy(next(case["input"] for case in _CASES["valid"] if case["name"] == "exact_agent_and_parameterized_skill"))
    return {
        "workspace_id": "ws_1",
        "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
        "submission_kind": "start",
        "submission_contract_version": "chat-references.v1",
        "model_control_batch": "references",
        "input_document": document,
        "message": InputDocument.model_validate(document).display_text(),
    }


@pytest.fixture
def fingerprint(monkeypatch):
    monkeypatch.setattr("bowhead_core.composer_rollout.submission_fingerprint_keyring", lambda: (("a" * 64, b"a" * 32),))

    def calculate(payload, **scope):
        return _submission_fingerprint(
            ChatSendRequest.model_validate(payload),
            **{"user_id": "u_1", "team_id": "team_1", "auth_session_generation": "generation", **scope},
        )[1]

    return calculate


@pytest.mark.parametrize(
    "batch,version,expected",
    [
        ("a", "chat-submit.v1", "66510c305e52bca9e4fc4bd7e21b5c88ee49963743b69483778514ad5ce3ee42"),
        ("b", "chat-model.v2", "335bed96e43e15a4644d558ab38261f4c797a948a5dc91e1a6658218c78b4247"),
        ("c", "chat-model-effort.v3", "f8c4b22d2228440da934d72b8e3af00aeedb73531b1b858a47dc74b825047d77"),
    ],
)
def test_old_submission_fingerprint_golden(fingerprint, batch, version, expected):
    # Captured from 0929e30b6 before the new contract was added.
    assert (
        fingerprint(
            {
                "workspace_id": "ws_1",
                "message": "盈利分析",
                "client_submission_id": "0da31a6b-2c6e-4fb3-8c80-f034b37d840f",
                "submission_kind": "start",
                "submission_contract_version": version,
                "model_control_batch": batch,
            }
        )
        == expected
    )


def test_retry_ignores_display_and_editor_changes(fingerprint):
    payload = reference_payload()
    expected = fingerprint(payload)
    node = payload["input_document"]["nodes"][0]
    node["label"] = "恶意显示标签 <script>"
    node["node_id"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    payload["input_document"]["nodes"].append({"type": "text", "text": ""})
    payload["message"] = InputDocument.model_validate(payload["input_document"]).display_text()
    assert fingerprint(payload) == expected


@pytest.mark.parametrize(
    "change", ["parameter", "null_missing", "version", "assignment", "release", "order", "text", "attachments", "provider", "planning", "session"]
)
def test_retry_detects_changed_execution_input(fingerprint, change):
    payload = reference_payload()
    expected = fingerprint(payload)
    nodes = payload["input_document"]["nodes"]
    if change == "parameter":
        nodes[2]["parameters"]["period"] = "2026Q4"
    elif change == "null_missing":
        del nodes[2]["parameters"]["optional"]
    elif change in {"version", "assignment", "release"}:
        key = {"version": "version", "assignment": "assignment_id", "release": "release_id"}[change]
        nodes[0]["resource"][key] += "2"
    elif change == "order":
        nodes[0], nodes[2] = nodes[2], nodes[0]
    elif change == "text":
        nodes[3]["text"] += " 范围扩大"
    elif change == "attachments":
        payload["attachments"] = ["art_other"]
    else:
        key = {"provider": "preferred_provider", "planning": "planning_mode", "session": "session_id"}[change]
        payload[key] = "planning" if change == "planning" else "other"
    payload["message"] = InputDocument.model_validate(payload["input_document"]).display_text()
    assert fingerprint(payload) != expected


@pytest.mark.parametrize("scope", ["user_id", "team_id", "auth_session_generation"])
def test_retry_binds_current_scope(fingerprint, scope):
    payload = reference_payload()
    assert fingerprint(payload, **{scope: "other"}) != fingerprint(payload)


@pytest.mark.parametrize(
    "patch",
    [
        {"input_document": None},
        {"message": "只读标签"},
        {"model_control_batch": "a"},
        {"submission_contract_version": "chat-submit.v1"},
        {"client_submission_id": None},
        {"input_document": {"schema_version": 2, "nodes": []}},
    ],
)
def test_invalid_reference_envelope(patch):
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate({**reference_payload(), **patch})


@pytest.mark.parametrize("document", [None, {"schema_version": 1, "nodes": []}])
def test_document_cannot_be_smuggled_into_legacy(document):
    with pytest.raises(ValidationError):
        ChatSendRequest(workspace_id="ws_1", message="hello", input_document=document)


def test_reference_replay_uses_only_persisted_source_and_steer_is_kind_exclusive():
    payload = reference_payload()
    payload.pop("input_document")
    payload.pop("message")
    payload.update(submission_kind="replay", replay_source_turn_id="t_source")
    assert ChatSendRequest.model_validate(payload).submission_kind == "replay"
    for key, value in [("message", ""), ("input_document", None), ("attachments", []), ("attachment_refs", None)]:
        with pytest.raises(ValidationError):
            ChatSendRequest.model_validate({**payload, key: value})
    steer = {
        **reference_payload(),
        "submission_kind": "steer",
        "session_id": "s_1",
        "expected_parent_turn_id": "t_1",
        "expected_execution_generation": 1,
    }
    assert ChatSendRequest.model_validate(steer).submission_kind == "steer"
    with pytest.raises(ValidationError):
        ChatSendRequest.model_validate({**steer, "preferred_model": "new-model"})


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/api/v1/chat", "/bowhead/api/v1/chat"])
async def test_raw_http_decoder_bounds_before_typed_endpoint(prefix):
    app = FastAPI()
    router = APIRouter(route_class=ChatReferenceRoute)
    received = []

    @router.post("/send")
    async def receive(body: ChatSendRequest):
        received.append(body)
        return {"ok": True}

    app.include_router(router, prefix=prefix)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as client:
        raw = json.dumps(reference_payload(), ensure_ascii=False)
        headers = {"content-type": REFERENCE_MEDIA_TYPE + "; charset=utf-8"}
        valid = await client.post(prefix + "/send", content=raw, headers=headers)
        assert valid.status_code == 200, valid.text
        assert len(received) == 1
        invalid = [
            raw.replace('"workspace_id": "ws_1"', '"workspace_id": "ws_1", "workspace_id": "ws_2"'),
            raw.replace('"period": "2026Q3"', '"period": "2026Q3", "period": "secret"'),
            raw.replace('"optional": null', '"optional": ' + "[" * 20 + "0" + "]" * 20),
            raw.replace('"schema_version": 1', '"schema_version": 2'),
            raw.replace('"message":', '"private_secret":'),
            "{",
            b"\xff",
        ]
        for invalid_raw in invalid:
            response = await client.post(prefix + "/send", content=invalid_raw, headers=headers)
            assert response.status_code == 422, response.text
            assert response.json() == {"detail": {"error": {"code": "reference_input_invalid"}}}

        async def oversized():
            yield b" " * MAX_REFERENCE_REQUEST_BYTES
            yield raw.encode()

        response = await client.post(prefix + "/send", content=oversized(), headers=headers)
        assert response.status_code == 413
        assert response.json() == {"detail": {"error": {"code": "reference_request_too_large"}}}
        assert len(received) == 1
        # Ordinary JSON still has its established reader and field semantics.
        response = await client.post(prefix + "/send", json={"workspace_id": "ws_1", "message": "x" * (MAX_REFERENCE_REQUEST_BYTES + 1)})
        assert response.status_code == 200
        assert len(received) == 2
