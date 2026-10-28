"""Chart-style V2 schema is additive, scoped, immutable and drift-checked."""

from __future__ import annotations

import asyncio
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from bowhead_core import canvas_export as legacy_export
from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0049_chart_style_customization")


def _config(path: Path, target: str = "core") -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    return config


def test_upgrade_preserves_existing_export_jobs_and_is_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    command.upgrade(config, MIGRATION.down_revision)
    asyncio.run(legacy_export.init_export_jobs(str(path)))
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO canvas_export_jobs(id,canvas_id,fmt,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("exp_existing", "cv_1", "pdf", "queued", "2026-09-06", "2026-09-06"),
        )

    command.upgrade(config, MIGRATION.revision)
    command.stamp(config, MIGRATION.down_revision)
    # Replay at its historical schema before later revisions replace immutable guards.
    command.upgrade(config, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        # Replay actually reaches this revision (checking the version after stamping head is always true and meaningless).
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
    command.upgrade(config, "head")
    command.upgrade(config, "head")

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT id,renderer_contract_version,status FROM canvas_export_jobs WHERE id='exp_existing'").fetchone() == (
            "exp_existing",
            1,
            "queued",
        )
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'team_chart_style%'")} == {
            "team_chart_styles",
            "team_chart_style_revisions",
            "team_chart_style_drafts",
            "team_chart_style_registry_state",
        }
        assert db.execute("SELECT name FROM sqlite_schema WHERE type='index' AND name='idx_export_jobs_claim_target'").fetchone() == (
            "idx_export_jobs_claim_target",
        )


@pytest.mark.parametrize(
    "target",
    ["files", "doc_types", "extracted_records", "extracted_rows", "per_row_jobs", "team_templates", "matrix"],
)
def test_non_core_target_has_no_chart_style_schema(tmp_path: Path, target: str) -> None:
    path = tmp_path / f"{target}.db"
    command.upgrade(_config(path, target), "head")
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'team_chart_style%' OR name='idx_export_jobs_claim_target'").fetchall() == []
        )


def test_revision_scope_active_name_and_immutability_are_enforced(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    command.upgrade(_config(path), "head")
    style_id = "tcs_0123456789abcdef01234567"
    other_style_id = "tcs_1123456789abcdef01234567"
    revision_id = "tcsr_0123456789abcdef01234567"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for current_style_id in (style_id, other_style_id):
            db.execute(
                "INSERT INTO team_chart_styles("
                "id,team_id,lifecycle,fallback_builtin_preset_id,created_by,created_at,updated_by,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (current_style_id, "team_1", "active", "bowhead-editorial", "u_1", "2026-09-06", "u_1", "2026-09-06"),
            )
        db.execute(
            "INSERT INTO team_chart_style_revisions("
            "id,team_id,style_id,sequence,schema_version,token_schema_version,resolver_policy_version,"
            "name,name_normalized,description,base_preset_id,base_preset_revision,base_preset_checksum,"
            "authoring_base_snapshot_json,authoring_base_snapshot_checksum,overrides_json,"
            "resolved_snapshot_json,snapshot_checksum,revision_checksum,validation_policy_version,"
            "validation_json,operation,created_by,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                "team_1",
                style_id,
                1,
                2,
                2,
                "chart-style/2.0",
                "Editorial",
                "editorial",
                "",
                "bowhead-editorial",
                "2026.09.1",
                "sha256:base",
                "{}",
                "sha256:authoring",
                "{}",
                "{}",
                "sha256:snapshot",
                "sha256:revision",
                "chart-style-validation/1",
                "{}",
                "publish",
                "u_1",
                "2026-09-06",
            ),
        )
        db.execute(
            "UPDATE team_chart_styles SET active_revision_id=?,active_name_normalized=?,row_version=row_version+1 WHERE id=?",
            (revision_id, "editorial", style_id),
        )

        with pytest.raises(sqlite3.IntegrityError, match="active revision unavailable"):
            db.execute(
                "UPDATE team_chart_styles SET active_revision_id=?,active_name_normalized=?,row_version=row_version+1 WHERE id=?",
                (revision_id, "editorial", other_style_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE team_chart_style_revisions SET name='Changed' WHERE id=?", (revision_id,))
        with pytest.raises(sqlite3.IntegrityError, match="base revision unavailable"):
            db.execute(
                "INSERT INTO team_chart_style_drafts("
                "style_id,team_id,schema_version,based_on_revision_id,base_preset_id,base_preset_revision,"
                "base_preset_checksum,authoring_base_snapshot_json,authoring_base_snapshot_checksum,name,"
                "overrides_json,created_by,created_at,updated_by,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    other_style_id,
                    "team_1",
                    2,
                    revision_id,
                    "bowhead-editorial",
                    "2026.09.1",
                    "sha256:base",
                    "{}",
                    "sha256:authoring",
                    "Other",
                    "{}",
                    "u_1",
                    "2026-09-06",
                    "u_1",
                    "2026-09-06",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="published chart style is retained"):
            db.execute("DELETE FROM team_chart_styles WHERE id=?", (style_id,))


@pytest.mark.parametrize("kind", ["table", "index", "trigger", "column"])
def test_schema_drift_is_rejected(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "core.db"
    config = _config(path)
    if kind == "column":
        command.upgrade(config, MIGRATION.down_revision)
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE canvas_export_jobs")
            db.execute("CREATE TABLE canvas_export_jobs(id TEXT PRIMARY KEY, renderer_contract_version TEXT DEFAULT '1')")
    else:
        command.upgrade(config, MIGRATION.revision)
        command.stamp(config, MIGRATION.down_revision)
        with sqlite3.connect(path) as db:
            if kind == "table":
                db.execute("ALTER TABLE team_chart_style_registry_state ADD COLUMN unexpected TEXT")
            elif kind == "index":
                db.execute("DROP INDEX idx_team_chart_styles_list")
                db.execute("CREATE INDEX idx_team_chart_styles_list ON team_chart_styles(id)")
            else:
                db.execute("DROP TRIGGER trg_team_chart_style_revision_update")
                db.execute(
                    "CREATE TRIGGER trg_team_chart_style_revision_update BEFORE UPDATE ON team_chart_style_revisions "
                    "WHEN 0 BEGIN SELECT RAISE(ABORT,'disabled'); END"
                )

    with pytest.raises(RuntimeError, match="schema drift"):
        command.upgrade(config, "head")
