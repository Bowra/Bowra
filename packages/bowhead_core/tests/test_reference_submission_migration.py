"""Upgrade live receipt/lease metadata without losing bytes or widening old pairs."""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.session_store import SessionStore

from bowhead_core import migrations

pytestmark = pytest.mark.unit
# Fixed upgrade endpoints, with revision IDs owned by their immutable modules.
_OLD = import_module("bowhead_core.migrations.versions.0060_bundle_inventory").revision
_NEW = import_module("bowhead_core.migrations.versions.0061_reference_submission_contract").revision


def _upgrade(path: Path, revision: str, target: str = "core") -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    command.upgrade(config, revision)


def _seed(connection):
    for i, (version, batch) in enumerate(
        [
            ("chat-submit.v1", "a"),
            ("chat-model.v2", "b"),
            ("chat-model-effort.v3", "c"),
        ]
    ):
        identity = ("start", version, batch, "1" * 64, "2" * 64, "3" * 64)
        connection.execute(
            "INSERT INTO chat_submission_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "team_1",
                "u_1",
                f"submission_{i}",
                "ws_1",
                None,
                "s_1",
                "t_1",
                *identity,
                "started",
                '{ "session_id": "s_1", "turn_id": "t_1", "说明": "原字节" }',
                "2026-09-09T00:00:00+00:00",
                "2099-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO chat_submission_inflight VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "team_1",
                "u_1",
                f"pending_{i}",
                "ws_1",
                None,
                *identity,
                "owner",
                3,
                "2099-01-01T00:00:00+00:00",
                "2026-09-09T00:00:00+00:00",
            ),
        )


@pytest.mark.asyncio
async def test_upgrade_preserves_receipt_and_live_lease_bytes_and_store_reader(tmp_path):
    path = tmp_path / "core.db"
    _upgrade(path, _OLD)
    with sqlite3.connect(path) as connection:
        _seed(connection)
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in ("chat_submission_receipts", "chat_submission_inflight")
        }
    _upgrade(path, _NEW)
    _upgrade(path, _NEW)
    with sqlite3.connect(path) as connection:
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
            assert connection.execute(f"PRAGMA index_info(idx_{table}_expiry)").fetchone()[2] in {"expires_at", "lease_expires_at"}
            # The newly supported pair is valid, but neither cross-pair is.
            connection.execute(f"UPDATE {table} SET submission_contract_version='chat-references.v1', model_control_batch='references'")
            for field, value in [("model_control_batch", "a"), ("submission_contract_version", "chat-submit.v1"), ("model_control_batch", "future")]:
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(f"UPDATE {table} SET {field}=?", (value,))
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {table} SET request_fingerprint='short'")
    receipt = await SessionStore(str(path)).get_chat_submission_receipt(
        team_id="team_1", user_id="u_1", client_submission_id="submission_0", workspace_id="ws_1", requested_session_id=None
    )
    assert receipt.submission_contract_version == "chat-references.v1"
    assert receipt.model_control_batch == "references"
    assert receipt.response["说明"] == "原字节"


@pytest.mark.parametrize("drift", ["column", "trigger", "index"])
def test_copy_failure_rolls_back_both_tables_and_revision(tmp_path, drift):
    path = tmp_path / "core.db"
    _upgrade(path, _OLD)
    with sqlite3.connect(path) as connection:
        _seed(connection)
        if drift == "column":
            connection.execute("ALTER TABLE chat_submission_inflight ADD COLUMN future_field TEXT")
        elif drift == "trigger":
            connection.execute("CREATE TRIGGER future_trigger AFTER DELETE ON chat_submission_inflight BEGIN SELECT 1; END")
        else:
            connection.execute("DROP INDEX idx_chat_submission_inflight_expiry")
            connection.execute("CREATE INDEX idx_chat_submission_inflight_expiry ON chat_submission_inflight(created_at)")
        before = connection.execute("SELECT * FROM chat_submission_receipts").fetchall()
    with pytest.raises(RuntimeError, match="drift"):
        _upgrade(path, _NEW)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == _OLD
        assert connection.execute("SELECT * FROM chat_submission_receipts").fetchall() == before
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_references_new'").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE chat_submission_receipts SET submission_contract_version='chat-references.v1', model_control_batch='references'"
            )


@pytest.mark.parametrize("target", [target for target in TARGET_FILES if target != "core"])
def test_other_targets_advance_without_submission_tables(tmp_path, target):
    path = tmp_path / f"{target}.db"
    _upgrade(path, _NEW, target)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == _NEW
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'chat_submission_%'").fetchall()
