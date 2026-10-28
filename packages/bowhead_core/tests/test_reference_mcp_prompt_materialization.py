from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import mcp.types
import pytest
from bowhead_core.chat_reference_projection import read_reference_projection, reference_resource_identity
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.chat_references import ReferenceNode
from bowhead_core.mcp_prompt_expansion import prepare_mcp_prompt_expansion
from bowhead_core.reference_mcp_prompt_execution import (
    MCP_PROMPT_RESULT_CONTENT_KEY_PREFIX,
    materialize_reference_mcp_prompt_results,
)
from cryptography.fernet import Fernet
from test_reference_mcp_preparation import _admitted_prompt_worker_case

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _remote_result(text: str) -> mcp.types.GetPromptResult:
    return mcp.types.GetPromptResult(
        description="Remote template",
        messages=[
            mcp.types.PromptMessage(
                role="user",
                content=mcp.types.TextContent(type="text", text=text),
            )
        ],
    )


async def _claimed_case(tmp_path, *, turn_id: str, prompt_count: int = 1, lead: bool = False):
    from bowhead_core.reference_mcp_prompt_execution import claim_reference_mcp_prompt_primary_for_worker

    case, _agent, _prompt, admission, source = await _admitted_prompt_worker_case(
        tmp_path,
        turn_id=turn_id,
        prompt_count=prompt_count,
        lead=lead,
    )
    selection = admission.mcp_prompt_selection
    assert selection is not None
    scope = selection.scope.model_copy(update={"session_id": source.session_id})
    claim = await claim_reference_mcp_prompt_primary_for_worker(
        index=admission.mcp_index,
        access=admission.mcp_access,
        scope=scope,
        turn_id=turn_id,
        owner_id="prompt-worker",
        expected_generation=1,
    )
    assert claim.outcome == "claimed"
    return case, admission, source, scope, ReferenceCipher(Fernet.generate_key().decode())


@pytest.mark.parametrize("lead", [False, True])
async def test_materializes_all_prompt_results_atomically_and_restores_without_remote(tmp_path, lead):
    turn_id = "t_reference_mcp_prompt_results"
    case, admission, source, scope, cipher = await _claimed_case(
        tmp_path,
        turn_id=turn_id,
        prompt_count=2,
        lead=lead,
    )
    calls: list[tuple[str, dict[str, str], str]] = []

    async def fetch(resource, parameters, source_revision):
        calls.append((resource.prompt_name, parameters, source_revision))
        return prepare_mcp_prompt_expansion(
            resource=resource,
            parameters=parameters,
            result=_remote_result(f"Analyze {resource.prompt_name}"),
        )

    materialized = await materialize_reference_mcp_prompt_results(
        index=admission.mcp_index,
        access=admission.mcp_access,
        cipher=cipher,
        scope=scope,
        turn_id=turn_id,
        owner_id="prompt-worker",
        expected_generation=1,
        fetch=fetch,
    )
    assert not materialized.reused
    assert [item[0] for item in calls] == ["profitability", "regional_profitability"]
    assert [document.parameters for document in materialized.documents] == [
        {"period": "FY2025"},
        {"region": "APAC"},
    ]
    assert [item.content.content_key for item in materialized.resolution.results] == [
        f"{MCP_PROMPT_RESULT_CONTENT_KEY_PREFIX}1",
        f"{MCP_PROMPT_RESULT_CONTENT_KEY_PREFIX}2",
    ]
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT resolution_seq,idempotency_key FROM chat_reference_resolutions WHERE turn_id=? ORDER BY resolution_seq",
            (turn_id,),
        ).fetchall() == [
            (1, "reference:primary"),
            (3 if lead else 2, "reference:primary:mcp-prompt-results"),
        ]
        assert db.execute(
            "SELECT content_key,retention_state FROM chat_reference_content_snapshots WHERE turn_id=? ORDER BY content_key",
            (turn_id,),
        ).fetchall() == [
            (f"{MCP_PROMPT_RESULT_CONTENT_KEY_PREFIX}1", "available"),
            (f"{MCP_PROMPT_RESULT_CONTENT_KEY_PREFIX}2", "available"),
        ]
        primary_digest = db.execute(
            "SELECT composition_digest FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1",
            (turn_id,),
        ).fetchone()[0]

    visible = frozenset(reference_resource_identity(node.resource) for node in source.input_document.nodes if isinstance(node, ReferenceNode))
    projection = await read_reference_projection(
        case.fixture.db_path,
        scope=ReferenceScope(**scope.model_dump(), policy_epoch=admission.policy_epoch),
        turn_id=turn_id,
        visible_resource_identities=visible,
        visibility_complete=True,
        include_content=True,
        cipher=cipher,
    )
    assert projection["composition"]["resolution_seq"] == 1
    assert projection["composition"]["composition_digest"] == primary_digest
    if lead:
        assert projection["delegated_payload"] is None
    else:
        assert projection["delegated_payload"]["content_hash"] is not None

    async def must_not_fetch(*_args):
        raise AssertionError("committed Prompt results must not access the remote server")

    restored = await materialize_reference_mcp_prompt_results(
        index=admission.mcp_index,
        access=admission.mcp_access,
        cipher=cipher,
        scope=scope,
        turn_id=turn_id,
        owner_id="prompt-worker",
        expected_generation=1,
        fetch=must_not_fetch,
    )
    assert restored.reused
    assert restored.resolution == materialized.resolution
    assert restored.documents == materialized.documents


@pytest.mark.parametrize("lead", [False, True])
@pytest.mark.parametrize("changed", ["source", "worker"])
async def test_materialization_rolls_back_when_final_fence_changes(tmp_path, changed, lead):
    turn_id = f"t_reference_mcp_prompt_result_changed_{changed}"
    case, admission, _source, scope, cipher = await _claimed_case(tmp_path, turn_id=turn_id, lead=lead)

    async def fetch(resource, parameters, _source_revision):
        with sqlite3.connect(case.fixture.db_path) as db:
            if changed == "source":
                db.execute(
                    "UPDATE team_tool_source SET updated_at=? WHERE id='ots_prompts'",
                    (datetime.now(UTC).isoformat(),),
                )
            else:
                db.execute(
                    "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
                    ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), turn_id),
                )
        return prepare_mcp_prompt_expansion(
            resource=resource,
            parameters=parameters,
            result=_remote_result("Uncommitted result"),
        )

    with pytest.raises(ValueError):
        await materialize_reference_mcp_prompt_results(
            index=admission.mcp_index,
            access=admission.mcp_access,
            cipher=cipher,
            scope=scope,
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
            fetch=fetch,
        )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=2",
            (turn_id,),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_content_snapshots WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


async def test_materialization_stops_before_next_remote_after_authority_change(tmp_path):
    turn_id = "t_reference_mcp_prompt_result_mid_batch_revoke"
    case, admission, _source, scope, cipher = await _claimed_case(
        tmp_path,
        turn_id=turn_id,
        prompt_count=2,
    )
    calls: list[str] = []

    async def fetch(resource, parameters, _source_revision):
        calls.append(resource.prompt_name)
        if len(calls) == 1:
            with sqlite3.connect(case.fixture.db_path) as db:
                db.execute(
                    "UPDATE team_tool_source SET updated_at=? WHERE id='ots_prompts'",
                    (datetime.now(UTC).isoformat(),),
                )
        return prepare_mcp_prompt_expansion(
            resource=resource,
            parameters=parameters,
            result=_remote_result("Uncommitted result"),
        )

    with pytest.raises(ValueError):
        await materialize_reference_mcp_prompt_results(
            index=admission.mcp_index,
            access=admission.mcp_access,
            cipher=cipher,
            scope=scope,
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
            fetch=fetch,
        )
    assert calls == ["profitability"]
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=2",
            (turn_id,),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_content_snapshots WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("lead", [False, True])
async def test_materialization_rejects_result_for_different_parameters_without_partial_snapshot(tmp_path, lead):
    turn_id = "t_reference_mcp_prompt_result_parameters"
    case, admission, _source, scope, cipher = await _claimed_case(tmp_path, turn_id=turn_id, lead=lead)

    async def fetch(resource, _parameters, _source_revision):
        return prepare_mcp_prompt_expansion(
            resource=resource,
            parameters={"period": "FY2024"},
            result=_remote_result("Wrong period"),
        )

    with pytest.raises(ValueError, match="reference_mcp_prompt_result_changed"):
        await materialize_reference_mcp_prompt_results(
            index=admission.mcp_index,
            access=admission.mcp_access,
            cipher=cipher,
            scope=scope,
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
            fetch=fetch,
        )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=2",
            (turn_id,),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_content_snapshots WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("lead", [False, True])
async def test_materialization_rolls_back_snapshots_when_result_resolution_insert_fails(tmp_path, lead):
    turn_id = "t_reference_mcp_prompt_result_atomic_failure"
    case, admission, _source, scope, cipher = await _claimed_case(
        tmp_path,
        turn_id=turn_id,
        prompt_count=2,
        lead=lead,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "CREATE TRIGGER fail_prompt_result_resolution BEFORE INSERT ON chat_reference_resolutions "
            "WHEN NEW.idempotency_key='reference:primary:mcp-prompt-results' "
            "BEGIN SELECT RAISE(ABORT,'injected Prompt result resolution failure'); END",
        )

    async def fetch(resource, parameters, _source_revision):
        return prepare_mcp_prompt_expansion(
            resource=resource,
            parameters=parameters,
            result=_remote_result(f"Analyze {resource.prompt_name}"),
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected Prompt result resolution failure"):
        await materialize_reference_mcp_prompt_results(
            index=admission.mcp_index,
            access=admission.mcp_access,
            cipher=cipher,
            scope=scope,
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
            fetch=fetch,
        )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=? AND idempotency_key='reference:primary:mcp-prompt-results'",
            (turn_id,),
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_content_snapshots WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)
        db.execute("DROP TRIGGER fail_prompt_result_resolution")

    committed = await materialize_reference_mcp_prompt_results(
        index=admission.mcp_index,
        access=admission.mcp_access,
        cipher=cipher,
        scope=scope,
        turn_id=turn_id,
        owner_id="prompt-worker",
        expected_generation=1,
        fetch=fetch,
    )
    assert not committed.reused

    async def must_not_fetch(*_args):
        raise AssertionError("committed retry must recover without remote I/O")

    restored = await materialize_reference_mcp_prompt_results(
        index=admission.mcp_index,
        access=admission.mcp_access,
        cipher=cipher,
        scope=scope,
        turn_id=turn_id,
        owner_id="prompt-worker",
        expected_generation=1,
        fetch=must_not_fetch,
    )
    assert restored.reused
    assert restored.resolution == committed.resolution


@pytest.mark.parametrize(
    "missing_trigger",
    [
        "trg_reference_mcp_prompt_result_snapshot",
        "trg_reference_mcp_prompt_result_resolution",
    ],
)
async def test_materialization_requires_result_migration_before_remote_io(tmp_path, missing_trigger):
    turn_id = f"t_reference_mcp_prompt_result_migration_{missing_trigger}"
    case, admission, _source, scope, cipher = await _claimed_case(tmp_path, turn_id=turn_id)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(f"DROP TRIGGER {missing_trigger}")
    calls = 0

    async def fetch(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError

    with pytest.raises(ValueError, match="reference_mcp_prompt_result_migration_required"):
        await materialize_reference_mcp_prompt_results(
            index=admission.mcp_index,
            access=admission.mcp_access,
            cipher=cipher,
            scope=scope,
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
            fetch=fetch,
        )
    assert calls == 0


async def test_result_migration_keeps_receiver_origin_guard_for_missing_policy(tmp_path):
    turn_id = "t_reference_mcp_prompt_missing_policy"
    case, _admission, _source, _scope, _cipher = await _claimed_case(tmp_path, turn_id=turn_id)
    with sqlite3.connect(case.fixture.db_path) as db:
        saved = json.loads(
            db.execute(
                "SELECT composition_json FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1",
                (turn_id,),
            ).fetchone()[0],
        )
        saved.pop("runtime_policy")
        saved.pop("node_id")
        saved["receiver_source"] = "lead"
        payload = json.dumps(saved, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
        with pytest.raises(sqlite3.IntegrityError, match="reference composition receiver origin mismatch"):
            db.execute(
                "INSERT INTO chat_reference_resolutions("
                "turn_id,resolution_seq,schema_version,kind,idempotency_key,source_turn_id,created_generation,"
                "execution_id,composition_digest,composition_json,created_at) "
                "VALUES (?,3,1,'composition','reference:forged-missing-policy',?,1,?,?,?,?)",
                (
                    turn_id,
                    turn_id,
                    saved["execution_id"],
                    digest,
                    payload,
                    datetime.now(UTC).isoformat(),
                ),
            )


async def test_lead_prompt_concurrent_materialization_commits_one_association(tmp_path):
    import asyncio

    turn_id = "t_prompt_concurrent_lead"
    case, admission, source, scope, cipher = await _claimed_case(tmp_path, turn_id=turn_id, lead=True)
    fetched = 0
    both_fetched = asyncio.Event()

    async def fetch(resource, parameters, _revision):
        nonlocal fetched
        fetched += 1
        if fetched == 2:
            both_fetched.set()
        await asyncio.wait_for(both_fetched.wait(), 10)
        return prepare_mcp_prompt_expansion(resource=resource, parameters=parameters, result=_remote_result("Retained template"))

    async def materialize(**overrides):
        return await materialize_reference_mcp_prompt_results(
            **{
                "index": admission.mcp_index,
                "access": admission.mcp_access,
                "cipher": cipher,
                "scope": scope,
                "turn_id": turn_id,
                "owner_id": "prompt-worker",
                "expected_generation": 1,
                "fetch": fetch,
                **overrides,
            }
        )

    for override in ({"owner_id": "other-worker"}, {"expected_generation": 2}, {"turn_id": "other-turn"}):
        with pytest.raises(ValueError):
            await materialize(**override)
    assert fetched == 0
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.reference_mcp_prompt_model import ReferenceMcpPromptModelAccess

    with sqlite3.connect(case.fixture.db_path) as db:
        execution, digest = db.execute(
            "SELECT execution_id,composition_digest FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1", (turn_id,)
        ).fetchone()
    model_access = ReferenceMcpPromptModelAccess(
        index=admission.mcp_index,
        access=admission.mcp_access,
        cipher=cipher,
        scope=ReferenceScope(**scope.model_dump(), policy_epoch=admission.policy_epoch),
        turn_id=turn_id,
        owner_id="prompt-worker",
        generation=1,
        execution_id=execution,
        composition_digest=digest,
    )
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        with pytest.raises(ValueError, match="reference_mcp_prompt_receiver_changed"):
            await model_access.read_in_connection(db)
    results = await asyncio.gather(materialize(), materialize())
    assert sorted(result.reused for result in results) == [False, True]
    assert results[0].resolution == results[1].resolution
    assert {result.resolution_seq for result in results} == {3}
    with sqlite3.connect(case.fixture.db_path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=? AND idempotency_key='reference:primary:mcp-prompt-results'",
                (turn_id,),
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM chat_reference_content_snapshots WHERE turn_id=?", (turn_id,)).fetchone()[0] == 1
    again = await materialize()
    assert again.reused and again.resolution_seq == 3 and fetched == 2


@pytest.mark.parametrize("change", ["position", "version", "generation", "source", "execution", "digest"])
async def test_lead_prompt_result_sql_rejects_changed_identity(tmp_path, change):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.reference_mcp_prompt_execution import _require_prompt_runtime_in_connection, _result_resolution

    turn_id = "t_prompt_guard_lead"
    case, admission, source, scope, cipher = await _claimed_case(tmp_path, turn_id=turn_id, lead=True)
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        primary, composition = await _require_prompt_runtime_in_connection(
            db,
            index=admission.mcp_index,
            access=admission.mcp_access,
            scope=ReferenceScope(**scope.model_dump(), policy_epoch=admission.policy_epoch),
            turn_id=turn_id,
            owner_id="prompt-worker",
            generation=1,
            bindings=AgentExecutionBindingStore(case.fixture.db_path),
        )
    prompt = composition.prompts[0]
    expansion = prepare_mcp_prompt_expansion(resource=prompt.resource, parameters={"period": "FY2025"}, result=_remote_result("Material"))
    result, _, _ = _result_resolution(primary=primary, composition=composition, expansions=(expansion,))
    descriptor = result.results[0].content
    # Use the supported snapshot writer, then probe a changed result in the same
    # transaction so no invalid or partial material survives the test.
    from bowhead_core.chat_reference_storage import ChatReferenceStore

    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await ChatReferenceStore(cipher).put_content_in_connection(
            db,
            scope=ReferenceScope(**scope.model_dump(), policy_epoch=admission.policy_epoch),
            turn_id=turn_id,
            descriptor=descriptor,
            content=expansion.content,
            generation=1,
            owner_id="prompt-worker",
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        value = result.model_dump(mode="json")
        if change == "version":
            value["runtime_policy"] = "lead-mcp-prompt-results.v99"
        if change in {"source", "execution"}:
            value["source_turn_id" if change == "source" else "execution_id"] = "other"
        if change == "digest":
            value["primary_composition_digest"] = "sha256:" + "0" * 64
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO chat_reference_resolutions(turn_id,resolution_seq,schema_version,kind,idempotency_key,source_turn_id,created_generation,execution_id,composition_digest,composition_json,created_at) VALUES(?,?,1,'composition','reference:primary:mcp-prompt-results',?,?,?,?,?,?)",
                (
                    turn_id,
                    2 if change == "position" else 3,
                    value["source_turn_id"],
                    2 if change == "generation" else 1,
                    value["execution_id"],
                    result.digest,
                    json.dumps(value, sort_keys=True, separators=(",", ":")),
                    datetime.now(UTC).isoformat(),
                ),
            )
        await db.rollback()
