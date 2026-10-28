"""Store file categories as language-neutral codes instead of Chinese labels.

Revision ID: 0124_file_category_codes
Revises: 0123_team_capability_bundles
"""

from __future__ import annotations

from alembic import context

revision = "0124_file_category_codes"
down_revision = "0123_team_capability_bundles"
branch_labels = None
depends_on = None

# Frozen copy of the legacy label → code table at the time of this revision. Do not
# import it from application code: a later edit there must not change what this
# immutable revision did. Values outside this table (unknown labels) are kept as-is;
# NULL stays NULL.
_LEGACY_TO_CODE = (
    ("汇报材料", "report"),
    ("数据明细", "dataset"),
    ("参考资料", "reference"),
    ("会议纪要", "meeting_notes"),
    ("口径定义", "metric_definition"),
    ("截图", "screenshot"),
    ("其他", "other"),
)


def _has_table(db, name: str) -> bool:  # type: ignore[no-untyped-def]
    return db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).first() is not None


def _dirty_rows(db) -> list[str]:  # type: ignore[no-untyped-def]
    """Rows whose UPDATE would be aborted by the 0046/0048 artifacts guards.

    Those BEFORE UPDATE triggers reject *any* update of a live (`deleted_at IS NULL`) artifact
    that already has a purge claim, or whose document-input copy was claimed for cleanup. Both
    states violate invariants; rewriting the category must not be the thing that trips over them
    with an opaque trigger message, so we name the rows before touching anything.
    """
    labels = tuple(label for label, _ in _LEGACY_TO_CODE)
    marks = ",".join("?" for _ in labels)
    problems: list[str] = []
    checks = (
        (
            "artifact_purge_claims",
            f"SELECT a.id FROM artifacts a JOIN artifact_purge_claims c ON c.artifact_id=a.id "
            f"WHERE a.deleted_at IS NULL AND a.category IN ({marks}) ORDER BY a.id",
            "artifacts that are live (deleted_at IS NULL) but already have a row in artifact_purge_claims; "
            "finish or remove the stale purge claim (or soft-delete the artifact) before upgrading",
        ),
        (
            "document_input_copies",
            f"SELECT a.id FROM artifacts a JOIN document_input_copies d ON d.artifact_id=a.id "
            f"WHERE a.deleted_at IS NULL AND d.cleanup_claimed_at IS NOT NULL AND a.category IN ({marks}) ORDER BY a.id",
            "document-input copies whose cleanup was claimed (cleanup_claimed_at set) but whose artifact is still "
            "live (deleted_at IS NULL); complete the document-input cleanup (soft-delete the artifact) before upgrading",
        ),
    )
    for table, sql, advice in checks:
        if not _has_table(db, table):
            continue
        ids = [row[0] for row in db.exec_driver_sql(sql, labels)]
        if ids:
            problems.append(f"[{table}] {len(ids)} {advice}: {', '.join(ids)}")
    return problems


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    if not _has_table(db, "artifacts"):
        return
    problems = _dirty_rows(db)
    if problems:
        raise RuntimeError(
            "0124_file_category_codes: refusing to rewrite file categories; inconsistent artifacts found "
            "(nothing was changed, the database stays at 0123). " + " | ".join(problems)
        )
    # One statement per value; atomicity comes from the single transaction the Bowhead
    # migration env opens around the whole upgrade (env.py, transaction_per_migration=False).
    for legacy, code in _LEGACY_TO_CODE:
        db.exec_driver_sql("UPDATE artifacts SET category=? WHERE category=?", (code, legacy))


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
