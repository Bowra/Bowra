"""The real route consumes the background receiver index with current space ACLs."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.chat_references import ReferenceNode
from bowhead_core.reference_mcp_metadata import ReferenceMcpMetadataIndex
from bowhead_core.reference_receiver_metadata import ReferenceReceiverMetadataCache
from bowhead_gateway.routes import chat_reference_catalog as routes
from bowhead_gateway.routes.auth import _current_user, _store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_reference_receiver_metadata import release_cells
from test_reference_start_preparation import activate, raw_document
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


@pytest.mark.parametrize("receiver_case", ["admission_extension_optional_unused_invoke"], indirect=True)
async def test_http_uses_the_exact_receiver_cache_and_fails_closed_when_it_is_cold(receiver_case, monkeypatch):
    case = receiver_case
    activate(case)
    release_cells(monkeypatch)
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP", "1")
    monkeypatch.setattr(routes, "reference_initial_runtime_ready", lambda request: True)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/chat/references")
    user = await case.case.fixture.users.get_user(case.scope.user_id)
    app.dependency_overrides[_current_user] = lambda: user
    app.dependency_overrides[_store] = lambda: case.case.fixture.users
    app.state.user_store = case.case.fixture.users
    app.state.access_control_store = AccessControlStore(case.case.fixture.db_path)
    app.state.builtin_skill_reference_metadata = case.sources.builtin_index()
    cache = ReferenceReceiverMetadataCache(case.case.fixture.db_path)
    app.state.reference_receiver_metadata = cache
    agent = next(node.resource for node in raw_document(case, skills=False).nodes if isinstance(node, ReferenceNode))
    body = {"workspace_id": case.scope.workspace_id, "trigger": "/", "selected_refs": [agent.model_dump(mode="json", exclude_none=True)]}
    client = TestClient(app)

    cold = client.post("/api/v1/chat/references/catalog", json=body)
    assert cold.status_code == 200, cold.text
    assert all(not cell["supported"] for cell in cold.json()["entry_support"] if cell["resource_kind"] in {"mcp_source", "mcp_tool"})

    class ChangingMcpCache:
        calls = 0

        def current(self):
            self.calls += 1
            return ReferenceMcpMetadataIndex(
                Path(case.case.fixture.db_path),
                ready=self.calls == 1,
            )

    mcp_cache = ChangingMcpCache()
    app.state.reference_mcp_metadata = mcp_cache
    app.state.orchestrator = SimpleNamespace(
        session_store=SimpleNamespace(db_path=case.case.fixture.db_path),
    )
    captured = client.post("/api/v1/chat/references/catalog", json=body)
    assert captured.status_code == 200, captured.text
    assert mcp_cache.calls == 1
    del app.state.reference_mcp_metadata

    def get():
        result = client.post("/api/v1/chat/references/catalog", json=body)
        assert result.status_code == 200, result.text
        return next(item for group in result.json()["groups"] for item in group["items"] if item["resource"] == case.optional.model_dump(mode="json"))

    assert get()["availability"] == "unavailable"
    await cache.refresh()
    assert get()["availability"] == "readonly_extension"
    app.state.reference_receiver_metadata = ReferenceReceiverMetadataCache(case.case.fixture.db_path)
    assert get()["availability"] == "unavailable"


@pytest.mark.parametrize("receiver_case", ["admission_capability_skill"], indirect=True)
async def test_http_catalog_intersects_agent_skill_and_capability(receiver_case, monkeypatch):
    case = receiver_case
    activate(case)
    release_cells(monkeypatch)
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_CAPABILITY", "1")
    monkeypatch.setattr(routes, "reference_initial_runtime_ready", lambda request: True)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1/chat/references")
    user = await case.case.fixture.users.get_user(case.scope.user_id)
    app.dependency_overrides[_current_user] = lambda: user
    app.dependency_overrides[_store] = lambda: case.case.fixture.users
    app.state.user_store = case.case.fixture.users
    app.state.access_control_store = AccessControlStore(case.case.fixture.db_path)
    app.state.builtin_skill_reference_metadata = case.sources.builtin_index()
    cache = ReferenceReceiverMetadataCache(case.case.fixture.db_path)
    await cache.refresh()
    app.state.reference_receiver_metadata = cache
    agent = next(node.resource for node in raw_document(case, skills=False).nodes if isinstance(node, ReferenceNode))
    capability = {"kind": "capability", "id": "cap_visualize", "version": 1}
    client = TestClient(app)

    def get(selected_refs):
        response = client.post(
            "/api/v1/chat/references/catalog",
            json={"workspace_id": case.scope.workspace_id, "trigger": "/", "selected_refs": selected_refs},
        )
        assert response.status_code == 200, response.text
        return response.json()

    with_skill = get([agent.model_dump(mode="json", exclude_none=True), case.selected.model_dump(mode="json")])
    capability_item = next(item for group in with_skill["groups"] for item in group["items"] if item["resource"] == capability)
    assert capability_item["availability"] == "baseline"
    with_capability = get([agent.model_dump(mode="json", exclude_none=True), capability])
    skill_item = next(
        item for group in with_capability["groups"] for item in group["items"] if item["resource"] == case.selected.model_dump(mode="json")
    )
    assert skill_item["availability"] == "baseline"
