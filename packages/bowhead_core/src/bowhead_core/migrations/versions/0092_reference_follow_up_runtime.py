"""Permit exact materialized reference follow-ups through the runtime ledger.

Revision ID: 0092_reference_follow_up_runtime
Revises: 0091_slack_app_settings

No rows are rewritten. Existing composition, model, evidence and continuation
guards are replaced only after their exact current definitions are verified.
"""

from __future__ import annotations

import re
from importlib import import_module

from alembic import context

revision = "0092_reference_follow_up_runtime"
down_revision = "0091_slack_app_settings"
branch_labels = None
depends_on = None

LEGACY = import_module(
    "bowhead_core.migrations.versions.0069_skill_reference_composition",
)
AUTOMATIC = import_module(
    "bowhead_core.migrations.versions.0086_reference_auto_composition",
)
CONTINUATION = import_module(
    "bowhead_core.migrations.versions.0078_reference_bi_continuation",
)
EXTENSIONS = import_module(
    "bowhead_core.migrations.versions.0081_skill_read_extensions",
)
CHECKPOINT = import_module(
    "bowhead_core.migrations.versions.0083_reference_read_checkpoint",
)

CURRENT = {
    LEGACY.TRIGGER: LEGACY.DDL,
    **AUTOMATIC.REPLACEMENTS,
    EXTENSIONS.CONTEXT_TRIGGER: EXTENSIONS.REPLACEMENTS[EXTENSIONS.CONTEXT_TRIGGER][1],
    CHECKPOINT.SCOPE: CHECKPOINT.SCOPE_DDL,
    "trg_chat_reference_evidence_scope": CONTINUATION.REPLACEMENTS["trg_chat_reference_evidence_scope"][1],
}

INITIAL_ONLY = "d.dispatch_kind='start' AND d.parent_turn_id IS NULL"
INITIAL_OR_EXACT_FOLLOW_UP = """(
  (d.dispatch_kind='start' AND d.parent_turn_id IS NULL)
  OR (
    d.dispatch_kind='follow_up' AND d.parent_turn_id IS NOT NULL
    AND d.source_submission_id IS NOT NULL
    AND d.source_submission_ids_json=json_array(d.source_submission_id)
    AND EXISTS (
      SELECT 1 FROM chat_reference_queued_inputs q
      JOIN sessions qs ON qs.id=q.session_id
      JOIN workspaces qw ON qw.id=qs.workspace_id
      JOIN turns qp ON qp.id=q.accepted_parent_turn_id
      JOIN turns qc ON qc.id=q.materialized_turn_id
      WHERE q.team_id=qw.team_id AND q.user_id=qs.user_id
        AND q.workspace_id=qs.workspace_id
        AND q.session_id=d.session_id
        AND q.client_submission_id=d.source_submission_id
        AND q.accepted_parent_turn_id=d.parent_turn_id
        AND q.state='materialized'
        AND q.materialized_turn_id=d.turn_id
        AND qp.session_id=q.session_id
        AND qc.session_id=q.session_id
    )
  )
)
""".strip()


def _replace_dispatch_guard(sql: str) -> str:
    if sql.count(INITIAL_ONLY) != 1:
        raise RuntimeError("reference follow-up runtime source drift")
    return sql.replace(INITIAL_ONLY, INITIAL_OR_EXACT_FOLLOW_UP)


DDL = {name: _replace_dispatch_guard(sql) for name, sql in CURRENT.items()}


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).casefold().rstrip(";")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for name, sql in CURRENT.items():
        row = db.exec_driver_sql(
            "SELECT type,sql FROM sqlite_master WHERE name=?",
            (name,),
        ).fetchone()
        if row is None or row[0] != "trigger" or _normalized(row[1]) != _normalized(sql):
            raise RuntimeError("reference follow-up runtime schema drift")
    db.exec_driver_sql("SAVEPOINT reference_follow_up_runtime")
    try:
        for name in CURRENT:
            db.exec_driver_sql(f"DROP TRIGGER {name}")
        for sql in DDL.values():
            db.exec_driver_sql(sql)
        db.exec_driver_sql("RELEASE SAVEPOINT reference_follow_up_runtime")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT reference_follow_up_runtime")
        db.exec_driver_sql("RELEASE SAVEPOINT reference_follow_up_runtime")
        raise


def downgrade() -> None:
    raise NotImplementedError(
        "Forward-only; restore the matching database snapshot to roll back.",
    )
