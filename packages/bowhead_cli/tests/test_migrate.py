from __future__ import annotations

import sqlite3

from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.migrations import chain_head

# Do not hardcode a revision number; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()


def test_upgrade_creates_all_targets_at_current_head(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"

    completed = upgrade(f"sqlite+aiosqlite:///{db_path}")

    assert completed == tuple(TARGET_FILES)
    for target, filename in TARGET_FILES.items():
        with sqlite3.connect(tmp_path / filename) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            assert revision == (HEAD_REVISION,), target
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'users'").fetchone() == (1,)
        decision_columns = {row[1] for row in connection.execute("PRAGMA table_info(turn_control_decisions)").fetchall()}
        assert {
            "requester_ref",
            "effect_principal_ref",
            "source_projection_json",
            "source_projection_digest",
            "prior_receipt_id",
            "prior_generation_transition_epoch",
            "recovery_attempt_count",
            "recovery_next_attempt_at",
            "recovery_last_code",
        } <= decision_columns
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='terminal_decision_bound_insert'").fetchone() == (1,)
        generation_columns = {row[1] for row in connection.execute("PRAGMA table_info(turn_generations)").fetchall()}
        assert {"admission_request_digest", "execution_intent_json"} <= generation_columns
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_decision_effects'").fetchone() == (1,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='terminal_decision_effect_identity_immutable'"
        ).fetchone() == (1,)


def test_upgrade_is_idempotent_for_existing_schema(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    url = f"sqlite+aiosqlite:///{db_path}"

    upgrade(url)

    assert upgrade(url) == tuple(TARGET_FILES)
