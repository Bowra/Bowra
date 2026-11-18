"""Explicit pre-split fixtures for historical single-file business contracts."""

from __future__ import annotations

import atexit
import shutil
import sqlite3
import tempfile
from contextlib import closing
from functools import cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import resolve_database_directory
from bowhead_core.sqlite_domain_upgrade import SOURCE_REVISION
from bowhead_core.sqlite_domains import ORIGINAL_TARGET_FILES

from bowhead_core import migrations

TARGET_FILES = dict(ORIGINAL_TARGET_FILES)


@cache
def migrated_core_template() -> Path:
    """Build the historical Core schema once per test process."""
    directory = Path(tempfile.mkdtemp(prefix="bowhead-legacy-core-"))
    atexit.register(shutil.rmtree, directory, True)
    path = directory / TARGET_FILES["core"]
    upgrade(f"sqlite:///{path}", target="core")
    return path


def copy_migrated_core(path: Path) -> None:
    """Give a test its own writable copy of the migrated Core database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{migrated_core_template().as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source, closing(sqlite3.connect(path)) as destination:
        source.backup(destination)


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
