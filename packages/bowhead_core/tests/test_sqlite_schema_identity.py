"""Only exact runtime KB field indexes may vary after schema admission."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.kb.extracted_record_store import _hash_field_name, _rebuild_field_indexes
from bowhead_core.kb.extracted_rows_store import _rebuild_row_field_indexes
from bowhead_core.sqlite_domain_upgrade import run_domain_upgrade, schema_digest
from bowhead_core.sqlite_schema_identity import is_runtime_field_index
from bowhead_core.sqlite_upgrade import SQLiteUpgradeError


def _index(name, table="kb_extracted_records", *, unique=False):
    return (
        "index",
        name,
        table,
        f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {table}(workspace_id,doc_type_id,CAST(json_extract(fields_json,'$.amount[0].normalized.amount') AS REAL)) WHERE soft_deleted_at IS NULL",
    )


def test_only_exact_name_owner_and_nonunique_builder_shape_are_mutable():
    name = f"idx_kb_records_fld_01234567_{_hash_field_name('amount')}_v2"
    assert is_runtime_field_index(_index(name))
    assert not is_runtime_field_index(_index(name, unique=True))
    assert not is_runtime_field_index(_index(name, table="kb_extracted_rows"))
    assert not is_runtime_field_index(_index(name.replace("amount_", "forged_")))
    bad = list(_index(name))
    bad[3] += " OR 1=1"
    assert not is_runtime_field_index(bad)


@pytest.mark.asyncio
async def test_valid_field_indexes_added_after_ready_survive_ordinary_restart(tmp_path, monkeypatch):
    import anyio

    directory = tmp_path / "sqlite"
    await anyio.to_thread.run_sync(lambda: upgrade(f"sqlite:///{directory / 'bowhead.db'}"))
    records = directory / "kb_extracted_records.db"
    rows = directory / "kb_extracted_rows.db"
    with closing(sqlite3.connect(records)) as db:
        expected = schema_digest(db)
    await _rebuild_field_indexes(records, "doctype-runtime", [SimpleNamespace(name="amount", type="currency")])
    await _rebuild_row_field_indexes(rows, "doctype-runtime", [SimpleNamespace(name="金额", type="number")])
    with closing(sqlite3.connect(records)) as db:
        assert schema_digest(db) == expected
        assert db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'idx_kb_records_fld_%'").fetchone()
    await anyio.to_thread.run_sync(lambda: run_domain_upgrade(directory, lambda *_: pytest.fail("A field-index change must not replay migrations")))
    malformed = f"idx_kb_rows_fld_01234567_{_hash_field_name('amount')}_v2"
    with closing(sqlite3.connect(records)) as db:
        db.execute(_index(malformed)[3])
    with pytest.raises(SQLiteUpgradeError, match="schema verification failed"):
        await anyio.to_thread.run_sync(lambda: run_domain_upgrade(directory, lambda *_: None))
