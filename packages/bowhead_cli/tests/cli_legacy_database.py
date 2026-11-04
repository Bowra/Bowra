"""Explicit pre-split fixtures for historical single-file business contracts."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import resolve_database_directory
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_core.sqlite_domains import ORIGINAL_TARGET_FILES

from bowhead_core import migrations

TARGET_FILES = dict(ORIGINAL_TARGET_FILES)


def upgrade(db_url: str | None = None, target: str | None = None) -> tuple[str, ...]:
    """Build historical fixtures without entering the production startup runner."""
    if target is not None and target not in TARGET_FILES:
        raise ValueError(f"Unknown historical SQLite target: {target}")
    directory = resolve_database_directory(db_url)
    directory.mkdir(parents=True, exist_ok=True)
    targets = (target,) if target is not None else tuple(TARGET_FILES)
    for name in targets:
        config = Config()
        config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{directory / TARGET_FILES[name]}")
        config.attributes["bowhead_target"] = name
        command.upgrade(config, SOURCE_REVISION)
    return targets
