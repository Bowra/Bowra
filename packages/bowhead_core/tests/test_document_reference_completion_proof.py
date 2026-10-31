"""Exercise the narrow SQL proof independently of broader completion branches."""

import sqlite3
from copy import deepcopy
from importlib import import_module

import pytest

MIGRATION = import_module("bowhead_core.migrations.versions.0208_document_reference_completion")
pytestmark = pytest.mark.integration

# A relational proof fixture, not an authorized writer or a seeded product job.
# Full authenticated HTTP tests separately exercise the installed completion guard.
PROOF = {
    "current_turn": [{"id": "t_native", "session_id": "s_native", "execution_generation": 2}],
    "primary_execution": [{"id": "aex_primary"}],
    "primary_output": [{"created_generation": 1, "retention_state": "available"}],
    "document_jobs": [
        {
            "id": "djob_native",
            "origin_turn_id": "t_native",
            "session_id": "s_native",
            "status": "queued",
            "requested_by": "usr_native",
            "workspace_id": "ws_native",
            "team_id": "team_native",
            "user_source_sha256": "source",
        }
    ],
    "document_dispatches": [{"job_id": "djob_native", "capability_execution_id": "ce_native"}],
    "turn_capability_execution": [
        {
            "id": "ce_native",
            "agent_execution_id": "aex_primary",
            "turn_id": "t_native",
            "team_id": "team_native",
            "status": "completed",
            "output_contract": "document.admission.v1",
        }
    ],
    "document_turn_dispatches": [
        {
            "job_id": "djob_native",
            "creation_generation": 1,
            "owner_id": "worker_create",
            "source_hash": "source",
            "runtime_snapshot_digest": "runtime",
        }
    ],
    "document_planning_attempts": [
        {
            "job_id": "djob_native",
            "attempt_no": 1,
            "creation_generation": 1,
            "owner_id": "worker_create",
            "checkpoint_digest": "checkpoint",
            "card_id": None,
        },
        {
            "job_id": "djob_native",
            "attempt_no": 2,
            "creation_generation": 2,
            "owner_id": "worker_resume",
            "checkpoint_digest": None,
            "card_id": "card_native",
        },
    ],
    "clarification_document_continuations": [
        {
            "job_id": "djob_native",
            "card_id": "card_native",
            "turn_id": "t_native",
            "creation_generation": 1,
            "creation_owner_id": "worker_create",
            "checkpoint_digest": "checkpoint",
            "source_hash": "source",
            "runtime_snapshot_digest": "runtime",
            "reserved_generation": 2,
            "consumed_generation": 2,
            "consumed_owner_id": "worker_resume",
        }
    ],
    "clarification_requests": [
        {
            "request_id": "card_native",
            "turn_id": "t_native",
            "session_id": "s_native",
            "user_id": "usr_native",
            "workspace_id": "ws_native",
            "status": "resolved",
        }
    ],
    "document_executions": [{"job_id": "djob_native", "owner": "worker_resume", "state": "review"}],
    "sessions": [{"id": "s_native", "user_id": "usr_native", "workspace_id": "ws_native", "deleted_at": None, "active_turn_id": "t_native"}],
    "workspaces": [{"id": "ws_native", "team_id": "team_native"}],
    "chat_turn_spawn_dispatch": [
        {
            "turn_id": "t_native",
            "session_id": "s_native",
            "state": "spawned",
            "lease_kind": "resume",
            "owner_id": "worker_resume",
            "lease_execution_generation": 2,
            "cancel_requested_at": None,
            "lease_expires_at": "2999-01-01",
        }
    ],
}


def qualifies(proof):
    ctes, parameters = [], []
    for table, baseline in PROOF.items():
        columns = tuple(baseline[0])
        rows = proof[table]
        names = ",".join(f'"{column}"' for column in columns)
        placeholders = ",".join("?" for _ in columns)
        if rows:
            body = "VALUES " + ",".join(f"({placeholders})" for _ in rows)
            parameters.extend(row[column] for row in rows for column in columns)
        else:
            body = f"SELECT {placeholders} WHERE 0"
            parameters.extend(None for _ in columns)
        ctes.append(f'"{table}"({names}) AS ({body})')
    sql = "WITH " + ",".join(ctes) + f" SELECT {MIGRATION.NATIVE_RESUME} FROM current_turn AS NEW, primary_execution AS e, primary_output AS o"
    with sqlite3.connect(":memory:") as db:
        return bool(db.execute(sql, parameters).fetchone()[0])


@pytest.mark.parametrize("creation", [1, 2])
@pytest.mark.parametrize("state", ["review", "published"])
def test_only_original_output_and_consumed_native_result_qualify(creation, state):
    proof = deepcopy(PROOF)
    proof["document_executions"][0]["state"] = state
    for table in ("document_turn_dispatches", "document_planning_attempts", "clarification_document_continuations", "primary_output"):
        column = "created_generation" if table == "primary_output" else "creation_generation"
        proof[table][0][column] = creation
    resumed = creation + 1
    proof["current_turn"][0]["execution_generation"] = resumed
    proof["document_planning_attempts"][1]["creation_generation"] = resumed
    proof["clarification_document_continuations"][0].update(reserved_generation=resumed, consumed_generation=resumed)
    proof["chat_turn_spawn_dispatch"][0]["lease_execution_generation"] = resumed
    assert qualifies(proof)


@pytest.mark.parametrize(
    ("table", "index", "field", "value"),
    [
        ("current_turn", 0, "execution_generation", 1),
        ("primary_execution", 0, "id", "aex_foreign"),
        ("primary_output", 0, "created_generation", 2),
        ("primary_output", 0, "retention_state", "purged"),
        ("document_jobs", 0, "origin_turn_id", "t_foreign"),
        ("document_jobs", 0, "session_id", "s_foreign"),
        ("document_jobs", 0, "requested_by", "usr_foreign"),
        ("document_jobs", 0, "workspace_id", "ws_foreign"),
        ("document_jobs", 0, "team_id", "team_foreign"),
        ("document_jobs", 0, "status", "cancelled"),
        ("document_jobs", 0, "user_source_sha256", "changed"),
        ("turn_capability_execution", 0, "status", "running"),
        ("turn_capability_execution", 0, "output_contract", "other.contract"),
        ("turn_capability_execution", 0, "agent_execution_id", "aex_foreign"),
        ("turn_capability_execution", 0, "turn_id", "t_foreign"),
        ("turn_capability_execution", 0, "team_id", "team_foreign"),
        ("document_turn_dispatches", 0, "creation_generation", 2),
        ("document_turn_dispatches", 0, "owner_id", "foreign"),
        ("document_turn_dispatches", 0, "source_hash", "changed"),
        ("document_turn_dispatches", 0, "runtime_snapshot_digest", "changed"),
        ("document_planning_attempts", 0, "checkpoint_digest", "changed"),
        ("document_planning_attempts", 0, "creation_generation", 2),
        ("document_planning_attempts", 1, "creation_generation", 3),
        ("document_planning_attempts", 1, "owner_id", "foreign"),
        ("document_planning_attempts", 1, "card_id", "card_foreign"),
        ("clarification_document_continuations", 0, "turn_id", "t_foreign"),
        ("clarification_document_continuations", 0, "reserved_generation", None),
        ("clarification_document_continuations", 0, "consumed_generation", None),
        ("clarification_document_continuations", 0, "consumed_owner_id", "foreign"),
        ("clarification_requests", 0, "turn_id", "t_foreign"),
        ("clarification_requests", 0, "session_id", "s_foreign"),
        ("clarification_requests", 0, "user_id", "usr_foreign"),
        ("clarification_requests", 0, "workspace_id", "ws_foreign"),
        ("clarification_requests", 0, "status", "pending"),
        ("document_executions", 0, "owner", "foreign"),
        ("document_executions", 0, "state", "planning"),
        ("document_executions", 0, "state", "question"),
        ("document_executions", 0, "state", "failed"),
        ("sessions", 0, "deleted_at", "2026-01-01"),
        ("sessions", 0, "active_turn_id", "t_foreign"),
        ("workspaces", 0, "team_id", "team_foreign"),
        ("chat_turn_spawn_dispatch", 0, "state", "claimed"),
        ("chat_turn_spawn_dispatch", 0, "lease_kind", "turn"),
        ("chat_turn_spawn_dispatch", 0, "owner_id", "foreign"),
        ("chat_turn_spawn_dispatch", 0, "lease_execution_generation", 1),
        ("chat_turn_spawn_dispatch", 0, "cancel_requested_at", "2026-01-01"),
        ("chat_turn_spawn_dispatch", 0, "lease_expires_at", "2000-01-01"),
        ("chat_turn_spawn_dispatch", 0, "lease_expires_at", None),
    ],
)
def test_foreign_unstarted_stale_or_failed_material_cannot_complete(table, index, field, value):
    proof = deepcopy(PROOF)
    proof[table][index][field] = value
    assert not qualifies(proof)


@pytest.mark.parametrize(
    "table",
    ["document_jobs", "document_dispatches", "clarification_document_continuations", "turn_capability_execution", "document_planning_attempts"],
)
def test_missing_native_material_cannot_complete(table):
    proof = deepcopy(PROOF)
    proof[table] = []
    assert not qualifies(proof)
