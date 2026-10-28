"""Live and replay apply the same original document reader before exposing pointers."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.documents.request import DocumentRequestError, DocumentScope
from bowhead_gateway.routes import documents
from bowhead_gateway.source_access_projection import project_event_payloads_for_viewer, project_live_event_payload_for_viewer
from fastapi import HTTPException

from bowhead_gateway import document_canvas_projection as projection

POINTER = {
    "version": 1,
    "turn_id": "t_doc",
    "session_id": "s_doc",
    "user_id": "u_doc",
    "team_id": "team_doc",
    "workspace_id": "ws_doc",
    "job_id": "djob_original",
    "draft_digest": "a" * 64,
}


@pytest.fixture
def reader(monkeypatch):
    scope = AsyncMock(return_value=DocumentScope(user_id="u_doc", team_id="team_doc", workspace_id="ws_doc"))
    validate = AsyncMock()
    connection = SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def database(_path):
        yield connection

    monkeypatch.setattr(documents, "_scope", scope)
    monkeypatch.setattr(documents, "_service", lambda _request: SimpleNamespace(delivery=SimpleNamespace(path="original.db")))
    monkeypatch.setattr(projection, "open_shared_ctx", database)
    monkeypatch.setattr(projection, "validate_document_draft_open_in_tx", validate)
    return scope, validate


@pytest.mark.anyio
async def test_live_and_persisted_pages_use_the_same_authorized_pointer(reader):
    scope, validate = reader
    live = await project_live_event_payload_for_viewer(None, "t_doc", "document.draft_open", {**POINTER, "seq": 7})
    replay = await project_event_payloads_for_viewer(None, "t_doc", [("document.draft_open", POINTER)])
    assert live == replay[0] == POINTER
    scope.assert_awaited_with(None, session_id="s_doc", job_id="djob_original")
    assert validate.await_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["owner", "read", "domain", "missing", "version", "content", "turn", "unavailable"])
async def test_all_event_surfaces_strip_inaccessible_or_untrusted_pointers(reader, failure):
    scope, validate = reader
    payload = dict(POINTER)
    if failure == "owner":
        scope.return_value = DocumentScope(user_id="u_other", team_id="team_doc", workspace_id="ws_doc")
    elif failure == "read":
        scope.side_effect = HTTPException(403)
    elif failure == "domain":
        validate.side_effect = DocumentRequestError("document_not_found")
    elif failure == "missing":
        del payload["version"]
    elif failure == "version":
        payload["version"] = True
    elif failure == "content":
        payload["title"] = "Private model output"
    elif failure == "turn":
        payload["turn_id"] = "t_other"
    elif failure == "unavailable":
        validate.side_effect = RuntimeError("Storage unavailable")
    assert await project_live_event_payload_for_viewer(None, "t_doc", "document.draft_open", payload) == {}
    assert await project_event_payloads_for_viewer(None, "t_doc", [("document.draft_open", payload)]) == [{}]
