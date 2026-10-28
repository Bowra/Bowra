"""B 线批 3a：教练办案 sidecar 表 + 候选回执时间列。

Revision ID: 0062_feedback_casework
Revises: 0061_reference_submission_contract
Create Date: 2026-09-09

设计权威：docs/sprints/agent-feedback-loop-2026-09-08/tech-design.md §2.8.2。
仅 core 目标，其余七个 SQLite 目标 no-op。expand-only：
- 新表 `feedback_casework`（一候选一行；办案产物是可重算的派生物）；
- 回执真相 `receipt_at` 也放在这张 sidecar 表上（不 ALTER 0059 的候选表——0059 的 drift 检查是
  列集合严格相等，且迁移历史不可改）；过期回执按「候选 expired 且 sidecar 无 receipt_at」LEFT JOIN 找行，
  惰性翻转的行也补得上。
"""

from __future__ import annotations

from alembic import context

revision = "0062_feedback_casework"
down_revision = "0061_reference_submission_contract"
branch_labels = None
depends_on = None

_CASEWORK_DDL = """
CREATE TABLE feedback_casework (
    candidate_id   TEXT PRIMARY KEY,
    team_id        TEXT NOT NULL,
    trigger        TEXT NOT NULL DEFAULT 'cron',
    status         TEXT NOT NULL DEFAULT 'queued',
    reason_code    TEXT,
    report_json    TEXT NOT NULL DEFAULT '{}',
    prefill_json   TEXT,
    base_json      TEXT NOT NULL DEFAULT '{}',
    attempts       INTEGER NOT NULL DEFAULT 0,
    redraft_count  INTEGER NOT NULL DEFAULT 0,
    redraft_day    TEXT NOT NULL DEFAULT '',
    receipt_at     TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)
"""

_EXPECTED_CASEWORK_COLUMNS = {
    "candidate_id",
    "team_id",
    "trigger",
    "status",
    "reason_code",
    "report_json",
    "prefill_json",
    "base_json",
    "attempts",
    "redraft_count",
    "redraft_day",
    "receipt_at",
    "started_at",
    "finished_at",
    "created_at",
    "updated_at",
}

_INDEXES = (
    ("idx_fbcw_team_status", "CREATE INDEX idx_fbcw_team_status ON feedback_casework(team_id, status)"),
    ("idx_fbcw_status_updated", "CREATE INDEX idx_fbcw_status_updated ON feedback_casework(status, updated_at)"),
)


def _columns(connection, table: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    existing = _columns(connection, "feedback_casework")
    if not existing:
        connection.exec_driver_sql(_CASEWORK_DDL)
    elif existing != _EXPECTED_CASEWORK_COLUMNS:
        raise RuntimeError(
            f"feedback_casework drift: has {sorted(existing)}, expected {sorted(_EXPECTED_CASEWORK_COLUMNS)}",
        )
    if not _columns(connection, "feedback_candidates"):
        raise RuntimeError("feedback_candidates missing: 0059 must run before 0062")
    existing_indexes = {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index'",
        ).fetchall()
    }
    for name, ddl in _INDEXES:
        if name not in existing_indexes:
            connection.exec_driver_sql(ddl)


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
