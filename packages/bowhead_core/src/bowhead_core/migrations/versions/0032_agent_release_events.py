"""Durable Agent release events and notification outbox.

M2 activation cannot publish a live release unless its audit event and every
frozen notification recipient are committed in the same transaction.  The
worker that projects this outbox into the notification center is a later work
package; this revision only adds the atomic persistence boundary.

Revision ID: 0032_agent_release_events
Revises: 0031_agent_release_baseline
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0032_agent_release_events"
down_revision = "0031_agent_release_baseline"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "team_subagent_release_event",
        """
        CREATE TABLE team_subagent_release_event (
          id            TEXT PRIMARY KEY CHECK(id LIKE 'are_%'),
          team_id       TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          subagent_id   TEXT NOT NULL,
          release_id    TEXT,
          candidate_id  TEXT,
          event_type    TEXT NOT NULL CHECK(event_type IN (
            'candidate.submitted','candidate.rejected','candidate.withdrawn',
            'candidate.stale','release.activated','audience.contracted'
          )),
          actor_user_id TEXT NOT NULL,
          payload_json  TEXT NOT NULL CHECK(length(payload_json) > 0),
          created_at    TEXT NOT NULL,
          CHECK(release_id IS NOT NULL OR candidate_id IS NOT NULL),
          UNIQUE(team_id, subagent_id, id),
          FOREIGN KEY(team_id, subagent_id)
            REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, release_id)
            REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT,
          FOREIGN KEY(team_id, subagent_id, candidate_id)
            REFERENCES team_subagent_release_candidate(team_id, subagent_id, id)
            ON DELETE RESTRICT
        )
        """,
    ),
    (
        "table",
        "team_subagent_notification_outbox",
        """
        CREATE TABLE team_subagent_notification_outbox (
          id                TEXT PRIMARY KEY CHECK(id LIKE 'ano_%'),
          release_event_id  TEXT NOT NULL
            REFERENCES team_subagent_release_event(id) ON DELETE RESTRICT,
          recipient_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          notification_type TEXT NOT NULL CHECK(length(notification_type) > 0),
          dedupe_key         TEXT NOT NULL UNIQUE CHECK(length(dedupe_key) > 0),
          payload_json       TEXT NOT NULL CHECK(length(payload_json) > 0),
          state              TEXT NOT NULL
            CHECK(state IN ('pending','sending','delivered','failed')),
          attempt_count      INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
          next_attempt_at    TEXT NOT NULL,
          lease_owner        TEXT,
          lease_expires_at   TEXT,
          delivered_at      TEXT,
          last_error_code    TEXT,
          created_at         TEXT NOT NULL,
          UNIQUE(release_event_id, recipient_user_id, notification_type),
          CHECK(
            (lease_owner IS NULL AND lease_expires_at IS NULL)
            OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
          ),
          CHECK(state != 'delivered' OR delivered_at IS NOT NULL)
        )
        """,
    ),
    (
        "index",
        "idx_agent_release_event_history",
        """
        CREATE INDEX idx_agent_release_event_history
        ON team_subagent_release_event(team_id, subagent_id, created_at DESC)
        """,
    ),
    (
        "index",
        "idx_agent_release_outbox_pending",
        """
        CREATE INDEX idx_agent_release_outbox_pending
        ON team_subagent_notification_outbox(state, next_attempt_at, created_at)
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_release_event_immutable_update",
        """
        CREATE TRIGGER trg_team_subagent_release_event_immutable_update
        BEFORE UPDATE ON team_subagent_release_event
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_release_event is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_release_event_immutable_delete",
        """
        CREATE TRIGGER trg_team_subagent_release_event_immutable_delete
        BEFORE DELETE ON team_subagent_release_event
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_release_event is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_release_event_actor_team",
        """
        CREATE TRIGGER trg_team_subagent_release_event_actor_team
        BEFORE INSERT ON team_subagent_release_event
        WHEN NOT EXISTS (
          SELECT 1 FROM users
          WHERE users.id=NEW.actor_user_id AND users.team_id=NEW.team_id
        )
        BEGIN
          SELECT RAISE(ABORT, 'release event actor belongs to another team');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_release_candidate_payload_immutable",
        """
        CREATE TRIGGER trg_team_subagent_release_candidate_payload_immutable
        BEFORE UPDATE ON team_subagent_release_candidate
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.subagent_id IS NOT OLD.subagent_id
          OR NEW.revision_id IS NOT OLD.revision_id
          OR NEW.base_live_release_id IS NOT OLD.base_live_release_id
          OR NEW.target_audience_json IS NOT OLD.target_audience_json
          OR NEW.target_audience_hash IS NOT OLD.target_audience_hash
          OR NEW.capability_fingerprint IS NOT OLD.capability_fingerprint
          OR NEW.dependency_fingerprint IS NOT OLD.dependency_fingerprint
          OR NEW.risk_compiler_version IS NOT OLD.risk_compiler_version
          OR NEW.risk_json IS NOT OLD.risk_json
          OR NEW.safety_report_id IS NOT OLD.safety_report_id
          OR NEW.eval_evidence_json IS NOT OLD.eval_evidence_json
          OR NEW.release_note IS NOT OLD.release_note
          OR NEW.candidate_hash IS NOT OLD.candidate_hash
          OR NEW.submitted_by IS NOT OLD.submitted_by
          OR NEW.submitted_at IS NOT OLD.submitted_at
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_release_candidate payload is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_release_candidate_immutable_delete",
        """
        CREATE TRIGGER trg_team_subagent_release_candidate_immutable_delete
        BEFORE DELETE ON team_subagent_release_candidate
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_release_candidate is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_release_outbox_payload_immutable",
        """
        CREATE TRIGGER trg_team_subagent_release_outbox_payload_immutable
        BEFORE UPDATE ON team_subagent_notification_outbox
        WHEN NEW.id IS NOT OLD.id
          OR NEW.release_event_id IS NOT OLD.release_event_id
          OR NEW.recipient_user_id IS NOT OLD.recipient_user_id
          OR NEW.notification_type IS NOT OLD.notification_type
          OR NEW.dedupe_key IS NOT OLD.dedupe_key
          OR NEW.payload_json IS NOT OLD.payload_json
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
          SELECT RAISE(ABORT, 'team_subagent_notification_outbox payload is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_team_subagent_release_outbox_recipient_team",
        """
        CREATE TRIGGER trg_team_subagent_release_outbox_recipient_team
        BEFORE INSERT ON team_subagent_notification_outbox
        WHEN NOT EXISTS (
          SELECT 1
          FROM team_subagent_release_event event
          JOIN users recipient ON recipient.id=NEW.recipient_user_id
          WHERE event.id=NEW.release_event_id
            AND recipient.team_id=event.team_id
        )
        BEGIN
          SELECT RAISE(ABORT, 'release notification recipient belongs to another team');
        END
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
        raise RuntimeError(f"0032 found incompatible {object_type} semantics: {name}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for object_type, name, ddl in _OBJECTS:
        _ensure_object(
            connection,
            object_type=object_type,
            name=name,
            ddl=ddl,
        )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
