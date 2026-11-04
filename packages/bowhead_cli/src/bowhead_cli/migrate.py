"""Multi-file SQLite Alembic runner."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION, SPLIT_REVISION, run_domain_upgrade
from bowhead_core.sqlite_domains import DOMAIN_LAYOUT, DOMAIN_TARGET_FILES, ORIGINAL_TARGET_FILES
from bowhead_core.sqlite_upgrade import _load_state, run_legacy_upgrade

TARGET_FILES = dict(DOMAIN_TARGET_FILES)


def resolve_database_directory(db_url: str | None = None) -> Path:
    value = db_url or os.environ.get("BOWHEAD_DB_URL")
    if not value or not value.startswith("sqlite") or "///" not in value:
        msg = "BOWHEAD_DB_URL must be a SQLite URL"
        raise ValueError(msg)
    path = Path(value.split("///", 1)[1])
    if path.name != "bowhead.db":
        raise ValueError("BOWHEAD_DB_URL must identify the migration-managed bowhead.db file")
    return path.parent


def upgrade(db_url: str | None = None) -> tuple[str, ...]:
    directory = resolve_database_directory(db_url)

    from bowhead_core import migrations

    script_location = str(Path(migrations.__file__).resolve().parent)
    migration_config = Config()
    migration_config.set_main_option("script_location", script_location)
    scripts = ScriptDirectory.from_config(migration_config)
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise ValueError("SQLite migration history must have exactly one head")
    known_revisions = frozenset(revision.revision for revision in scripts.walk_revisions())

    def run_revision(name: str, path: Path, revision: str, *, cleanup: bool = False) -> None:
        config = Config()
        config.set_main_option("script_location", script_location)
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        config.attributes["bowhead_target"] = name
        config.attributes["bowhead_split_cleanup"] = cleanup
        config.attributes["bowhead_domain_coordinator"] = revision != SOURCE_REVISION
        command.upgrade(config, revision)

    state = _load_state(directory) if directory.exists() else None
    domain_layout = state is not None and state["layout"] == DOMAIN_LAYOUT
    if not domain_layout:
        run_legacy_upgrade(
            directory,
            tuple(ORIGINAL_TARGET_FILES),
            SOURCE_REVISION,
            known_revisions,
            lambda name, path: run_revision(name, path, SOURCE_REVISION),
        )
    return run_domain_upgrade(
        directory,
        lambda name, path, cleanup: run_revision(name, path, SPLIT_REVISION, cleanup=cleanup),
        target_head=heads[0],
        known_revisions=known_revisions,
        upgrade_append=lambda name, path: run_revision(name, path, heads[0]),
        revision_order=tuple(revision.revision for revision in reversed(list(scripts.iterate_revisions(heads[0], "base")))),
    )
