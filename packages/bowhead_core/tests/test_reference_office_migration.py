"""Advance persisted-envelope readers without rewriting existing data."""

import sqlite3
from importlib import import_module

import pytest
from alembic import command
from test_office_upload_confirmation_migration import _config

pytestmark = pytest.mark.integration
REVISION = import_module("bowhead_core.migrations.versions.0132_reference_office_reads")


@pytest.mark.parametrize("target", ["core", "files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"])
def test_office_envelope_upgrade_keeps_existing_schema_and_data(tmp_path, target):
    path = tmp_path / "existing.db"
    config = _config(path, target)
    command.upgrade(config, REVISION.down_revision)
    with sqlite3.connect(path) as db:
        before = [line for line in db.iterdump() if '"alembic_version"' not in line]
    command.upgrade(config, REVISION.revision)
    command.upgrade(config, REVISION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION.revision,)
        assert [line for line in db.iterdump() if '"alembic_version"' not in line] == before
