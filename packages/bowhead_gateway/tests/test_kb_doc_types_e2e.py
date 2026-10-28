"""kb-knowledge-tables sprint Day 1: backend e2e — TestClient HTTP tests (new schema).

V1.0 改语义: extract_schema (fields) 替 V3 columns / chunk_strategy.

Covers:
- POST/GET/PUT/DELETE /api/v1/kb/doc_types
- POST /api/v1/kb/doc_types/draft (LLM mock returns fields not columns)
- Upload file with doc_type_id (P0-V3-1 cross-ws leak 拦截 still applies)
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.kb.doc_type_store import KbDocTypeStore
from bowhead_core.kb.files_store import KbFilesStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb as kb_routes
from bowhead_gateway.routes import kb_doc_types as doc_type_routes
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path


def _user(user_id: str = "u_alice", email: str = "alice@test.com") -> User:
    return User(id=user_id, email=email, team_id="team_test", tier="ultra", email_verified=True)


class _TeamRouterRegistryStub:
    """resolve_team_byo_overrides 期望的最小注册表——调用期回读 app.state.router。"""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def get(self, team_id: str, cfg: Any, version: int) -> Any:
        return self._app.state.router


@pytest.fixture
def app(tmp_path: Path):
    """Build a minimal FastAPI app with kb + doc_types routers + dependency overrides."""
    a = FastAPI()
    a.include_router(kb_routes.router, tags=["knowledge-base"])
    a.include_router(doc_type_routes.router, tags=["knowledge-base"])

    # Override auth (no JWT)
    a.dependency_overrides[_current_user] = _user
    return a


@pytest.fixture
async def app_with_stores(app: FastAPI, tmp_path: Path):
    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    files_store = KbFilesStore(tmp_path / "files.db")
    await files_store.initialize()
    app.state.kb_doc_type_store = dt_store
    app.state.kb_files_store = files_store
    # Mock router for /draft endpoint — new shape {"fields": [...]}.
    mock_resp = MagicMock(
        content=(
            '{"fields":['
            '{"name":"合同金额","type":"currency","description":"合同总金额",'
            '"required":true,"cross_verify":true,"extraction_hint":"通常在合同首页"},'
            '{"name":"起始日","type":"date","description":"合同生效日期",'
            '"required":false,"cross_verify":false,"extraction_hint":""}'
            "]}"
        )
    )
    mock_router = MagicMock()
    mock_router.invoke = AsyncMock(return_value=mock_resp)
    app.state.router = mock_router
    # credential-readiness D2.2: /draft now uses the team router (team_router_or_409),
    # no longer consuming process-level app.state.router. Stub registry reads app.state.router at invocation time
    # so tests retain their existing pattern of changing state.router.invoke.
    app.state.team_router_registry = _TeamRouterRegistryStub(app)
    # Once user_store exists, access_context uses real authorization (has_workspace_access_source
    # becomes true); supply its three awaited queries to preserve other cases' existing semantics (allow members).
    app.state.user_store = MagicMock(
        get_team_llm_config_with_version=AsyncMock(
            return_value=({"schema_version": 2, "providers": {"p": {"base_url": "http://x", "api_key": "k"}}}, 1),
        ),
        get_user_team_id=AsyncMock(return_value="team_test"),
        get_workspace_team_id=AsyncMock(return_value="team_test"),
        get_member_role=AsyncMock(return_value="admin"),
        get_workspace=AsyncMock(return_value={"status": "active"}),
        get_team_role=AsyncMock(return_value="team_admin"),
    )
    return app


@pytest.fixture
def client(app_with_stores: FastAPI) -> TestClient:
    return TestClient(app_with_stores)


def _assert_code_only(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    error = response.json()["detail"]["error"]
    assert error == {"code": code}
    assert "message" not in error


# ─── doc_types CRUD ────────────────────────────


def test_create_then_list_then_delete(client: TestClient) -> None:
    """POST → GET → DELETE round-trip with new fields schema."""
    r = client.post(
        "/api/v1/kb/doc_types",
        headers={"X-Workspace-Id": "ws_test"},
        json={
            "workspace_id": "ws_test",
            "name": "销售合同",
            "description": "2024 销售合同",
            "fields": [
                {
                    "name": "合同金额",
                    "type": "currency",
                    "description": "合同总金额",
                    "required": True,
                    "cross_verify": True,
                    "extraction_hint": "在第一条",
                },
                {
                    "name": "起始日",
                    "type": "date",
                    "description": "合同生效日期",
                },
            ],
            "extractor_model": "qwen-plus",
            "cross_verify_enabled": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "销售合同"
    assert body["extractor_model"] == "qwen-plus"
    assert body["cross_verify_enabled"] is True
    assert body["record_count"] == 0
    assert len(body["fields"]) == 2
    assert body["fields"][0]["name"] == "合同金额"
    assert body["fields"][0]["type"] == "currency"
    assert body["fields"][0]["required"] is True
    assert body["fields"][0]["cross_verify"] is True
    dt_id = body["doc_type_id"]
    assert dt_id.startswith("dt_")

    # GET list
    r2 = client.get(
        "/api/v1/kb/doc_types?workspace_id=ws_test",
        headers={"X-Workspace-Id": "ws_test"},
    )
    assert r2.status_code == 200
    items = r2.json()["items"]
    assert len(items) == 1
    assert items[0]["doc_type_id"] == dt_id

    # DELETE
    r3 = client.delete(
        f"/api/v1/kb/doc_types/{dt_id}",
        headers={"X-Workspace-Id": "ws_test"},
    )
    assert r3.status_code == 200
    assert r3.json()["deleted"] is True

    # GET should be empty
    r4 = client.get(
        "/api/v1/kb/doc_types?workspace_id=ws_test",
        headers={"X-Workspace-Id": "ws_test"},
    )
    assert r4.json()["items"] == []


def test_update_doc_type(client: TestClient) -> None:
    """PUT change name + cross_verify_enabled + fields."""
    r = client.post(
        "/api/v1/kb/doc_types",
        headers={"X-Workspace-Id": "ws_test"},
        json={"workspace_id": "ws_test", "name": "old_name"},
    )
    dt_id = r.json()["doc_type_id"]

    r2 = client.put(
        f"/api/v1/kb/doc_types/{dt_id}",
        headers={"X-Workspace-Id": "ws_test"},
        json={
            "name": "new_name",
            "cross_verify_enabled": False,
            "fields": [{"name": "X", "type": "string"}],
        },
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["name"] == "new_name"
    assert body["cross_verify_enabled"] is False
    assert len(body["fields"]) == 1
    assert body["fields"][0]["name"] == "X"


def test_create_validation_error_uses_stable_code(client: TestClient) -> None:
    """Create with too many fields → finite code without validation prose."""
    too_many = [{"name": f"f{i}"} for i in range(21)]
    r = client.post(
        "/api/v1/kb/doc_types",
        headers={"X-Workspace-Id": "ws_test"},
        json={
            "workspace_id": "ws_test",
            "name": "bad",
            "fields": too_many,
        },
    )
    _assert_code_only(r, 422, "too_many_doc_type_fields")


def test_create_schema_errors_use_finite_codes(client: TestClient) -> None:
    duplicate = client.post(
        "/api/v1/kb/doc_types",
        headers={"X-Workspace-Id": "ws_test"},
        json={
            "workspace_id": "ws_test",
            "name": "duplicate",
            "fields": [{"name": "金额"}, {"name": "金额"}],
        },
    )
    _assert_code_only(duplicate, 422, "duplicate_field_names")

    missing_options = client.post(
        "/api/v1/kb/doc_types",
        headers={"X-Workspace-Id": "ws_test"},
        json={
            "workspace_id": "ws_test",
            "name": "missing options",
            "fields": [{"name": "状态", "type": "select"}],
        },
    )
    _assert_code_only(missing_options, 422, "options_required")

    invalid_field = client.post(
        "/api/v1/kb/doc_types",
        headers={"X-Workspace-Id": "ws_test"},
        json={
            "workspace_id": "ws_test",
            "name": "invalid field",
            "fields": [{"name": ""}],
        },
    )
    _assert_code_only(invalid_field, 422, "invalid_doc_type_schema")


def test_create_internal_store_error_is_not_exposed(
    client: TestClient,
    app_with_stores: FastAPI,
) -> None:
    app_with_stores.state.kb_doc_type_store.create = AsyncMock(
        side_effect=RuntimeError("RAW_SQL_PATH_AND_SCHEMA"),
    )

    response = client.post(
        "/api/v1/kb/doc_types",
        headers={"X-Workspace-Id": "ws_test"},
        json={"workspace_id": "ws_test", "name": "safe failure"},
    )

    _assert_code_only(response, 503, "kb_doc_type_write_unavailable")
    assert "RAW_SQL_PATH_AND_SCHEMA" not in response.text


def test_update_404_for_unknown(client: TestClient) -> None:
    r = client.put(
        "/api/v1/kb/doc_types/dt_does_not_exist",
        headers={"X-Workspace-Id": "ws_test"},
        json={"name": "x"},
    )
    _assert_code_only(r, 404, "doc_type_not_found")


def test_delete_404_for_unknown(client: TestClient) -> None:
    r = client.delete(
        "/api/v1/kb/doc_types/dt_does_not_exist",
        headers={"X-Workspace-Id": "ws_test"},
    )
    _assert_code_only(r, 404, "doc_type_not_found")


def test_missing_doc_type_store_uses_service_code(
    client: TestClient,
    app_with_stores: FastAPI,
) -> None:
    app_with_stores.state.kb_doc_type_store = None

    response = client.get(
        "/api/v1/kb/doc_types?workspace_id=ws_test",
        headers={"X-Workspace-Id": "ws_test"},
    )

    _assert_code_only(response, 503, "kb_doc_type_service_unavailable")


# ─── /draft (LLM extract_schema gen) ───────────────────────


def test_draft_fields_via_mock_llm(client: TestClient) -> None:
    """POST /draft → mock LLM → parsed fields with new schema."""
    r = client.post(
        "/api/v1/kb/doc_types/draft",
        headers={"X-Workspace-Id": "ws_test"},
        json={
            "workspace_id": "ws_test",
            "user_query": "我想从合同抽合同金额、起止日",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "fields" in body
    fields = body["fields"]
    assert len(fields) == 2
    assert fields[0]["name"] == "合同金额"
    assert fields[0]["type"] == "currency"
    assert fields[0]["required"] is True
    assert fields[0]["cross_verify"] is True
    assert fields[0]["extraction_hint"] == "通常在合同首页"
    assert fields[1]["name"] == "起始日"
    assert fields[1]["type"] == "date"


def test_draft_failures_do_not_expose_model_diagnostics(
    client: TestClient,
    app_with_stores: FastAPI,
) -> None:
    app_with_stores.state.router.invoke = AsyncMock(
        side_effect=RuntimeError("RAW_PROVIDER_TOKEN_AND_ENDPOINT"),
    )

    response = client.post(
        "/api/v1/kb/doc_types/draft",
        headers={"X-Workspace-Id": "ws_test"},
        json={
            "workspace_id": "ws_test",
            "user_query": "生成合同字段",
        },
    )

    _assert_code_only(response, 502, "doc_type_draft_failed")
    assert "RAW_PROVIDER_TOKEN_AND_ENDPOINT" not in response.text


def test_draft_invalid_model_response_uses_stable_code(
    client: TestClient,
    app_with_stores: FastAPI,
) -> None:
    app_with_stores.state.router.invoke = AsyncMock(
        return_value=MagicMock(content="RAW_MODEL_OUTPUT_WITH_INTERNAL_CONTEXT"),
    )

    response = client.post(
        "/api/v1/kb/doc_types/draft",
        headers={"X-Workspace-Id": "ws_test"},
        json={
            "workspace_id": "ws_test",
            "user_query": "生成合同字段",
        },
    )

    _assert_code_only(response, 502, "doc_type_draft_invalid_response")
    assert "RAW_MODEL_OUTPUT_WITH_INTERNAL_CONTEXT" not in response.text


# ─── Upload file with doc_type_id (P0-V3-1 cross-ws leak fix) ──


def test_upload_with_invalid_doc_type_id_404(
    client: TestClient,
    app_with_stores: FastAPI,
) -> None:
    """Upload with doc_type_id not in any ws → 404."""
    fake_artifact = MagicMock()
    fake_artifact.put_bytes = AsyncMock(return_value=MagicMock(id="art_x", filename="x.xlsx"))
    fake_worker = MagicMock()
    fake_worker.parse_and_index = AsyncMock()
    app_with_stores.state.artifact_store = fake_artifact
    app_with_stores.state.kb_worker = fake_worker

    r = client.post(
        "/api/v1/kb/files",
        headers={"X-Workspace-Id": "ws_test"},
        files={
            "file": (
                "x.xlsx",
                io.BytesIO(b"\x00" * 100),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={
            "scope": "workspace",
            "scope_id": "ws_test",
            "use_llm": "false",
            "doc_type_id": "dt_does_not_exist",
        },
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"]["code"] == "doc_type_not_found"


async def test_upload_cross_ws_doc_type_leak_blocked(
    client: TestClient,
    app_with_stores: FastAPI,
    tmp_path: Path,
) -> None:
    """Upload with a doc_type from another space → neutral 404 (no enumeration)."""
    dt_store: KbDocTypeStore = app_with_stores.state.kb_doc_type_store
    dt_id_other = await dt_store.create(
        workspace_id="ws_alice",
        name="alice_only",
        created_by="u_alice",
    )

    fake_artifact = MagicMock()
    fake_artifact.put_bytes = AsyncMock(return_value=MagicMock(id="art_x", filename="x.xlsx"))
    fake_worker = MagicMock()
    fake_worker.parse_and_index = AsyncMock()
    app_with_stores.state.artifact_store = fake_artifact
    app_with_stores.state.kb_worker = fake_worker

    r = client.post(
        "/api/v1/kb/files",
        headers={"X-Workspace-Id": "ws_bob"},
        files={
            "file": (
                "x.xlsx",
                io.BytesIO(b"\x00" * 100),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={
            "scope": "workspace",
            "scope_id": "ws_bob",
            "use_llm": "false",
            "doc_type_id": dt_id_other,
        },
    )
    _assert_code_only(r, 404, "doc_type_not_found")
