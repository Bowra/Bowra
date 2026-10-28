"""Multi-file SQLite Alembic runner."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

TARGET_FILES: dict[str, str] = {
    "core": "bowhead.db",
    "files": "kb_files.db",
    "doc_types": "kb_doc_types.db",
    "extracted_records": "kb_extracted_records.db",
    "extracted_rows": "kb_extracted_rows.db",
    "per_row_jobs": "kb_per_row_jobs.db",
    "team_templates": "kb_team_templates.db",
    "matrix": "kb_matrix.db",
}


def resolve_database_directory(db_url: str | None = None) -> Path:
    value = db_url or os.environ.get("BOWHEAD_DB_URL")
    if not value or not value.startswith("sqlite") or "///" not in value:
        msg = "BOWHEAD_DB_URL must be a SQLite URL"
        raise ValueError(msg)
    return Path(value.split("///", 1)[1]).parent


def upgrade(db_url: str | None = None, target: str | None = None) -> tuple[str, ...]:
    directory = resolve_database_directory(db_url)
    names = (target,) if target else tuple(TARGET_FILES)
    unknown = set(names).difference(TARGET_FILES)
    if unknown:
        msg = f"unknown migration target: {', '.join(sorted(unknown))}"
        raise ValueError(msg)

    from bowhead_core import migrations

    script_location = str(Path(migrations.__file__).resolve().parent)
    completed: list[str] = []
    for name in names:
        path = directory / TARGET_FILES[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        config = Config()
        config.set_main_option("script_location", script_location)
        config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
        config.attributes["bowhead_target"] = name
        command.upgrade(config, "head")
        completed.append(name)
    return tuple(completed)
