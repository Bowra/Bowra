"""0060_bundle_inventory：加表迁移 + 行存取 + CHECK 上界。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.skill_import.bundle_inventory_store import get_bundle_inventory, save_bundle_inventory

from bowhead_core import migrations

pytestmark = pytest.mark.integration

MIGRATION = import_module("bowhead_core.migrations.versions.0060_bundle_inventory")


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def test_upgrade_creates_table_is_idempotent_and_core_only(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='chat_attachment_bundle_inventory'").fetchone()
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.stamp(config, MIGRATION.down_revision)
    # Replay only this revision, then restore the ledger to head; later historical revisions (e.g. 0063 table creation) may not rerun on upgraded DBs.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Verify replay reached this revision (checking after stamp head is always true and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.stamp(config, "head")
    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(chat_attachment_bundle_inventory)")}
        assert cols == {"bundle_artifact_id", "created_by", "inventory_json", "created_at", "updated_at"}
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO chat_attachment_bundle_inventory VALUES ('art_x','u',?, 't','t')",
                (json.dumps({"pad": "x" * 70_000}),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO chat_attachment_bundle_inventory VALUES ('art_y','u','not json','t','t')")
    # Other targets are no-ops.
    other = tmp_path / "other.db"
    command.upgrade(_config(other, target="files"), "head")
    with sqlite3.connect(other) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='chat_attachment_bundle_inventory'").fetchone()


def test_store_roundtrip_and_owner_filter(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")

    async def _run():
        assert await save_bundle_inventory(str(path), bundle_artifact_id="art_1", created_by="u1", inventory_json=json.dumps({"grade": "red"}))
        assert await save_bundle_inventory(
            str(path), bundle_artifact_id="art_1", created_by="u1", inventory_json=json.dumps({"grade": "green"})
        )  # upsert
        assert (await get_bundle_inventory(str(path), bundle_artifact_id="art_1", created_by="u1")) == {"grade": "green"}
        assert await get_bundle_inventory(str(path), bundle_artifact_id="art_1", created_by="u2") is None
        assert await get_bundle_inventory(str(path), bundle_artifact_id="nope") is None
        assert not await save_bundle_inventory(str(path), bundle_artifact_id="art_2", created_by="u1", inventory_json="x" * 70_000)

    asyncio.run(_run())
