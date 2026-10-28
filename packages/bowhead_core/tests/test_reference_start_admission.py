"""Real submission claims atomically become a single exact reference Turn."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import anyio
import pytest
import pytest_asyncio
from bowhead_core.chat_reference_admission import ReferenceStartAdmission
from bowhead_core.chat_references import InputDocument
from bowhead_core.chat_turn_source import ReferenceExecutionUnavailableError
from bowhead_core.session_store import SessionProtocolManagedError, SessionStore
from test_agent_admission_transaction import _system_admission_case, _team_admission_case

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture(params=["team", "system"])
async def start_case(tmp_path: Path, request: pytest.FixtureRequest) -> SimpleNamespace:
    case = await (_team_admission_case(tmp_path) if request.param == "team" else _system_admission_case(tmp_path))
    fixture = case.fixture
    store = SessionStore(fixture.db_path)
    assert await store.register_chat_auth_session_generation(user_id=fixture.member_id, auth_session_generation_hash="0" * 64)
    resource = {"kind": "agent", "source_kind": case.source_kind, "id": case.expected.agent_id, "version": case.expected.revision_ref}
    if case.expected.assignment_id is not None:
        resource.update(assignment_id=case.expected.assignment_id, release_id=case.expected.release_id)
    document = InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                {"type": "reference", "node_id": str(uuid4()), "resource": resource, "label": "利润分析师", "parameters": {}},
                {"type": "text", "text": "分析利润变化"},
            ],
        }
    )
    admission = ReferenceStartAdmission(
        input_document=document,
        policy_epoch=case.epoch,
        profile_name=case.args["profile_name"],
        expected=case.expected,
        system_revision=case.args.get("prepared_revision"),
        manifest_fingerprint=case.args.get("manifest_fingerprint"),
        override_version=case.args.get("override_version"),
    )
    common = {
        "team_id": fixture.team_id,
        "user_id": fixture.member_id,
        "client_submission_id": str(uuid4()),
        "workspace_id": fixture.workspace_id,
        "requested_session_id": None,
        "submission_kind": "start",
        "submission_contract_version": "chat-references.v1",
        "model_control_batch": "references",
        "request_fingerprint": "1" * 64,
        "accepted_request_fingerprints": ("1" * 64,),
        "fingerprint_key_id": "2" * 64,
        "auth_session_generation_hash": "0" * 64,
    }
    claim = await store.claim_chat_submission(**common)
    assert claim.kind == "claimed"
    args = {
        key: value
        for key, value in common.items()
        if key not in {"accepted_request_fingerprints", "fingerprint_key_id", "auth_session_generation_hash"}
    }
    args.update(
        actual_session_id="s_reference",
        turn_id="t_reference",
        user_text=document.display_text(),
        attachment_ids=[],
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        response={"session_id": "s_reference", "turn_id": "t_reference", "status": "started", "stream_url": "/api/v1/chat/stream/t_reference"},
        reference_start=admission,
    )
    return SimpleNamespace(agent=case, fixture=fixture, store=store, common=common, args=args)


def _assert_no_acceptance(case: SimpleNamespace, *, existing_session: bool = False) -> None:
    with sqlite3.connect(case.fixture.db_path) as db:
        for table in (
            "chat_turn_user_sources",
            "chat_reference_plans",
            "turn_agent_binding",
            "turn_agent_execution",
            "chat_submission_receipts",
            "chat_turn_spawn_dispatch",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE turn_id='t_reference'").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM turns WHERE id='t_reference'").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE id='s_reference'").fetchone() == (int(existing_session),)
        assert db.execute("SELECT COUNT(*) FROM chat_submission_inflight").fetchone() == (1,)
        if case.agent.source_kind == "system":
            assert db.execute("SELECT COUNT(*) FROM system_agent_effective_revision").fetchone() == (0,)
            assert db.execute("SELECT COUNT(*) FROM system_agent_revision_behavior").fetchone() == (0,)


async def test_start_commits_source_plan_primary_receipt_and_dispatch_once(start_case: SimpleNamespace) -> None:
    case = start_case
    first = await case.store.finalize_started_chat_submission(**case.args)
    assert first.outcome == "started" and first.response["execution_generation"] == 1
    with sqlite3.connect(case.fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        binding = db.execute("SELECT * FROM turn_agent_binding WHERE turn_id='t_reference'").fetchone()
        execution = db.execute("SELECT * FROM turn_agent_execution WHERE id=?", (binding["primary_execution_id"],)).fetchone()
        assert execution["turn_id"] == "t_reference" and execution["status"] == "admitted"
        assert execution["revision_ref"] == case.agent.expected.revision_ref
        assert execution["dispatch_source"] == "explicit" and execution["dispatch_key"] == "reference:primary"
        source = db.execute("SELECT input_document_json FROM chat_turn_user_sources WHERE turn_id='t_reference'").fetchone()
        assert InputDocument.model_validate_json(source[0]) == case.args["reference_start"].input_document
        plan = json.loads(db.execute("SELECT plan_json FROM chat_reference_plans WHERE turn_id='t_reference'").fetchone()[0])
        assert plan["admissions"][0]["policy_digest"] == binding["policy_fingerprint"]
        assert db.execute("SELECT active_turn_id FROM sessions WHERE id='s_reference'").fetchone()[0] == "t_reference"
        assert tuple(
            db.execute("SELECT state,owner_id,source_submission_id FROM chat_turn_spawn_dispatch WHERE turn_id='t_reference'").fetchone()
        ) == ("claimed", None, case.common["client_submission_id"])
        assert db.execute("SELECT COUNT(*) FROM chat_submission_inflight").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM turn_events WHERE turn_id='t_reference'").fetchone()[0] == 0
        # Receipt recovery must not re-admit after Agent access is withdrawn.
        db.execute("UPDATE agent_assignment SET personal_state='disabled' WHERE agent_id=?", (case.agent.expected.agent_id,))
    assert await case.store.finalize_started_chat_submission(**case.args) == first
    claim = await case.store.claim_chat_submission(**case.common)
    assert claim.kind == "receipt" and claim.receipt == first
    with pytest.raises(ReferenceExecutionUnavailableError):
        await case.store.require_legacy_chat_execution(turn_id="t_reference")
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == (1,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("stage", ["chat_reference_plans", "turn_agent_binding", "chat_submission_receipts", "chat_turn_spawn_dispatch"])
async def test_late_failure_rolls_back_all_facts_and_retains_claim(start_case: SimpleNamespace, stage: str) -> None:
    case = start_case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(f"CREATE TRIGGER fail_reference_start BEFORE INSERT ON {stage} BEGIN SELECT RAISE(ABORT,'forced late failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced late failure"):
        await case.store.finalize_started_chat_submission(**case.args)
    _assert_no_acceptance(case)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("DROP TRIGGER fail_reference_start")
    assert (await case.store.finalize_started_chat_submission(**case.args)).outcome == "started"


@pytest.mark.parametrize("denial", ["lease", "fingerprint", "expired", "auth", "scope", "contract", "policy", "assignment", "space"])
async def test_start_rechecks_claim_and_current_authority(start_case: SimpleNamespace, denial: str) -> None:
    case = start_case
    args = dict(case.args)
    with sqlite3.connect(case.fixture.db_path) as db:
        if denial == "lease":
            args["lease_version"] += 1
            code = "submission_claim_lost"
        elif denial == "fingerprint":
            args["request_fingerprint"] = "3" * 64
            code = "submission_claim_lost"
        elif denial == "expired":
            db.execute("UPDATE chat_submission_inflight SET lease_expires_at='2000-01-01T00:00:00+00:00'")
            code = "submission_claim_lost"
        elif denial == "auth":
            db.execute("UPDATE chat_auth_session_generations SET state='invalidated'")
            code = "submission_auth_generation_inactive"
        elif denial == "scope":
            db.execute("UPDATE chat_submission_inflight SET requested_session_id='s_different'")
            code = "submission_session_scope_conflict"
        elif denial == "contract":
            db.execute("UPDATE chat_submission_inflight SET submission_contract_version='chat-submit.v1',model_control_batch='a'")
            code = "reference_submission_contract_invalid"
        elif denial == "policy":
            args["reference_start"] = replace(args["reference_start"], policy_epoch=case.agent.epoch + 1)
            code = "reference_policy_changed"
        elif denial == "assignment":
            db.execute("UPDATE agent_assignment SET personal_state='disabled' WHERE agent_id=?", (case.agent.expected.agent_id,))
            code = "agent_assignment_inactive"
        else:
            db.execute("UPDATE workspace_members SET status='removed' WHERE user_id=?", (case.fixture.member_id,))
            code = "agent_turn_scope_invalid"
    with pytest.raises((ValueError, RuntimeError), match=code):
        await case.store.finalize_started_chat_submission(**args)
    _assert_no_acceptance(case)


async def test_reclaimed_lease_fences_old_finalizer(start_case: SimpleNamespace) -> None:
    case = start_case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE chat_submission_inflight SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    claim = await case.store.claim_chat_submission(**case.common)
    assert claim.kind == "claimed" and claim.lease_version == case.args["lease_version"] + 1
    with pytest.raises(ValueError, match="submission_claim_lost"):
        await case.store.finalize_started_chat_submission(**case.args)
    _assert_no_acceptance(case)
    assert (
        await case.store.finalize_started_chat_submission(**{**case.args, "lease_owner": claim.lease_owner, "lease_version": claim.lease_version})
    ).outcome == "started"


async def test_concurrent_finalizers_return_one_receipt(start_case: SimpleNamespace) -> None:
    receipts = []

    async def finalize() -> None:
        receipts.append(await start_case.store.finalize_started_chat_submission(**start_case.args))

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(finalize)
            group.start_soon(finalize)
    assert len(receipts) == 2 and receipts[0] == receipts[1]
    with sqlite3.connect(start_case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM turns WHERE id='t_reference'").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id='t_reference'").fetchone() == (1,)


@pytest.mark.parametrize("protocol", ["legacy", "terminal_v1"])
async def test_start_preserves_an_existing_active_session(start_case: SimpleNamespace, protocol: str) -> None:
    from bowhead_core.terminal_protocol import AdmissionContext, TerminalAdmissionRequest
    from bowhead_core.terminal_protocol_store import AdmissionDisposition, TerminalProtocolStore
    from bowhead_core.terminal_runtime_factory import terminal_auth_resolve_context
    from bowhead_core.types import TurnRequest

    case = start_case
    fixture = case.fixture
    await case.store.ensure_session(session_id="s_reference", workspace_id=fixture.workspace_id, user_id=fixture.member_id)
    if protocol == "legacy":
        await case.store.record_turn(turn_id="t_busy", session_id="s_reference", user_text="already running")
        error, message = ValueError, "submission_session_active"
    else:
        now = datetime.now(tz=UTC)
        with sqlite3.connect(fixture.db_path) as db:
            db.execute(
                """INSERT INTO terminal_protocol_writer_lease(
                  lease_name,owner_instance_id,epoch,admission_state,admission_epoch,
                  rollout_config_generation,lease_expires_at,heartbeat_at
                ) VALUES ('v1_writer','gateway_1',7,'open',3,'rollout_1',?,?)""",
                ((now + timedelta(minutes=5)).isoformat(), now.isoformat()),
            )
        store = TerminalProtocolStore(str(fixture.db_path), _clock=lambda: now)
        request = TurnRequest(
            session_id="s_reference",
            workspace_id=fixture.workspace_id,
            user_id=fixture.member_id,
            member_uid=fixture.member_id,
            text="already running",
            turn_id="t_busy",
            team_id=fixture.team_id,
        )
        vector = await store.ensure_auth_scope_epochs(terminal_auth_resolve_context(request))
        admitted = await store.admit_or_steer_v1(
            TerminalAdmissionRequest(
                context=AdmissionContext(
                    team_id=fixture.team_id,
                    session_id=request.session_id,
                    request_id="req_busy",
                    surface="web",
                    cohort="all;config=rollout_1",
                    actor=fixture.member_id,
                    writer_instance_id="gateway_1",
                    writer_lease_epoch=7,
                    writer_admission_epoch=3,
                    rollout_config_generation="rollout_1",
                    auth_fence_vector_digest=vector.auth_fence_vector_digest,
                ),
                turn_id=request.turn_id,
                user_text=request.text,
                execution_intent={"text": request.text},
                presentation_locale="zh-CN",
                message_catalog_version=1,
                allow_steer=False,
            )
        )
        assert admitted.disposition is AdmissionDisposition.ADMITTED
        error, message = SessionProtocolManagedError, "Terminal Protocol"
    with pytest.raises(error, match=message):
        await case.store.finalize_started_chat_submission(**case.args)
    _assert_no_acceptance(case, existing_session=True)
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT active_turn_id FROM sessions WHERE id='s_reference'").fetchone() == ("t_busy",)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("start_case", ["system"], indirect=True)
async def test_start_rechecks_a_system_override_after_claim(start_case: SimpleNamespace, enabled: bool) -> None:
    case = start_case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "INSERT INTO team_builtin_subagent_override(team_id,name,enabled,row_version,created_at,updated_at) VALUES(?,'compute',?,1,'now','now')",
            (case.fixture.team_id, int(enabled)),
        )
    with pytest.raises(RuntimeError, match="agent_system_definition_changed" if enabled else "agent_not_runnable"):
        await case.store.finalize_started_chat_submission(**case.args)
    _assert_no_acceptance(case)


@pytest.mark.parametrize("field", ["submission_kind", "attachment_ids", "response", "candidate", "text", "parameters"])
async def test_unsupported_or_mismatched_input_is_never_accepted(start_case: SimpleNamespace, field: str) -> None:
    case = start_case
    args = dict(case.args)
    if field == "submission_kind":
        args[field] = "replay"
    elif field == "attachment_ids":
        args[field] = ["unvalidated"]
    elif field == "response":
        args[field] = {**args[field], "turn_id": "different"}
    elif field == "candidate":
        args["reference_start"] = replace(args["reference_start"], expected=replace(case.agent.expected, revision_ref="arv_stale"))
    elif field == "parameters":
        args["reference_start"].input_document.nodes[0].parameters["mode"] = "extra"
    else:
        args["user_text"] = "downgraded text"
    with pytest.raises(ValueError):
        await case.store.finalize_started_chat_submission(**args)
    _assert_no_acceptance(case)


async def test_primary_link_cannot_be_rewritten_or_dangle(start_case: SimpleNamespace) -> None:
    case = start_case
    await case.store.finalize_started_chat_submission(**case.args)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        execution_id = db.execute("SELECT primary_execution_id FROM turn_agent_binding WHERE turn_id='t_reference'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE turn_agent_binding SET primary_execution_id=NULL WHERE turn_id='t_reference'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("INSERT OR REPLACE INTO turn_agent_binding SELECT * FROM turn_agent_binding WHERE turn_id='t_reference'")
        db.execute("DELETE FROM turn_agent_execution WHERE id=?", (execution_id,))
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.commit()
        db.rollback()
        assert db.execute("SELECT id FROM turn_agent_execution WHERE id=?", (execution_id,)).fetchone() == (execution_id,)
        # The deferred FK permits the existing whole-Turn cascade to clean all
        # dependent facts, while an execution-only deletion cannot strand a root.
        db.execute("DELETE FROM turns WHERE id='t_reference'")
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM turn_agent_binding WHERE turn_id='t_reference'").fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_primary_link_rejects_an_execution_from_another_turn(start_case: SimpleNamespace) -> None:
    case = start_case
    await case.store.finalize_started_chat_submission(**case.args)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM turn_agent_binding WHERE turn_id='t_reference'").fetchone())
        # Free the unique slot so only the cross-Turn binding guard can reject.
        db.execute("DELETE FROM turn_agent_binding WHERE turn_id='t_reference'")
        row["turn_id"] = case.fixture.turn_id
        columns = ",".join(row)
        with pytest.raises(sqlite3.IntegrityError, match="reference primary execution mismatch"):
            db.execute(f"INSERT INTO turn_agent_binding({columns}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
        db.rollback()


async def test_receipt_retry_still_requires_an_active_login_chain(start_case: SimpleNamespace) -> None:
    case = start_case
    first = await case.store.finalize_started_chat_submission(**case.args)
    assert await case.store.invalidate_chat_auth_session_generation(user_id=case.fixture.member_id, auth_session_generation_hash="0" * 64)
    with pytest.raises(ValueError, match="submission_auth_generation_inactive"):
        await case.store.finalize_started_chat_submission(**case.args)
    # The accepted fact remains; denial does not rewrite it as rejected.
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT outcome FROM chat_submission_receipts WHERE turn_id=?", (first.turn_id,)).fetchone() == ("started",)


@pytest.mark.parametrize("mode", ["observe", "cancel", "mutate_input", "expired_wait"])
async def test_commit_boundary_and_input_freezing(start_case: SimpleNamespace, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    import bowhead_core.session_store as module

    case = start_case
    clock = [datetime.now(tz=UTC)]
    monkeypatch.setattr(module, "_now", lambda: clock[0].isoformat())
    cancelled = anyio.get_cancelled_exc_class()
    connect = module.open_shared_ctx
    checked = []
    tables = (
        "turns",
        "chat_turn_user_sources",
        "chat_reference_plans",
        "turn_agent_binding",
        "turn_agent_execution",
        "chat_submission_receipts",
        "chat_turn_spawn_dispatch",
    )

    def observe(*counts: int) -> int:
        assert counts == (1,) * len(tables)
        with sqlite3.connect(case.fixture.db_path) as db:
            for table in tables:
                key = "id" if table == "turns" else "turn_id"
                assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE {key}='t_reference'").fetchone() == (0,)
        checked.append(True)
        return 1

    @asynccontextmanager
    async def connection(path):
        async with connect(path) as db:
            if mode == "observe":
                await db.create_function("observe_reference_start", len(tables), observe)
                counts = ",".join(f"(SELECT COUNT(*) FROM {table} WHERE {'id' if table == 'turns' else 'turn_id'}='t_reference')" for table in tables)
                await db.execute(
                    f"CREATE TEMP TRIGGER probe_reference_start BEFORE DELETE ON chat_submission_inflight BEGIN SELECT observe_reference_start({counts}); END"
                )
            elif mode == "cancel":
                execute = db.execute

                def interrupted(sql, *args, **kwargs):
                    if sql.startswith("DELETE FROM chat_submission_inflight "):
                        raise cancelled()
                    return execute(sql, *args, **kwargs)

                monkeypatch.setattr(db, "execute", interrupted)
            elif mode == "mutate_input":
                case.args["reference_start"].input_document.nodes[0].parameters["mode"] = "changed after validation"
            else:
                clock[0] += timedelta(minutes=5)
            yield db

    monkeypatch.setattr(module, "open_shared_ctx", connection)
    if mode == "expired_wait":
        with pytest.raises(ValueError, match="submission_claim_lost"):
            await case.store.finalize_started_chat_submission(**case.args)
        _assert_no_acceptance(case)
    elif mode == "cancel":
        with pytest.raises(cancelled):
            await case.store.finalize_started_chat_submission(**case.args)
        _assert_no_acceptance(case)
    else:
        first = await case.store.finalize_started_chat_submission(**case.args)
        assert first.outcome == "started"
        if mode == "observe":
            assert checked == [True]
        else:
            with sqlite3.connect(case.fixture.db_path) as db:
                stored = db.execute("SELECT input_document_json FROM chat_turn_user_sources WHERE turn_id='t_reference'").fetchone()[0]
                assert "changed after validation" not in stored
