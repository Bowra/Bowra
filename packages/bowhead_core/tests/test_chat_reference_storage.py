"""Real SQLite contracts for immutable reference plans, material and queue sources."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import (
    ChatReferenceStore,
    ContentDescriptor,
    QueuedReferenceInput,
    ReferenceCipher,
    ReferencePlan,
    ReferenceScope,
    reference_source_hash,
)
from bowhead_core.chat_references import InputDocument
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.session_store import SessionStore, _insert_chat_turn_user_source_in_tx, _project_legacy_steer_buffer
from bowhead_core.user_store import UserStore

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0063_chat_reference_storage")
ROOT_KEY = base64.urlsafe_b64encode(b"r" * 32).decode()
SUBMISSION_ID = "0da31a6b-2c6e-4fb3-8c80-f034b37d840f"


def _upgrade(path: Path, revision: str = "head", target: str = "core") -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    command.upgrade(config, revision)


#: Tables a later revision retires on purpose. Only these may exist in a historical schema
#: while missing from the current one: ``public_entry_provider_status`` (removed by 0002),
#: ``pbi_connection`` / ``pbi_dax_template`` (copied into the built-in tool registry and
#: dropped by 0122), and ``user_glossary`` (retired by 0187). Any other table missing
#: at head is a real schema regression.
RETIRED_TABLES = frozenset(
    {
        "public_entry_provider_status",
        "pbi_connection",
        "pbi_dax_template",
        "user_glossary",
        # 0190 removed Agent version webhooks.
        "agent_version_webhook_delivery",
        "agent_version_webhook_event",
        "agent_version_webhook_subscription",
    }
)


def _historical_fixture(source_path: Path, target_path: Path, revision: str) -> None:
    """Export fixture data into an actual historical schema, using its columns.

    Current production writers deliberately require the current schema. Migration
    tests must not add a production fallback just to manufacture old data. Disable
    destination triggers only during this fixture import (table order is arbitrary),
    then restore the exact historical triggers and check all foreign keys.
    """
    _upgrade(target_path, revision)
    with sqlite3.connect(target_path) as db:
        db.execute("ATTACH DATABASE ? AS fixture", (str(source_path),))
        triggers = db.execute("SELECT name,sql FROM main.sqlite_master WHERE type='trigger'").fetchall()
        for name, _ in triggers:
            db.execute(f'DROP TRIGGER "{name}"')
        tables = [
            r[0]
            for r in db.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name<>'alembic_version' AND name NOT LIKE 'sqlite_%'")
        ]
        current = {r[0] for r in db.execute("SELECT name FROM fixture.sqlite_master WHERE type='table'")}
        skipped = set(tables) - current
        assert skipped <= RETIRED_TABLES, f"historical tables missing at head but not retired on purpose: {sorted(skipped - RETIRED_TABLES)}"
        for name in tables:
            columns = ",".join('"' + r[1] + '"' for r in db.execute(f'PRAGMA main.table_info("{name}")'))
            db.execute(f'DELETE FROM main."{name}"')
            if name in skipped:
                # Current writers cannot have produced rows for a retired table; its historical copy stays empty.
                continue
            db.execute(f'INSERT INTO main."{name}"({columns}) SELECT {columns} FROM fixture."{name}"')
        for _, sql in triggers:
            db.execute(sql)
        assert db.execute("PRAGMA main.foreign_key_check").fetchall() == []


def _head_without_trigger(source_path: Path, target_path: Path, trigger: str) -> None:
    """Copy a head database and remove exactly one revision's guard trigger.

    Current Stores and writers operate only at head (startup runs ``upgrade head``),
    so a runtime "migration required" gate is proven on the head schema with only
    that revision's trigger missing, never by running current readers on an old schema.
    """
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)
    with sqlite3.connect(target_path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone() == (1,)
        db.execute(f'DROP TRIGGER "{trigger}"')


def _renew_dispatch_lease(path: Path, turn_id: str = "t_reference", seconds: int = 30) -> None:
    """Restart the Turn's 30-second dispatch lease after slow fixture migrations.

    Historical fixtures re-run dozens of revisions between the fixture's
    admission and the worker call; under parallel load that wall-clock time
    alone expired the lease (``reference_execution_fence_lost``). Production
    migrates before any Turn is accepted, so the lease models a live dispatch.
    """
    with sqlite3.connect(path) as db:
        renewed = db.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            ((datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(), turn_id),
        ).rowcount
    assert renewed == 1


def assert_rows_survive_upgrade(before: dict[str, list], after: dict[str, list]) -> None:
    """Every historical row survives ``upgrade head``; only empty ``RETIRED_TABLES`` may disappear.

    Later revisions legitimately contract superseded tables (``0122`` drops the old
    Power BI tables after copying them into the built-in tool registry). A table
    missing after the upgrade is acceptable only if the fixture held no rows in it,
    so this never hides a data loss. ``ADD COLUMN`` appends, so each historical row
    must survive as the exact prefix of its upgraded row (0123 appends
    ``turn_agent_execution.resolved_definition_json``); a rebuild that reorders or
    rewrites a historical column still fails.
    """
    retired = set(before) - set(after)
    assert retired <= RETIRED_TABLES, f"tables dropped by the upgrade but not retired on purpose: {sorted(retired - RETIRED_TABLES)}"
    assert {name: before[name] for name in retired} == {name: [] for name in retired}
    kept = {name: rows for name, rows in before.items() if name in after}
    width = {name: len(rows[0]) for name, rows in kept.items() if rows}
    assert {name: [row[: width.get(name, 0)] for row in after[name]] for name in kept} == kept


@pytest_asyncio.fixture
async def case(tmp_path: Path):
    path = tmp_path / "core.db"
    _upgrade(path)
    users = UserStore(path)
    await users.initialize()
    team = await users.create_team(name="References", max_seats=3)
    user = await users.create_user(team_id=team.id, email="references@example.com", display_name="Reader", tier="ultra", email_verified=True)
    await users.set_team_creator(team.id, user.id)
    workspace_id = await users.create_local_workspace(team_id=team.id, name="Reference Space", creator_user_id=user.id)
    scope = ReferenceScope(team_id=team.id, user_id=user.id, workspace_id=workspace_id, session_id="s_ref", policy_epoch=0)
    now = datetime.now(UTC).isoformat()
    expires = (datetime.now(UTC) + timedelta(days=90)).isoformat()
    resource = {"kind": "skill", "source_kind": "team", "id": "sk_dupont", "version": "sha256:" + "a" * 64}
    node_id = "00000000-0000-4000-8000-000000000001"
    doc = {
        "schema_version": 1,
        "nodes": [
            {
                "type": "reference",
                "node_id": node_id,
                "resource": resource,
                "label": "杜邦分析",
                "parameters": {"period": "2026Q3", "optional": None},
            },
            {"type": "text", "text": " 分析盈利能力"},
        ],
    }
    document = InputDocument.model_validate(doc)
    source = ChatTurnUserSource(
        turn_id="t_ref",
        session_id=scope.session_id,
        user_id=user.id,
        workspace_id=workspace_id,
        user_text=document.display_text(),
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        input_document=document,
    )
    plan = ReferencePlan.model_validate(
        {
            "schema_version": 1,
            "source_turn_id": "t_ref",
            "logical_source_turn_id": "t_ref",
            "snapshot_owner_turn_id": "t_ref",
            "source_hash": reference_source_hash(source),
            "admissions": [{"node_id": node_id, "resource": resource, "operation": "read", "policy_digest": "sha256:" + "b" * 64}],
        }
    )
    content = b"method-unique-secret\nROE = margin * turnover * leverage"
    descriptor = ContentDescriptor.model_validate(
        {
            "schema_version": 1,
            "content_key": "skill:dupont:body",
            "node_id": node_id,
            "resource": resource,
            "content_kind": "skill_read",
            "materialization_hash": "sha256:" + "c" * 64,
            "content_hash": "sha256:" + hashlib.sha256(content).hexdigest(),
            "byte_count": len(content),
        }
    )
    queued = QueuedReferenceInput.model_validate({"schema_version": 1, "input_document": doc, "attachment_ids": [], "attachment_refs": []})
    cipher = ReferenceCipher(ROOT_KEY)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO sessions(id,workspace_id,user_id,started_at,last_active_at,active_turn_id) VALUES(?,?,?,?,?,'t_ref')",
            (scope.session_id, workspace_id, user.id, now, now),
        )
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_ref',?,1,?,?)", (scope.session_id, source.user_text, now))
        db.execute(
            """INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,owner_id,lease_expires_at,created_at,updated_at)
            VALUES('t_ref',?,'start','spawned','worker_1',?,?,?)""",
            (scope.session_id, expires, now, now),
        )
    return SimpleNamespace(
        path=path,
        scope=scope,
        source=source,
        plan=plan,
        descriptor=descriptor,
        content=content,
        queued=queued,
        cipher=cipher,
        store=ChatReferenceStore(cipher),
        expires=expires,
        now=now,
        users=users,
    )


async def _plan(db, case):
    await _insert_chat_turn_user_source_in_tx(db, source=case.source, created_at=case.now)
    return await case.store.put_plan_in_connection(db, scope=case.scope, turn_id="t_ref", plan=case.plan, expires_at=case.expires)


async def test_queued_input_round_trips_server_receiver_without_mutating_user_source(case):
    raw = case.queued.model_dump(mode="python", warnings="error")
    raw["automatic_receiver"] = {
        "kind": "agent",
        "source_kind": "system",
        "id": "system:business_analyst",
        "version": "builtin-v1",
    }
    queued = QueuedReferenceInput.model_validate(raw)
    restored = QueuedReferenceInput.model_validate_json(
        queued.model_dump_json(exclude_none=True),
    )
    assert restored == queued
    assert restored.input_document == case.queued.input_document
    assert restored.automatic_receiver is not None
    assert restored.automatic_receiver.id == "system:business_analyst"
    with pytest.raises(ValueError, match="reference_lead_contract_unsupported"):
        QueuedReferenceInput.model_validate({**raw, "orchestration_contract": "lead-delegation.v1"})

    explicit = case.queued.model_dump(mode="python", warnings="error")
    explicit["orchestration_contract"] = "lead-delegation.v1"
    explicit["input_document"]["nodes"].insert(
        0,
        {
            "type": "reference",
            "node_id": "00000000-0000-4000-8000-000000000002",
            "resource": raw["automatic_receiver"],
            "label": "Selected analyst",
            "parameters": {},
        },
    )
    explicit_queue = QueuedReferenceInput.model_validate(explicit)
    assert QueuedReferenceInput.model_validate_json(explicit_queue.model_dump_json(exclude_none=True)) == explicit_queue

    capability = case.queued.model_dump(mode="python", warnings="error")
    capability["input_document"]["nodes"][0]["resource"] = {
        "kind": "capability",
        "id": "cap_visualize",
        "version": 1,
    }
    capability["automatic_receiver"] = raw["automatic_receiver"]
    assert QueuedReferenceInput.model_validate(capability).automatic_receiver is not None

    unsupported = case.queued.model_dump(mode="python", warnings="error")
    unsupported["input_document"]["nodes"][0]["resource"] = {
        "kind": "mcp_source",
        "source_id": "mcp_finance",
        "local_contract_revision": "sha256:" + "c" * 64,
        "connection_binding_id": "mcp_binding",
        "connection_subject_fingerprint": "sha256:" + "d" * 64,
    }
    unsupported["automatic_receiver"] = raw["automatic_receiver"]
    with pytest.raises(ValueError, match="reference_automatic_receiver_invalid"):
        QueuedReferenceInput.model_validate(unsupported)


async def _content(db, case, **changes):
    return await case.store.put_content_in_connection(
        db,
        **{
            "scope": case.scope,
            "turn_id": "t_ref",
            "descriptor": case.descriptor,
            "content": case.content,
            "generation": 1,
            "owner_id": "worker_1",
            "expires_at": case.expires,
            **changes,
        },
    )


async def _queue(db, case, **changes):
    return await case.store.put_queued_input_in_connection(
        db,
        **{
            "scope": case.scope,
            "submission_id": SUBMISSION_ID,
            "accepted_parent_turn_id": "t_ref",
            "accepted_generation": 1,
            "payload": case.queued,
            "expires_at": case.expires,
            **changes,
        },
    )


async def _receipt(db, case):
    await db.execute(
        """INSERT INTO chat_submission_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            case.scope.team_id,
            case.scope.user_id,
            SUBMISSION_ID,
            case.scope.workspace_id,
            case.scope.session_id,
            case.scope.session_id,
            "t_ref",
            "steer",
            "chat-references.v1",
            "references",
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "buffered",
            '{"status":"buffered"}',
            case.now,
            case.expires,
        ),
    )


@pytest.mark.asyncio
async def test_plan_and_source_share_callers_transaction_and_exact_identity(case):
    async with open_shared_ctx(str(case.path)) as db:
        with pytest.raises(ValueError, match="transaction_required"):
            await case.store.put_plan_in_connection(db, scope=case.scope, turn_id="t_ref", plan=case.plan, expires_at=case.expires)
        await db.execute("BEGIN IMMEDIATE")
        assert await _plan(db, case)
        assert not await _plan(db, case)
        assert await case.store.get_plan_in_connection(db, scope=case.scope, turn_id="t_ref") == case.plan
        await db.rollback()
        for table in ("chat_reference_plans", "chat_turn_user_sources"):
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0
        await db.execute("BEGIN IMMEDIATE")
        assert await _plan(db, case)
        await db.commit()
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN")
        assert await case.store.get_plan_in_connection(db, scope=case.scope, turn_id="t_ref") == case.plan


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["hash", "resource", "order", "lineage"])
async def test_plan_cannot_rebind_canonical_input(case, change):
    raw = case.plan.model_dump(exclude_none=True)
    if change == "hash":
        raw["source_hash"] = "sha256:" + "f" * 64
    elif change == "resource":
        raw["admissions"][0]["resource"]["version"] = "sha256:" + "f" * 64
    elif change == "order":
        raw["admissions"] = []
    else:
        raw["logical_source_turn_id"] = "t_other"
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=case.source, created_at=case.now)
        with pytest.raises(ValueError, match="reference_(source_changed|admission_source_mismatch|lineage_unavailable)"):
            await case.store.put_plan_in_connection(
                db, scope=case.scope, turn_id="t_ref", plan=ReferencePlan.model_validate(raw), expires_at=case.expires
            )


@pytest.mark.asyncio
async def test_content_is_sealed_and_deduplicated_without_plaintext_reader(case):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        assert await _content(db, case)
        assert not await _content(db, case)
        descriptor, ciphertext = await case.store.get_encrypted_content_in_connection(
            db, scope=case.scope, turn_id="t_ref", content_key=case.descriptor.content_key
        )
        assert descriptor == case.descriptor
        assert case.content.decode() not in ciphertext
        assert (
            case.cipher.open(scope=case.scope, purpose="content", identity=f"t_ref/{descriptor.content_key}", ciphertext=ciphertext) == case.content
        )
        with pytest.raises(ValueError, match="reference_content_changed"):
            await _content(db, case, content=b"changed")
        changed = case.descriptor.model_dump(exclude_none=True)
        changed["materialization_hash"] = "sha256:" + "f" * 64
        with pytest.raises(ValueError, match="reference_content_conflict"):
            await _content(db, case, descriptor=ContentDescriptor.model_validate(changed))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "generation", "lease", "epoch", "membership", "session_deleted"])
async def test_prepare_return_rechecks_current_fences(case, change):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        args = {}
        if change == "owner":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET owner_id='successor' WHERE turn_id='t_ref'")
        elif change == "generation":
            await db.execute("UPDATE turns SET execution_generation=2 WHERE id='t_ref'")
        elif change == "lease":
            await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE turn_id='t_ref'")
        elif change == "epoch":
            await db.execute("INSERT INTO access_policy_state(team_id,epoch,updated_at) VALUES(?,1,?)", (case.scope.team_id, case.now))
        elif change == "membership":
            await db.execute(
                "UPDATE workspace_members SET status='removed' WHERE workspace_id=? AND user_id=?", (case.scope.workspace_id, case.scope.user_id)
            )
        else:
            await db.execute("UPDATE sessions SET deleted_at=? WHERE id=?", (case.now, case.scope.session_id))
        with pytest.raises(ValueError, match="reference_(execution_fence_lost|policy_changed|scope_unavailable)"):
            await _content(db, case, **args)
        async with db.execute("SELECT COUNT(*) FROM chat_reference_content_snapshots") as cursor:
            assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_queue_requires_accepted_receipt_and_survives_receipt_expiry(case):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="receipt_required"):
            await _queue(db, case)
        await _receipt(db, case)
        assert await _queue(db, case)
        assert not await _queue(db, case)
        await db.execute("DELETE FROM chat_submission_receipts")
        await db.commit()
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        payload, state, version = await case.store.get_queued_input_in_connection(db, scope=case.scope, submission_id=SUBMISSION_ID)
        assert payload == case.queued and (state, version) == ("queued", 1)
        assert await case.store.settle_queued_input_in_connection(
            db, scope=case.scope, submission_id=SUBMISSION_ID, expected_version=1, cancel_reason="user_cancelled"
        )
        assert not await case.store.settle_queued_input_in_connection(
            db, scope=case.scope, submission_id=SUBMISSION_ID, expected_version=1, cancel_reason="source_deleted"
        )
        assert not await _queue(db, case)  # An identity replay does not resurrect it.
        _, state, version = await case.store.get_queued_input_in_connection(db, scope=case.scope, submission_id=SUBMISSION_ID)
        assert (state, version) == ("cancelled", 2)


@pytest.mark.asyncio
async def test_reference_steer_commits_receipt_pointer_and_encrypted_source_atomically(case):
    auth_generation = "4" * 64
    with sqlite3.connect(case.path) as db:
        db.execute(
            "INSERT INTO chat_auth_session_generations(user_id,auth_session_generation_hash,state,created_at) VALUES(?,?,'active',?)",
            (case.scope.user_id, auth_generation, case.now),
        )
    sessions = SessionStore(case.path)
    common = {
        "team_id": case.scope.team_id,
        "user_id": case.scope.user_id,
        "client_submission_id": SUBMISSION_ID,
        "workspace_id": case.scope.workspace_id,
        "requested_session_id": case.scope.session_id,
        "submission_kind": "steer",
        "submission_contract_version": "chat-references.v1",
        "model_control_batch": "references",
        "request_fingerprint": "5" * 64,
        "accepted_request_fingerprints": ("5" * 64,),
        "fingerprint_key_id": "6" * 64,
        "auth_session_generation_hash": auth_generation,
    }
    claim = await sessions.claim_chat_submission(**common)
    assert claim.kind == "claimed"
    receipt = await sessions.finalize_steer_chat_submission(
        team_id=case.scope.team_id,
        user_id=case.scope.user_id,
        client_submission_id=SUBMISSION_ID,
        workspace_id=case.scope.workspace_id,
        requested_session_id=case.scope.session_id,
        expected_parent_turn_id="t_ref",
        expected_execution_generation=1,
        text=case.queued.input_document.display_text(),
        attachment_ids=[],
        attachment_refs=[],
        submission_contract_version="chat-references.v1",
        model_control_batch="references",
        request_fingerprint="5" * 64,
        lease_owner=claim.lease_owner,
        lease_version=claim.lease_version,
        reference_store=case.store,
        reference_scope=case.scope,
        reference_input=case.queued,
    )
    assert receipt.outcome == "buffered"
    replay = await sessions.claim_chat_submission(**common)
    assert replay.kind == "receipt" and replay.receipt == receipt
    with sqlite3.connect(case.path) as db:
        buffer_json = db.execute("SELECT steer_buffer_json FROM sessions WHERE id=?", (case.scope.session_id,)).fetchone()[0]
        buffer = json.loads(buffer_json)
        assert buffer == [
            {
                "text": case.queued.input_document.display_text(),
                "attachment_ids": [],
                "attachment_refs": [],
                "submission_id": SUBMISSION_ID,
                "ts": buffer[0]["ts"],
                "reference_submission_id": SUBMISSION_ID,
            }
        ]
        queued = db.execute(
            "SELECT state,row_version,payload_hash,ciphertext FROM chat_reference_queued_inputs "
            "WHERE team_id=? AND user_id=? AND client_submission_id=?",
            (case.scope.team_id, case.scope.user_id, SUBMISSION_ID),
        ).fetchone()
        assert queued[:2] == ("queued", 1)
        assert queued[2].startswith("sha256:") and case.queued.input_document.display_text() not in queued[3]
    with pytest.raises(ValueError, match="legacy continuation buffer is malformed"):
        _project_legacy_steer_buffer(buffer_json)


@pytest.mark.asyncio
async def test_reference_steer_rolls_back_receipt_and_buffer_when_source_write_fails(case):
    auth_generation = "7" * 64
    with sqlite3.connect(case.path) as db:
        db.execute(
            "INSERT INTO chat_auth_session_generations(user_id,auth_session_generation_hash,state,created_at) VALUES(?,?,'active',?)",
            (case.scope.user_id, auth_generation, case.now),
        )
        db.execute(
            "CREATE TRIGGER fail_reference_queue BEFORE INSERT ON chat_reference_queued_inputs BEGIN SELECT RAISE(ABORT,'injected'); END",
        )
    sessions = SessionStore(case.path)
    common = {
        "team_id": case.scope.team_id,
        "user_id": case.scope.user_id,
        "client_submission_id": SUBMISSION_ID,
        "workspace_id": case.scope.workspace_id,
        "requested_session_id": case.scope.session_id,
        "submission_kind": "steer",
        "submission_contract_version": "chat-references.v1",
        "model_control_batch": "references",
        "request_fingerprint": "8" * 64,
        "accepted_request_fingerprints": ("8" * 64,),
        "fingerprint_key_id": "9" * 64,
        "auth_session_generation_hash": auth_generation,
    }
    claim = await sessions.claim_chat_submission(**common)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        await sessions.finalize_steer_chat_submission(
            team_id=case.scope.team_id,
            user_id=case.scope.user_id,
            client_submission_id=SUBMISSION_ID,
            workspace_id=case.scope.workspace_id,
            requested_session_id=case.scope.session_id,
            expected_parent_turn_id="t_ref",
            expected_execution_generation=1,
            text=case.queued.input_document.display_text(),
            attachment_ids=[],
            attachment_refs=[],
            submission_contract_version="chat-references.v1",
            model_control_batch="references",
            request_fingerprint="8" * 64,
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
            reference_store=case.store,
            reference_scope=case.scope,
            reference_input=case.queued,
        )
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_submission_receipts").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_queued_inputs").fetchone() == (0,)
        assert db.execute("SELECT steer_buffer_json FROM sessions WHERE id=?", (case.scope.session_id,)).fetchone() == ("[]",)
        assert db.execute("SELECT COUNT(*) FROM chat_submission_inflight").fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("scope", "reference_submission_scope_invalid"),
        ("source", "reference_submission_source_changed"),
    ],
)
async def test_reference_steer_rejects_server_scope_or_source_drift_without_mutation(case, change, error):
    auth_generation = "a" * 64
    with sqlite3.connect(case.path) as db:
        db.execute(
            "INSERT INTO chat_auth_session_generations(user_id,auth_session_generation_hash,state,created_at) VALUES(?,?,'active',?)",
            (case.scope.user_id, auth_generation, case.now),
        )
    sessions = SessionStore(case.path)
    common = {
        "team_id": case.scope.team_id,
        "user_id": case.scope.user_id,
        "client_submission_id": SUBMISSION_ID,
        "workspace_id": case.scope.workspace_id,
        "requested_session_id": case.scope.session_id,
        "submission_kind": "steer",
        "submission_contract_version": "chat-references.v1",
        "model_control_batch": "references",
        "request_fingerprint": "b" * 64,
        "accepted_request_fingerprints": ("b" * 64,),
        "fingerprint_key_id": "c" * 64,
        "auth_session_generation_hash": auth_generation,
    }
    claim = await sessions.claim_chat_submission(**common)
    scope = case.scope.model_copy(update={"workspace_id": "ws_other"}) if change == "scope" else case.scope
    text = case.queued.input_document.display_text() + (" changed" if change == "source" else "")
    with pytest.raises(ValueError, match=error):
        await sessions.finalize_steer_chat_submission(
            team_id=case.scope.team_id,
            user_id=case.scope.user_id,
            client_submission_id=SUBMISSION_ID,
            workspace_id=case.scope.workspace_id,
            requested_session_id=case.scope.session_id,
            expected_parent_turn_id="t_ref",
            expected_execution_generation=1,
            text=text,
            attachment_ids=[],
            attachment_refs=[],
            submission_contract_version="chat-references.v1",
            model_control_batch="references",
            request_fingerprint="b" * 64,
            lease_owner=claim.lease_owner,
            lease_version=claim.lease_version,
            reference_store=case.store,
            reference_scope=scope,
            reference_input=case.queued,
        )
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_submission_receipts").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_queued_inputs").fetchone() == (0,)
        assert db.execute("SELECT steer_buffer_json FROM sessions WHERE id=?", (case.scope.session_id,)).fetchone() == ("[]",)
        assert db.execute("SELECT COUNT(*) FROM chat_submission_inflight").fetchone() == (1,)


@pytest.mark.asyncio
async def test_receipt_queue_source_and_plan_rollback_together(case):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _receipt(db, case)
        await _queue(db, case)
        await _plan(db, case)
        await _content(db, case)
        # A real SQL failure in the last operation must rollback all prior pieces.
        try:
            await db.execute("INSERT INTO chat_reference_plans SELECT * FROM chat_reference_plans")
        except sqlite3.IntegrityError:
            await db.rollback()
        else:
            pytest.fail("duplicate plan unexpectedly overwrote its source")
        for table in (
            "chat_submission_receipts",
            "chat_reference_queued_inputs",
            "chat_turn_user_sources",
            "chat_reference_plans",
            "chat_reference_content_snapshots",
        ):
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "parameters", "attachment"])
async def test_materialization_requires_the_complete_child_source_and_plan(case, change):
    child = replace(case.source, turn_id="t_child")
    if change == "parameters":
        raw_document = child.input_document.model_dump(exclude_none=True)
        raw_document["nodes"][0]["parameters"]["period"] = "2026Q2"
        child = replace(child, input_document=InputDocument.model_validate(raw_document))
    elif change == "attachment":
        child = replace(child, attachment_ids=("file_other",))
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _receipt(db, case)
        await _queue(db, case)
        await db.execute(
            "INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_child',?,2,?,?)", (case.scope.session_id, child.user_text, case.now)
        )
        await _insert_chat_turn_user_source_in_tx(db, source=child, created_at=case.now)
        raw = case.plan.model_dump(exclude_none=True)
        raw.update(
            source_turn_id="t_child", logical_source_turn_id="t_child", snapshot_owner_turn_id="t_child", source_hash=reference_source_hash(child)
        )
        await case.store.put_plan_in_connection(
            db, scope=case.scope, turn_id="t_child", plan=ReferencePlan.model_validate(raw), expires_at=case.expires
        )
        if change:
            with pytest.raises(ValueError, match="reference_materialized_source_changed"):
                await case.store.settle_queued_input_in_connection(
                    db, scope=case.scope, submission_id=SUBMISSION_ID, expected_version=1, materialized_turn_id="t_child"
                )
            async with db.execute("SELECT state,row_version FROM chat_reference_queued_inputs") as cursor:
                assert tuple(await cursor.fetchone()) == ("queued", 1)
            return
        assert await case.store.settle_queued_input_in_connection(
            db, scope=case.scope, submission_id=SUBMISSION_ID, expected_version=1, materialized_turn_id="t_child"
        )
        assert not await case.store.settle_queued_input_in_connection(
            db, scope=case.scope, submission_id=SUBMISSION_ID, expected_version=1, cancel_reason="user_cancelled"
        )
        async with db.execute("SELECT state,materialized_turn_id FROM chat_reference_queued_inputs") as cursor:
            assert tuple(await cursor.fetchone()) == ("materialized", "t_child")


@pytest.mark.asyncio
async def test_purge_is_irreversible_and_prevents_late_prepare(case):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _receipt(db, case)
        await _queue(db, case)
        await _plan(db, case)
        await _content(db, case)
        await case.store.purge_in_connection(db, scope=case.scope)
        for table in ("chat_reference_content_snapshots", "chat_reference_queued_inputs"):
            async with db.execute(f"SELECT ciphertext,retention_state FROM {table}") as cursor:
                assert tuple(await cursor.fetchone()) == (None, "purged")
        with pytest.raises(ValueError, match="reference_source_unavailable"):
            await _content(db, case, descriptor=case.descriptor.model_copy(update={"content_key": "late_prepare"}))
        with pytest.raises(ValueError, match="reference_source_unavailable"):
            await case.store.get_queued_input_in_connection(db, scope=case.scope, submission_id=SUBMISSION_ID)
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("UPDATE chat_reference_plans SET retention_state='available'")
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["team_id", "user_id", "workspace_id", "session_id"])
async def test_cross_scope_read_and_cipher_swap_are_rejected(case, field):
    wrong = case.scope.model_copy(update={field: "other"})
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        await _content(db, case)
        descriptor, ciphertext = await case.store.get_encrypted_content_in_connection(
            db, scope=case.scope, turn_id="t_ref", content_key=case.descriptor.content_key
        )
        with pytest.raises(ValueError, match="reference_scope_unavailable"):
            await case.store.get_plan_in_connection(db, scope=wrong, turn_id="t_ref")
        with pytest.raises(ValueError, match="reference_ciphertext_invalid"):
            case.cipher.open(scope=wrong, purpose="content", identity=f"t_ref/{descriptor.content_key}", ciphertext=ciphertext)
        with pytest.raises(ValueError, match="reference_ciphertext_invalid"):
            case.cipher.open(scope=case.scope, purpose="queued_input", identity=f"t_ref/{descriptor.content_key}", ciphertext=ciphertext)


@pytest.mark.asyncio
async def test_expired_material_cannot_be_read_or_extended(case, monkeypatch):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        await _content(db, case)
        future = datetime.now(UTC) + timedelta(days=91)
        monkeypatch.setattr("bowhead_core.chat_reference_storage._now", lambda: future)
        with pytest.raises(ValueError, match="reference_source_unavailable"):
            await case.store.get_encrypted_content_in_connection(db, scope=case.scope, turn_id="t_ref", content_key=case.descriptor.content_key)
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("UPDATE chat_reference_content_snapshots SET expires_at='2099-01-01T00:00:00+00:00'")


@pytest.mark.asyncio
async def test_concurrent_plan_writers_deduplicate_one_identity(case):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=case.source, created_at=case.now)
        await db.commit()

    async def write():
        async with open_shared_ctx(str(case.path)) as db:
            await db.execute("BEGIN IMMEDIATE")
            result = await case.store.put_plan_in_connection(db, scope=case.scope, turn_id="t_ref", plan=case.plan, expires_at=case.expires)
            await db.commit()
            return result

    assert sorted(await asyncio.gather(write(), write())) == [False, True]


def test_migration_from_previous_head_preserves_legacy_rows_and_indexes(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT sql FROM sqlite_master WHERE name='chat_submission_receipts'").fetchone()
    _upgrade(path, MIGRATION.revision)
    _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='chat_submission_receipts'").fetchone() == before
        assert len(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'chat_reference_%'").fetchall()) == 3
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        for index in ("idx_chat_reference_plans_logical_source", "idx_chat_reference_queue_session", "idx_chat_reference_content_expiry"):
            assert db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)).fetchone()


@pytest.mark.parametrize("target", [target for target in TARGET_FILES if target != "core"])
def test_migration_is_noop_for_other_targets(tmp_path, target):
    path = tmp_path / f"{target}.db"
    _upgrade(path, target=target)
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'chat_reference_%'").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["actual_session_id", "turn_id", "expired", "generation"])
async def test_queue_checks_current_parent_and_receipt_identity(case, change):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _receipt(db, case)
        if change == "generation":
            await db.execute("UPDATE turns SET execution_generation=2 WHERE id='t_ref'")
        elif change == "expired":
            await db.execute("UPDATE chat_submission_receipts SET expires_at='2000-01-01T00:00:00+00:00'")
        elif change == "actual_session_id":
            await db.execute("UPDATE chat_submission_receipts SET actual_session_id='s_other'")
        else:
            await db.execute("UPDATE chat_submission_receipts SET turn_id='t_other'")
        with pytest.raises(ValueError, match="reference_(receipt_required|parent_changed)"):
            await _queue(db, case)
        async with db.execute("SELECT COUNT(*) FROM chat_reference_queued_inputs") as cursor:
            assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_queue_cas_and_identity_reject_stale_changes(case):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _receipt(db, case)
        await _queue(db, case)
        await _plan(db, case)
        changed = case.queued.model_dump(exclude_none=True)
        changed["input_document"]["nodes"][0]["parameters"]["period"] = "2026Q2"
        with pytest.raises(ValueError, match="reference_queued_input_conflict"):
            await _queue(db, case, payload=QueuedReferenceInput.model_validate(changed))
        with pytest.raises(ValueError, match="reference_materialized_source_changed"):
            await case.store.settle_queued_input_in_connection(
                db, scope=case.scope, submission_id=SUBMISSION_ID, expected_version=1, materialized_turn_id="t_ref"
            )
        assert not await case.store.settle_queued_input_in_connection(
            db, scope=case.scope, submission_id=SUBMISSION_ID, expected_version=2, cancel_reason="user_cancelled"
        )
        async with db.execute("SELECT state,row_version FROM chat_reference_queued_inputs") as cursor:
            assert tuple(await cursor.fetchone()) == ("queued", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("table,field", [("chat_reference_plans", "plan_json"), ("chat_reference_content_snapshots", "descriptor_json")])
async def test_sql_rejects_unversioned_records_and_metadata_rebinding(case, table, field):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        await _content(db, case)
        async with db.execute(f"SELECT * FROM {table}") as cursor:
            columns = [item[0] for item in cursor.description]
            original = list(await cursor.fetchone())
        # Reinsert through the real table CHECK, rather than the update trigger.
        if table == "chat_reference_plans":
            # Isolate the envelope CHECK; ordinary plan deletion is now forbidden.
            await db.execute("DROP TRIGGER trg_reference_plan_parent_delete")
            await db.execute("DELETE FROM chat_reference_content_snapshots")
        await db.execute(f"DELETE FROM {table}")
        index = columns.index(field)
        record = json.loads(original[index])
        for change in ("missing", "boolean", "rebound"):
            raw = dict(record)
            if change == "missing":
                del raw["schema_version"]
            elif change == "boolean":
                raw["schema_version"] = True
            else:
                raw["source_hash" if table == "chat_reference_plans" else "content_hash"] = "sha256:" + "f" * 64
            values = original.copy()
            values[index] = json.dumps(raw)
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                await db.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values)


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["chat_reference_plans", "chat_reference_content_snapshots", "chat_reference_queued_inputs"])
async def test_sql_forbids_replacement_or_ciphertext_changes(case, table):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _receipt(db, case)
        await _queue(db, case)
        await _plan(db, case)
        await _content(db, case)
        with pytest.raises(sqlite3.IntegrityError, match="already exists"):
            await db.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
        if table != "chat_reference_plans":
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                await db.execute(f"UPDATE {table} SET ciphertext='replacement'")


@pytest.mark.asyncio
async def test_cipher_binds_content_identity_key_and_bounded_byte_payload(case):
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        with pytest.raises(ValueError, match="reference_content_requires_bytes"):
            await _content(db, case, content=10)
        await _content(db, case)
        descriptor, ciphertext = await case.store.get_encrypted_content_in_connection(
            db, scope=case.scope, turn_id="t_ref", content_key=case.descriptor.content_key
        )
        with pytest.raises(ValueError, match="reference_ciphertext_invalid"):
            case.cipher.open(scope=case.scope, purpose="content", identity="t_other/body", ciphertext=ciphertext)
        other_cipher = ReferenceCipher(base64.urlsafe_b64encode(b"x" * 32).decode())
        with pytest.raises(ValueError, match="reference_ciphertext_invalid"):
            other_cipher.open(scope=case.scope, purpose="content", identity=f"t_ref/{descriptor.content_key}", ciphertext=ciphertext)
        for model in (case.plan, case.descriptor, case.queued):
            raw = model.model_dump(exclude_none=True)
            del raw["schema_version"]
            with pytest.raises(ValueError, match="reference_schema_version_required"):
                type(model).model_validate(raw)
            raw["schema_version"] = True
            with pytest.raises(ValueError, match="reference_schema_version_required"):
                type(model).model_validate(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["source", "purged_plan", "legacy"])
async def test_legacy_dispatch_cannot_claim_structured_or_purged_source(case: SimpleNamespace, marker: str) -> None:
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        if marker == "source":
            await _insert_chat_turn_user_source_in_tx(db, source=case.source, created_at=case.now)
        if marker == "purged_plan":
            await _plan(db, case)
            await case.store.purge_in_connection(db, scope=case.scope)
            await db.execute("DELETE FROM chat_turn_user_sources")
        await db.execute("UPDATE chat_turn_spawn_dispatch SET state='claimed',owner_id=NULL")
        await db.commit()
    store = SessionStore(str(case.path))
    assert await store.claim_chat_spawn_dispatch_for_worker(turn_id="t_ref", owner_id="new_worker") == (
        "claimed" if marker == "legacy" else "rejected"
    )
    if marker != "legacy":
        with pytest.raises(ValueError, match="reference_execution_adapter_unavailable"):
            await store.require_legacy_chat_execution(turn_id="t_ref")
    with sqlite3.connect(case.path) as db:
        state, owner = db.execute("SELECT state,owner_id FROM chat_turn_spawn_dispatch").fetchone()
        assert (state, owner) == (("spawned", "new_worker") if marker == "legacy" else ("claimed", None))


@pytest.mark.asyncio
async def test_reference_resume_does_not_consume_a_paused_generation(case: SimpleNamespace) -> None:
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        await db.execute("UPDATE turns SET status='clarification_pending',ended_at=?", (case.now,))
        await db.execute("UPDATE chat_turn_spawn_dispatch SET state='terminal',owner_id=NULL")
        await db.commit()
    store = SessionStore(str(case.path))
    assert not await store.is_turn_resume_ready("t_ref")
    assert await store.advance_turn_execution_generation(turn_id="t_ref") is None
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT status,execution_generation FROM turns").fetchone() == ("clarification_pending", 1)
        assert db.execute("SELECT state,owner_id FROM chat_turn_spawn_dispatch").fetchone() == ("terminal", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expired", "stale_untracked", "orphan", "settled_buffer", "settled_empty"])
async def test_legacy_maintenance_preserves_unsupported_reference_work(case: SimpleNamespace, state: str) -> None:
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        await db.execute("UPDATE turns SET started_at='2000-01-01T00:00:00+00:00'")
        await db.execute("UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00'")
        if state in {"orphan", "settled_buffer", "settled_empty"}:
            # Simulate historical/corrupt terminal rows; 0066 rejects new writes of this shape.
            await db.execute("DROP TRIGGER trg_reference_primary_completion")
            await db.execute("UPDATE turns SET status='completed',ended_at='2000-01-01T00:00:00+00:00'")
            await db.execute("UPDATE chat_turn_spawn_dispatch SET state='terminal',owner_id=NULL")
        if state in {"orphan", "stale_untracked"}:
            await db.execute("DELETE FROM chat_turn_spawn_dispatch")
        if state != "settled_empty":
            await db.execute("UPDATE sessions SET steer_buffer_json=?", (json.dumps([{"text": "keep this accepted message"}]),))
        await db.commit()
    with sqlite3.connect(case.path) as db:
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("sessions", "turns", "chat_turn_spawn_dispatch", "chat_turn_user_sources")
        }
    store = SessionStore(str(case.path))
    assert await store.cleanup_stale_running_turns(older_than_min=1) == 0
    assert await store.recover_expired_chat_spawn_dispatches() == []
    with sqlite3.connect(case.path) as db:
        for table, expected in before.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == expected
        assert db.execute("SELECT COUNT(*) FROM turn_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_legacy_materializer_preserves_reference_parent_and_buffer(case: SimpleNamespace) -> None:
    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        # Historical unsupported terminal input remains isolated from the legacy materializer.
        await db.execute("DROP TRIGGER trg_reference_primary_completion")
        await db.execute("UPDATE turns SET status='completed',ended_at=?", (case.now,))
        await db.execute("UPDATE sessions SET steer_buffer_json=?", (json.dumps([{"text": "pending input"}]),))
        await db.commit()
    store = SessionStore(str(case.path))
    with pytest.raises(ValueError, match="reference_execution_adapter_unavailable"):
        await store.materialize_steer_follow_up(session_id=case.scope.session_id, current_turn_id="t_ref", next_turn_id="t_child")
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT id FROM turns").fetchall() == [("t_ref",)]
        assert json.loads(db.execute("SELECT steer_buffer_json FROM sessions").fetchone()[0]) == [{"text": "pending input"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [True, False])
async def test_actual_turn_runner_stops_before_events_or_model_for_reference_source(
    case: SimpleNamespace, fake_subagent_runner: Any, structured: bool
) -> None:
    from bowhead_core.events import EventBusRegistry
    from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnLogStore
    from bowhead_core.turn_runner import TurnRunner
    from bowhead_core.types import TurnRequest
    from test_turn_runner_unit import _make_orch

    async with open_shared_ctx(str(case.path)) as db:
        await db.execute("BEGIN IMMEDIATE")
        if structured:
            await _plan(db, case)
        else:
            await _insert_chat_turn_user_source_in_tx(db, source=replace(case.source, input_document=None), created_at=case.now)
        await db.commit()
    sink = OrderedTurnLogSink(TurnLogStore(str(case.path)))
    registry = EventBusRegistry(default_log_sink=sink)
    bus = await registry.get_or_create("t_ref", exclusive_producer=True, persist_before_live=True)
    orch = _make_orch(session_store=SessionStore(str(case.path)), subagent_runner=fake_subagent_runner)
    orch.event_registry = registry
    request = TurnRequest(
        turn_id="t_ref",
        session_id=case.scope.session_id,
        user_id=case.scope.user_id,
        workspace_id=case.scope.workspace_id,
        text=case.source.user_text,
        member_uid="",
    )
    if structured:
        with pytest.raises(ValueError, match="reference_execution_adapter_unavailable"):
            await TurnRunner(orch).run(request)
        assert bus.snapshot() == []
        assert fake_subagent_runner.invocations == []
    else:
        await TurnRunner(orch).run(request)
        assert any(event.event_type == "turn_started" for event in bus.snapshot())
        assert fake_subagent_runner.invocations
    await registry.close("t_ref", expected=bus)


async def test_prepared_skill_text_roundtrips_through_real_plan_and_encrypted_snapshot(case, tmp_path):
    from bowhead_core.chat_references import SkillResourceRef
    from bowhead_core.skill_read_material import prepare_skill_read, restore_skill_read
    from bowhead_core.skill_upload import bundle_sha256
    from test_skill_read_material import _skill

    root = _skill(tmp_path / "selected_bundle", entry_file="SKILL.md")
    resource = SkillResourceRef(kind="skill", source_kind="team", id="sk_dupont", version="sha256:" + bundle_sha256(root))
    prepared = prepare_skill_read(root, resource=resource, expected_name="dupont")
    document = case.source.input_document.model_dump(mode="json")
    document["nodes"][0]["resource"] = resource.model_dump(mode="json")
    document["nodes"][0]["parameters"] = {"company": "Example"}
    case.source = replace(case.source, input_document=InputDocument.model_validate(document))
    plan = case.plan.model_dump(mode="json")
    plan["source_hash"] = reference_source_hash(case.source)
    plan["admissions"][0]["resource"] = resource.model_dump(mode="json")
    case.plan = ReferencePlan.model_validate(plan)
    descriptor = case.descriptor.model_dump(mode="json")
    descriptor.update(
        resource=resource.model_dump(mode="json"),
        content_hash=prepared.content_hash,
        materialization_hash=prepared.materialization_hash,
        byte_count=len(prepared.content),
    )
    case.descriptor = ContentDescriptor.model_validate(descriptor)
    case.content = prepared.content
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _plan(db, case)
        assert await _content(db, case)
        assert not await _content(db, case)
        await db.commit()
    (root / "SKILL.md").write_text("NEW_PACKAGE_BODY")
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN")
        saved = await case.store.get_encrypted_content_in_connection(db, scope=case.scope, turn_id="t_ref", content_key=case.descriptor.content_key)
        await db.rollback()
    stored_descriptor, ciphertext = saved
    assert stored_descriptor == case.descriptor
    recovered = case.cipher.open(scope=case.scope, purpose="content", identity="t_ref/" + case.descriptor.content_key, ciphertext=ciphertext)
    assert recovered == prepared.content
    assert restore_skill_read(recovered, descriptor=stored_descriptor) == prepared
    assert json.loads(recovered)["files"][0]["text"] == "MANIFEST_BODY\n"
    with sqlite3.connect(case.path) as db:
        assert "MANIFEST_BODY" not in "\n".join(db.iterdump())
