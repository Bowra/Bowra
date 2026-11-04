"""Read provenance before resuming migrations without admitting business access."""

from __future__ import annotations

import gc
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from bowhead_core.installation import preflight_installation, read_installation
from bowhead_core.sqlite_domains import DOMAIN_LAYOUT


def test_provenance_probe_can_precede_interrupted_domain_upgrade(tmp_path: Path) -> None:
    core = tmp_path / "bowhead.db"
    with sqlite3.connect(core) as db:
        db.execute(
            "CREATE TABLE installation_identity(singleton INTEGER,installation_id TEXT,origin_edition TEXT,current_edition TEXT,setup_complete INTEGER)"
        )
        db.execute("INSERT INTO installation_identity VALUES(1,'installation-original','business','business',1)")
    (tmp_path / "sqlite-layout.json").write_text(json.dumps({"format": 1, "layout": DOMAIN_LAYOUT, "status": "in_progress", "phase": "copy"}))
    identity = read_installation(str(core))
    assert identity is not None and identity.installation_id == "installation-original"
    assert not preflight_installation(str(core), "business")


@pytest.mark.parametrize("known_identity", [False, True])
def test_provenance_probes_release_wal_connections_without_waiting_for_garbage_collection(tmp_path, known_identity):
    core = tmp_path / "bowhead.db"
    with closing(sqlite3.connect(core)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE retained(value TEXT)")
        db.execute("INSERT INTO retained VALUES ('original')")
        if known_identity:
            db.execute(
                "CREATE TABLE installation_identity(singleton INTEGER,installation_id TEXT,origin_edition TEXT,current_edition TEXT,setup_complete INTEGER)"
            )
            db.execute("INSERT INTO installation_identity VALUES(1,'installation-original','business','business',1)")
        db.commit()
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        assert (read_installation(str(core)) is not None) == known_identity
        assert not preflight_installation(str(core), "business")
        with closing(sqlite3.connect(core, timeout=0.05)) as db:
            assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
            assert db.execute("SELECT value FROM retained").fetchall() == [("original",)]
    finally:
        if enabled:
            gc.enable()
        gc.collect()
