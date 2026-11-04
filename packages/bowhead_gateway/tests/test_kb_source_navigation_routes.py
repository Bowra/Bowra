"""The source directory reuses live KB authority and never generates on reads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bowhead_core.kb.generations import KbGenerationStore
from bowhead_core.kb.source_navigation_store import SourceNavigationStore
from test_kb_files_management_routes import UID, WS
from test_kb_files_management_routes import production_ctx as production_ctx

pytestmark = pytest.mark.integration


@pytest.fixture
async def navigation(production_ctx):
    client, files = production_ctx
    client.app.state.user_store.get_user = AsyncMock(return_value=SimpleNamespace(status="active"))
    client.app.state.user_store.get_team_llm_config_with_version = AsyncMock(
        return_value=(
            {
                "providers": {
                    "team": {
                        "type": "openai_compatible",
                        "base_url": "https://models.invalid",
                        "models": [{"id": "private-model", "capabilities": ["chat"]}],
                    }
                },
                "kb": {
                    "active": {"source_description": "source"},
                    "models": {"source": {"capability": "source_description", "provider": "team", "real_id": "private-model"}},
                },
            },
            1,
        )
    )

    async def create(owner=UID):
        identity = await files.create(
            artifact_id="art-test",
            filename="Travel policy.md",
            mime_type="text/markdown",
            size_bytes=100,
            scope="space",
            scope_id=WS,
            workspace_id=WS,
            team_id=WS,
            uploaded_by=owner,
        )
        file = await files.get(identity)
        generations = KbGenerationStore(files._db_path)
        generation = await generations.begin(file, "a" * 64, 1, "index-a")
        assert await generations.publish(file, generation, layer="L0", duration_ms=1, summary_json=None)
        return await files.get(identity)

    return client, files, SourceNavigationStore(files._db_path), create


async def test_maintainer_edits_and_explicit_generation_are_versioned_and_idempotent(navigation):
    client, _, store, create = navigation
    file = await create()
    headers = {"X-Workspace-Id": WS}
    path = f"/api/v1/kb/files/{file.file_id}"
    before = client.get(f"{path}/navigation", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["generation_model"] == "private-model"
    assert await store.latest_job(file.file_id) is None
    payload = {
        "expected_generation_id": file.active_generation_id,
        "expected_source_version": file.version,
        "expected_revision": 0,
        "action_id": "explicit-action-a",
    }
    first = client.post(f"{path}/description-jobs", headers=headers, json=payload)
    second = client.post(f"{path}/description-jobs", headers=headers, json=payload)
    assert first.status_code == second.status_code == 200, first.text
    assert first.json()["job_id"] == second.json()["job_id"]
    receipt = client.get(f"{path}/navigation", headers=headers).json()["description_job"]
    assert receipt["action_id"] == payload["action_id"]
    saved = client.patch(
        f"{path}/navigation",
        headers=headers,
        json={
            "expected_generation_id": file.active_generation_id,
            "expected_source_version": file.version,
            "expected_revision": 0,
            "manual_description": "Travel reimbursement procedures.",
        },
    )
    assert saved.status_code == 200, saved.text
    stale = client.patch(
        f"{path}/navigation",
        headers=headers,
        json={
            "expected_generation_id": file.active_generation_id,
            "expected_source_version": file.version,
            "expected_revision": 0,
            "manual_description": "Stale",
        },
    )
    assert stale.status_code == 409
    assert client.get(f"{path}/navigation", headers=headers).json()["description"] == "Travel reimbursement procedures."


async def test_readers_do_not_receive_model_names_actor_ids_or_mutation_rights(navigation):
    client, _, store, create = navigation
    file = await create(owner="u_other")
    await store.save_category(team_id=WS, scope="space", scope_id=WS, name="Travel", aliases=[], parent_id=None, actor_user_id="sensitive-actor")
    result = client.get(f"/api/v1/kb/files/{file.file_id}/navigation", headers={"X-Workspace-Id": WS})
    assert result.status_code == 200, result.text
    assert not result.json()["can_edit"]
    assert result.json()["generation_model"] is None
    assert "sensitive-actor" not in result.text and "private-model" not in result.text
    assert await store.latest_job(file.file_id) is None


async def test_current_resource_denial_removes_sources_and_category_metadata(navigation, monkeypatch):
    client, _, store, create = navigation
    await create()
    await store.save_category(team_id=WS, scope="space", scope_id=WS, name="Restricted taxonomy", aliases=[], parent_id=None, actor_user_id=UID)
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = client.app.state.access_control_store
    headers = {"X-Workspace-Id": WS}
    assert client.get("/api/v1/kb/sources", headers=headers).json()["sources"]
    await access.create_or_update_resource_grant(
        team_id=WS, resource_kind="kb_scope", resource_id=f"workspace:{WS}", principal_type="user", principal_id=UID, permission="read", effect="deny"
    )
    result = client.get("/api/v1/kb/sources", headers=headers)
    assert result.status_code == 200, result.text
    assert not result.json()["sources"]
    assert "Travel policy" not in result.text and "Restricted taxonomy" not in client.get("/api/v1/kb/source-categories", headers=headers).text


async def test_optional_model_lookup_failure_preserves_source_navigation(navigation):
    client, _, _, create = navigation
    file = await create()
    client.app.state.user_store.get_team_llm_config_with_version = AsyncMock(side_effect=RuntimeError("private diagnostic"))
    result = client.get(f"/api/v1/kb/files/{file.file_id}/navigation", headers={"X-Workspace-Id": WS})
    assert result.status_code == 200, result.text
    assert result.json()["generation_status"] == "unavailable"
    assert "private diagnostic" not in result.text


async def test_admitted_generation_uses_current_team_router_and_settles_known_usage_once(navigation):
    import json
    from unittest.mock import Mock

    from bowhead_core.kb.source_description import description_assignment
    from bowhead_gateway.kb_source_navigation import run_description_job
    from bowhead_router.types import LLMResponse, TokenUsage

    client, _, store, create = navigation
    file = await create()
    app = client.app
    app.state.user_store.get_user = AsyncMock(return_value=SimpleNamespace(status="active", credit_balance=100))
    app.state.user_store.get_team = AsyncMock(return_value=SimpleNamespace(team_credit_balance=100))
    app.state.user_store.try_deduct_credit = AsyncMock(return_value=(True, ""))
    router = SimpleNamespace(
        bind=Mock(),
        invoke=AsyncMock(
            return_value=LLMResponse(
                content=json.dumps({"description": "Travel policy and reimbursement.", "topics": ["Travel"], "supporting_chunks": ["chunk-a"]}),
                usage=TokenUsage(input_tokens=100, output_tokens=20),
                provider="team",
                model="private-model",
            )
        ),
        max_retries=5,
        model_credit_rates={},
        model_provider_types={"team": "openai_compatible"},
    )
    app.state.team_router_registry = SimpleNamespace(get=AsyncMock(return_value=router))
    runtime = SimpleNamespace(
        kb_store=SimpleNamespace(
            sample_current_chunks=AsyncMock(
                return_value=[("chunk-a", "Travel policy and reimbursement procedures.", {"generation_id": file.active_generation_id})]
            )
        )
    )
    app.state.team_kb_runtime_registry = SimpleNamespace(get=AsyncMock(return_value=runtime))
    config, _ = await app.state.user_store.get_team_llm_config_with_version(WS)
    args = {"fingerprint": description_assignment(config).fingerprint, "actor_user_id": UID, "workspace_id": WS, "revision": 0}
    await store.enqueue(file, admission_key="generation-first", **args)
    await store.enqueue(file, admission_key="equivalent-admission", **args)
    await run_description_job(app, await store.claim("worker-a"))
    await run_description_job(app, await store.claim("worker-a"))
    assert (await store.latest_job(file.file_id))["state"] == "ready"
    assert (await store.file_wide_descriptions(file))[0]["text"] == "Travel policy and reimbursement."
    router.invoke.assert_awaited_once()
    assert router.invoke.call_args.kwargs["retry_policy"].max_attempts_per_target == 2
    app.state.user_store.try_deduct_credit.assert_awaited_once()
    assert app.state.user_store.try_deduct_credit.call_args.kwargs["reason"] == "kb_source_description"
    assert file.active_generation_id == (await app.state.kb_files_store.get(file.file_id)).active_generation_id


async def test_native_matrix_role_reuses_only_current_unedited_origin_and_is_not_copied(navigation, tmp_path):
    from bowhead_core.kb.matrix_store import KbMatrixStore
    from bowhead_gateway.kb_source_navigation import refresh_matrix_navigation

    client, files, _, create = navigation
    file = await create()
    from gateway_legacy_database import upgrade

    assert upgrade(f"sqlite+aiosqlite:///{(tmp_path / 'requested.db').with_name('bowhead.db')}", target="matrix") == ("matrix",)
    matrices = KbMatrixStore(tmp_path / "kb_matrix.db")
    await matrices.initialize()
    client.app.state.kb_matrix_store = matrices
    matrix = await matrices.create_matrix(
        name="Source review",
        workspace_id=WS,
        team_id=WS,
        created_by=UID,
        files=[{"file_id": file.file_id}],
        columns=[{"name": "Summary", "type": "string"}],
    )
    column = matrix.columns[0]
    await matrices.update_column(matrix.matrix_id, column["column_id"], workspace_id=WS, navigation_role="description")
    await matrices.enqueue_cells(
        matrix.matrix_id,
        [(file.file_id, column["column_id"])],
        run_id="run-native",
        workspace_id=WS,
        team_id=WS,
        column_versions={column["column_id"]: 1},
        file_versions={file.file_id: file.version},
    )
    await matrices.finalize_cell(
        matrix.matrix_id,
        file.file_id,
        column["column_id"],
        status="done",
        variants=[
            {
                "is_primary": True,
                "extracted_by": "tier1",
                "chunk_id": "chunk-native",
                "source_excerpt": "Travel policy",
                "normalized": {"type": "string", "value": "Native travel policy description"},
            }
        ],
        prompt_version=1,
        file_version=file.version,
        kb_generation_id=file.active_generation_id,
        generation_db_path=files._db_path,
    )
    matrix = await matrices.get_matrix(matrix.matrix_id, workspace_id=WS)
    assert matrix.columns[0]["prompt_version"] == 1
    await refresh_matrix_navigation(
        client.app,
        SimpleNamespace(user_id=UID, team_id=WS, workspace_id=WS),
        matrix,
        {file.file_id: file},
        await matrices.list_cells(matrix.matrix_id),
    )
    path = f"/api/v1/kb/files/{file.file_id}/navigation"
    result = client.get(path, headers={"X-Workspace-Id": WS})
    assert result.status_code == 200, result.text
    assert result.json()["description_kind"] == "matrix"
    copied = await matrices.create_matrix(
        name="Copied structure", workspace_id=WS, team_id=WS, created_by=UID, files=[{"file_id": file.file_id}], columns=matrix.columns
    )
    assert "navigation_role" not in copied.columns[0]
    await matrices.update_column(matrix.matrix_id, column["column_id"], workspace_id=WS, navigation_role="none")
    assert client.get(path, headers={"X-Workspace-Id": WS}).json()["description_kind"] == "name"
