"""Native receipt indexes preserve old events across all database targets."""

import json
import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0198_artifact_presentation")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_upgrade_preserves_historical_events_and_is_idempotent(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        with sqlite3.connect(path) as db:
            db.executemany(
                "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES(?,'t_old',?,'tool_result',?,'2026-10-01')",
                [(f"evt_{i}", i, value) for i, value in enumerate(("{", "null", "[]", '{"result_meta":{}}'), 1)],
            )
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    installed = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == installed
    if target == "core":
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (MIGRATION.INDEX,)).fetchone() == (1,)
            payload = json.dumps(
                {
                    "result_meta": {
                        "artifact_presentation": {
                            "contract": "artifact-presentation.v1",
                            "origin": "explicit_tool",
                            "items": [{"artifact_id": "art_a"}],
                        },
                        "artifact_presentation_receipt": {"call_key": "a" * 64},
                    }
                }
            )
            db.execute(
                "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES('evt_native','t_old',5,'tool_result',?,'2026-10-01')",
                (payload,),
            )
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at) VALUES('evt_duplicate','t_old',6,'tool_result',?,'2026-10-01')",
                    (payload,),
                )
