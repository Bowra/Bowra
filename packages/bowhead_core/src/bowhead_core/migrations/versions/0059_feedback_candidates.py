"""B 线批 2a′：团队反馈候选（建议收件箱）+ 冷却表。

Revision ID: 0059_feedback_candidates
Revises: 0058_chat_input_document
Create Date: 2026-09-09

设计权威：docs/sprints/agent-feedback-loop-2026-09-08/tech-design.md §2.1。
纯加表（无 copy-migrate）；仅 core 目标，其余七个 SQLite 目标 no-op。
`uq_fbc_open_sig` 是部分唯一索引（先例：0015 `idx_sjr_one_running`）——
同 (team_id, semantic_sig) 的 **open** 候选全团队唯一，合并靠 store 层
BEGIN IMMEDIATE 单事务（evidence 追加数组）。
"""

from __future__ import annotations

from alembic import context

revision = "0059_feedback_candidates"
down_revision = "0058_chat_input_document"
branch_labels = None
depends_on = None

_CANDIDATES_DDL = """
CREATE TABLE feedback_candidates (
    id               TEXT PRIMARY KEY,
    team_id          TEXT NOT NULL,
    target_kind      TEXT NOT NULL,
    target_ref       TEXT NOT NULL DEFAULT '',
    base_fingerprint TEXT NOT NULL DEFAULT '',
    proposed         TEXT NOT NULL,
    rubric_text      TEXT NOT NULL DEFAULT '',
    evidence         TEXT NOT NULL DEFAULT '[]',
    semantic_sig     TEXT NOT NULL,
    evidence_count   INTEGER NOT NULL DEFAULT 1,
    cluster_id       TEXT,
    state            TEXT NOT NULL DEFAULT 'open',
    proposer_id      TEXT NOT NULL,
    decided_by       TEXT,
    decided_at       TEXT,
    decide_note      TEXT,
    expires_at       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
)
"""

_SUPPRESSIONS_DDL = """
CREATE TABLE feedback_candidate_suppressions (
    team_id              TEXT NOT NULL,
    semantic_sig         TEXT NOT NULL,
    target_kind          TEXT NOT NULL DEFAULT '',
    target_ref           TEXT NOT NULL DEFAULT '',
    last_state           TEXT NOT NULL,
    last_evidence_count  INTEGER NOT NULL DEFAULT 1,
    pending_contributors TEXT NOT NULL DEFAULT '[]',
    suppressed_until     TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (team_id, semantic_sig)
)
"""

_EXPECTED_CANDIDATE_COLUMNS = {
    "id",
    "team_id",
    "target_kind",
    "target_ref",
    "base_fingerprint",
    "proposed",
    "rubric_text",
    "evidence",
    "semantic_sig",
    "evidence_count",
    "cluster_id",
    "state",
    "proposer_id",
    "decided_by",
    "decided_at",
    "decide_note",
    "expires_at",
    "created_at",
    "updated_at",
}

_EXPECTED_SUPPRESSION_COLUMNS = {
    "team_id",
    "semantic_sig",
    "target_kind",
    "target_ref",
    "last_state",
    "last_evidence_count",
    "pending_contributors",
    "suppressed_until",
    "created_at",
    "updated_at",
}

_INDEX_CONTRACT_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "uq_fbc_open_sig": ("UNIQUE", "WHERE state='open'"),
}

_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "idx_fbc_team_state",
        "CREATE INDEX idx_fbc_team_state ON feedback_candidates(team_id, state)",
    ),
    (
        "uq_fbc_open_sig",
        "CREATE UNIQUE INDEX uq_fbc_open_sig "
        "ON feedback_candidates(team_id, semantic_sig) WHERE state='open'",
    ),
)


def _columns(connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    }


def _ensure_table(connection, table: str, ddl: str, expected: set[str]) -> None:
    existing = _columns(connection, table)
    if not existing:
        connection.exec_driver_sql(ddl)
        return
    if existing != expected:
        raise RuntimeError(
            f"feedback table drift: {table} has {sorted(existing)}, "
            f"expected {sorted(expected)}",
        )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    _ensure_table(
        connection,
        "feedback_candidates",
        _CANDIDATES_DDL,
        _EXPECTED_CANDIDATE_COLUMNS,
    )
    _ensure_table(
        connection,
        "feedback_candidate_suppressions",
        _SUPPRESSIONS_DDL,
        _EXPECTED_SUPPRESSION_COLUMNS,
    )
    existing_indexes = {
        str(row[0]): str(row[1] or "")
        for row in connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type='index'",
        ).fetchall()
    }
    for name, ddl in _INDEXES:
        current = existing_indexes.get(name)
        if current is None:
            connection.exec_driver_sql(ddl)
            continue
        # 同名索引必须携带同等约束——比名字存在更强（防同名非 partial/非 unique
        # 索引静默顶替，唯一性契约破了却不报 drift）。
        for fragment in _INDEX_CONTRACT_FRAGMENTS.get(name, ()):
            if fragment not in current:
                raise RuntimeError(f"feedback index drift: {name} lacks {fragment!r}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
