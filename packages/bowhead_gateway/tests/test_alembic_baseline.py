from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import anyio
import pytest
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.session_store import SessionStore
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION as SQLITE_LEGACY_REVISION
from bowhead_core.sqlite_domains import domain_database_path
from bowhead_gateway.edition_extensions import read_build_edition
from bowhead_gateway.installation import prepare_installation
from bowhead_gateway.main import create_app
from gateway_legacy_database import TARGET_FILES, upgrade

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = SQLITE_LEGACY_REVISION


def test_gateway_requires_explicit_preparation_for_a_legacy_schema(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "bowhead.db"
    community = read_build_edition() == "community"
    if not community:
        with closing(sqlite3.connect(db_path)) as connection:
            connection.executescript(initial_schema("core"))
        unprepared_database = db_path.read_bytes()
    monkeypatch.setenv("BOWHEAD_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BOWHEAD_ENV", "dev")
    # Lifespan derives artifact-fetch keys from BOWHEAD_JWT_SECRET and refuses startup without it; independent of local env/.env.
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-alembic-baseline-jwt-secret-32bytes")
    monkeypatch.setenv("BOWHEAD_DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    # With a Fernet key the lifespan opens the privacy ledger; keep it inside the test directory, not /app.
    monkeypatch.setenv("BOWHEAD_PRIVACY_LEDGER_DB", str(tmp_path / "privacy" / "deletions.db"))
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "local")
    monkeypatch.setenv("BOWHEAD_SANDBOX_RUNTIME", "local")

    async def initialize() -> None:
        app = create_app()
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(
        RuntimeError,
        match=(
            "Community installation preparation must run before Gateway startup" if community else "clarification invalidation schema is unavailable"
        ),
    ):
        anyio.run(initialize)

    if not community:
        assert db_path.read_bytes() == unprepared_database

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='clarification_resolution_outbox'",
            ).fetchone()
            is None
        ), "post-1.0.0 tables must not be created by Store.initialize()"

    assert upgrade(f"sqlite+aiosqlite:///{(db_path).with_name('bowhead.db')}") == tuple(TARGET_FILES)
    for filename in TARGET_FILES.values():
        with sqlite3.connect(tmp_path / filename) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='public_entry_provider_status'",
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='clarification_resolution_outbox'",
        ).fetchone() == ("clarification_resolution_outbox",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='clarification_invalidation_outbox'",
        ).fetchone() == ("clarification_invalidation_outbox",)

    # Community must refuse a populated legacy database with no edition provenance.
    if community:
        from bowhead_core.installation import InstallationError

        with pytest.raises(InstallationError, match="unknown installation provenance"):
            prepare_installation(db_url=f"sqlite+aiosqlite:///{db_path}", migration_command=str(Path(sys.executable).with_name("bowhead")))
        return

    # Production startup requires the original coordinator and twelve-file layout.
    prepare_installation(db_url=f"sqlite+aiosqlite:///{db_path}", migration_command=str(Path(sys.executable).with_name("bowhead")))
    anyio.run(initialize)

    store = SessionStore(db_path)
    execution_path = domain_database_path(db_path, "execution")
    assert anyio.run(store.chat_submission_protocol_schema_ready) is True
    with closing(sqlite3.connect(execution_path)) as connection:
        tombstone_trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_turn_delete_cancel_receipt_tombstone'",
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_turn_delete_cancel_receipt_tombstone")
    assert anyio.run(store.chat_submission_protocol_schema_ready) is False
    with closing(sqlite3.connect(execution_path)) as connection:
        connection.execute(
            """
            CREATE TRIGGER trg_turn_delete_cancel_receipt_tombstone
            BEFORE DELETE ON turns WHEN 0
            BEGIN
              UPDATE turn_cancel_receipts SET turn_erased=1 WHERE turn_id=OLD.id;
            END
            """,
        )
    assert anyio.run(store.chat_submission_protocol_schema_ready) is False
    with closing(sqlite3.connect(execution_path)) as connection:
        connection.execute("DROP TRIGGER trg_turn_delete_cancel_receipt_tombstone")
        connection.execute(tombstone_trigger)
    assert anyio.run(store.chat_submission_protocol_schema_ready) is True
    with closing(sqlite3.connect(execution_path)) as connection:
        connection.execute("DROP TABLE clarification_resolution_outbox")
    assert anyio.run(store.chat_submission_protocol_schema_ready) is False
