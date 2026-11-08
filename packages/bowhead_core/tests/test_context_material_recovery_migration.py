"""Native material JSON evolves without modifying existing domain data."""

from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core.sqlite_domain_upgrade import run_domain_upgrade
from bowhead_core.sqlite_domains import DOMAIN_TARGET_FILES
from bowhead_core.sqlite_upgrade import database_head
from test_sqlite_domain_upgrade import _advance
from test_sqlite_domain_upgrade import directory as directory
from test_sqlite_domain_upgrade import legacy_source as legacy_source
from test_sqlite_mutation_migration import evidence

from bowhead_core import migrations

MIGRATION = import_module("bowhead_core.migrations.versions.0225_context_material_recovery")
pytestmark = pytest.mark.integration


def advance_to(directory, revision):
    def append(name, path):
        config = Config()
        config.set_main_option("script_location", str(Path(migrations.__file__).parent))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        config.attributes["bowhead_target"] = name
        config.attributes["bowhead_domain_coordinator"] = True
        command.upgrade(config, revision)

    run_domain_upgrade(directory, _advance, target_head=revision, upgrade_append=append)


def test_upgrade_preserves_all_domain_rows_and_is_idempotent(directory):
    advance_to(directory, MIGRATION.down_revision)
    before = evidence(directory)
    advance_to(directory, MIGRATION.revision)
    assert evidence(directory) == before
    assert all(database_head(directory / name) == MIGRATION.revision for name in DOMAIN_TARGET_FILES.values())
    advance_to(directory, MIGRATION.revision)
    assert evidence(directory) == before
