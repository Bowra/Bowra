"""Agent M2 candidate name claims and recoverable baseline import ledger.

The migration is expand-only.  It freezes neither business rows nor live
pointers; the explicit offline baseline command owns those mutations.

Revision ID: 0031_agent_release_baseline
Revises: 0030_canvas_surface_target
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0031_agent_release_baseline"
down_revision = "0030_canvas_surface_target"
branch_labels = None
depends_on = None


_TABLE_DDL: tuple[tuple[str, str], ...] = (
    (
        "agent_release_candidate_name_claim",
        """
        CREATE TABLE agent_release_candidate_name_claim (
          team_id        TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          candidate_id   TEXT NOT NULL,
          agent_id       TEXT NOT NULL,
          locale         TEXT NOT NULL,
          normalized_key TEXT NOT NULL,
          claim_kind     TEXT NOT NULL CHECK(claim_kind IN ('exclusive','inherited')),
          state          TEXT NOT NULL CHECK(state IN ('reserved','activated','released')),
          claimed_at     TEXT NOT NULL,
          settled_at     TEXT,
          PRIMARY KEY(candidate_id, locale, normalized_key),
          UNIQUE(team_id, candidate_id, agent_id, locale, normalized_key),
          CHECK(length(locale) > 0 AND length(normalized_key) > 0),
          CHECK(
            (state='reserved' AND settled_at IS NULL)
            OR (state IN ('activated','released') AND settled_at IS NOT NULL)
          ),
          FOREIGN KEY(team_id, agent_id)
            REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, agent_id, candidate_id)
            REFERENCES team_subagent_release_candidate(team_id, subagent_id, id)
            ON DELETE RESTRICT
        ) WITHOUT ROWID
        """,
    ),
    (
        "agent_release_baseline_run",
        """
        CREATE TABLE agent_release_baseline_run (
          id                 TEXT PRIMARY KEY CHECK(id LIKE 'abr_%'),
          state              TEXT NOT NULL
            CHECK(state IN ('staging','staged','importing','completed','failed','abandoned')),
          compiler_version   TEXT NOT NULL,
          source_build       TEXT,
          snapshot_at        TEXT NOT NULL,
          expected_count     INTEGER NOT NULL DEFAULT 0 CHECK(expected_count >= 0),
          staged_count       INTEGER NOT NULL DEFAULT 0 CHECK(staged_count >= 0),
          ready_count        INTEGER NOT NULL DEFAULT 0 CHECK(ready_count >= 0),
          degraded_count     INTEGER NOT NULL DEFAULT 0 CHECK(degraded_count >= 0),
          blocked_count      INTEGER NOT NULL DEFAULT 0 CHECK(blocked_count >= 0),
          imported_count     INTEGER NOT NULL DEFAULT 0 CHECK(imported_count >= 0),
          error_count        INTEGER NOT NULL DEFAULT 0 CHECK(error_count >= 0),
          expected_digest    TEXT CHECK(expected_digest IS NULL OR expected_digest LIKE 'sha256:%'),
          lease_owner        TEXT,
          lease_expires_at   TEXT,
          started_at         TEXT NOT NULL,
          staged_at          TEXT,
          finished_at        TEXT,
          updated_at         TEXT NOT NULL,
          CHECK(
            (lease_owner IS NULL AND lease_expires_at IS NULL)
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
          )
        )
        """,
    ),
    (
        "agent_release_baseline_expected",
        """
        CREATE TABLE agent_release_baseline_expected (
          run_id                   TEXT NOT NULL
            REFERENCES agent_release_baseline_run(id) ON DELETE CASCADE,
          team_id                  TEXT NOT NULL,
          agent_id                 TEXT NOT NULL,
          source_row_version       INTEGER NOT NULL CHECK(source_row_version >= 1),
          source_updated_at        TEXT NOT NULL,
          stage_state              TEXT NOT NULL
            CHECK(stage_state IN ('pending','ready','degraded','blocked')),
          source_hash              TEXT CHECK(source_hash IS NULL OR source_hash LIKE 'sha256:%'),
          definition_hash          TEXT CHECK(definition_hash IS NULL OR definition_hash LIKE 'sha256:%'),
          dependency_fingerprint   TEXT
            CHECK(dependency_fingerprint IS NULL OR dependency_fingerprint LIKE 'sha256:%'),
          capability_fingerprint   TEXT
            CHECK(capability_fingerprint IS NULL OR capability_fingerprint LIKE 'sha256:%'),
          audience_hash            TEXT CHECK(audience_hash IS NULL OR audience_hash LIKE 'sha256:%'),
          safety_report_id         TEXT,
          reason_codes_json        TEXT NOT NULL DEFAULT '[]' CHECK(length(reason_codes_json) > 0),
          imported_revision_id     TEXT,
          imported_release_id      TEXT,
          imported_at              TEXT,
          PRIMARY KEY(run_id, team_id, agent_id),
          CHECK(
            (
              stage_state='pending'
              AND source_hash IS NULL
              AND definition_hash IS NULL
              AND dependency_fingerprint IS NULL
              AND capability_fingerprint IS NULL
              AND audience_hash IS NULL
              AND safety_report_id IS NULL
            )
            OR (
              stage_state!='pending'
              AND source_hash IS NOT NULL
              AND definition_hash IS NOT NULL
              AND dependency_fingerprint IS NOT NULL
              AND capability_fingerprint IS NOT NULL
              AND audience_hash IS NOT NULL
              AND safety_report_id IS NOT NULL
            )
          ),
          CHECK(
            (imported_at IS NULL AND imported_revision_id IS NULL AND imported_release_id IS NULL)
            OR (
              imported_at IS NOT NULL
              AND imported_revision_id IS NOT NULL
              AND imported_release_id IS NOT NULL
            )
          ),
          CHECK(imported_at IS NULL OR stage_state IN ('ready','degraded')),
          FOREIGN KEY(team_id, agent_id)
            REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, agent_id, safety_report_id)
            REFERENCES agent_safety_report(team_id, agent_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, agent_id, imported_revision_id)
            REFERENCES team_subagent_revision(team_id, subagent_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, agent_id, imported_release_id)
            REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT
        ) WITHOUT ROWID
        """,
    ),
)

_INDEX_DDL: tuple[tuple[str, str], ...] = (
    (
        "idx_agent_candidate_name_claim_active",
        """
        CREATE INDEX idx_agent_candidate_name_claim_active
        ON agent_release_candidate_name_claim(team_id, agent_id, state)
        """,
    ),
    (
        "uq_agent_release_baseline_one_open",
        """
        CREATE UNIQUE INDEX uq_agent_release_baseline_one_open
        ON agent_release_baseline_run((1))
        WHERE state IN ('staging','staged','importing')
        """,
    ),
    (
        "idx_agent_release_baseline_expected_stage",
        """
        CREATE INDEX idx_agent_release_baseline_expected_stage
        ON agent_release_baseline_expected(run_id, stage_state, team_id, agent_id)
        """,
    ),
    (
        "idx_agent_release_baseline_expected_import",
        """
        CREATE INDEX idx_agent_release_baseline_expected_import
        ON agent_release_baseline_expected(run_id, imported_at, team_id, agent_id)
        """,
    ),
)


def _canonical_sql(value: str) -> str:
    return "".join(value.lower().replace("if not exists", "").split())


def _object_sql(connection: Any, *, object_type: str, name: str) -> str | None:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def _ensure_object(
    connection: Any,
    *,
    object_type: str,
    name: str,
    ddl: str,
) -> None:
    existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None:
        connection.exec_driver_sql(ddl)
        existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None or _canonical_sql(existing) != _canonical_sql(ddl):
        raise RuntimeError(f"0031 found incompatible {object_type} semantics: {name}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for table_name, ddl in _TABLE_DDL:
        _ensure_object(
            connection,
            object_type="table",
            name=table_name,
            ddl=ddl,
        )
    for index_name, ddl in _INDEX_DDL:
        _ensure_object(
            connection,
            object_type="index",
            name=index_name,
            ddl=ddl,
        )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
