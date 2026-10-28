"""Access-request store — content access-request + approval queue (content-permissions wave 2).

pull-direction of sharing: a viewer who can't access a shared resource files a
request; the resource owner (∪ team-admin) approves → a grant is created.

Mirrors ``approval_store.ApprovalStore`` (atomic ``set_decision_once``, dedupe via
an active-unique partial index). report-only this wave; ``kind`` column keeps it
generic. See docs/sprints/access-requests/{prd,tech-design}.md.

table access_requests:
    id, team_id, kind, resource_id, requester_id, owner_id, message,
    status (pending|approved|denied|stale|approve_failed|withdrawn|expired),
    decided_by, decided_at, created_at, updated_at,
    + lifecycle columns (0126_access_request_lifecycle): decision_note, expires_at,
    requested_role, granted_permission, withdrawn_at, target, shadow,
    grant_expires_at, cooldown_until, escalated_at, release_candidate_id

Dual ownership (0011/0016 precedent): the 1.0.0 table is created by ``SCHEMA`` here and
the 0125 lifecycle columns are added by both ``initialize()`` (PRAGMA-guarded ALTER) and
the Alembic revision, in the same order; both sides are idempotent.

Shadow rows (``shadow=1``) are requests whose resource could not be resolved for the
requester (missing, cross-team, already accessible, not requestable). They exist so the
requester-facing status is identical for existing and missing resources; approver-side
reads always exclude them and they only ever end as ``expired`` / ``withdrawn`` / ``stale``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx

SCHEMA = """
CREATE TABLE IF NOT EXISTS access_requests (
    id            TEXT PRIMARY KEY,
    team_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    requester_id  TEXT NOT NULL,
    owner_id      TEXT,
    message       TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    decided_by    TEXT,
    decided_at    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_acr_owner_status ON access_requests(team_id, owner_id, status);
CREATE INDEX IF NOT EXISTS idx_acr_requester ON access_requests(team_id, requester_id, status);
CREATE INDEX IF NOT EXISTS idx_acr_resource ON access_requests(team_id, kind, resource_id, status);
-- list_for_approver admin-history (status != 'pending') + the ORDER BY created_at DESC of
-- the approver page (polled every 5s); serves both team-scoped list scopes without a temp sort.
CREATE INDEX IF NOT EXISTS idx_acr_team_created ON access_requests(team_id, created_at DESC);
-- dedupe: at most one pending request per (team, kind, resource, requester). Greenfield
-- table → all columns exist from boot, so this partial unique index lives in SCHEMA
-- (no ALTER-after ordering hazard like approval_store's request_key index).
CREATE UNIQUE INDEX IF NOT EXISTS idx_acr_active
    ON access_requests(team_id, kind, resource_id, requester_id) WHERE status='pending';
"""

# Lifecycle columns added by 0126_access_request_lifecycle. Keep the order identical to the
# revision so a Store-initialized table and an Alembic-upgraded table have the same layout.
_LIFECYCLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("decision_note", "TEXT"),
    ("expires_at", "TEXT"),
    ("requested_role", "TEXT"),
    ("granted_permission", "TEXT"),
    ("withdrawn_at", "TEXT"),
    ("target", "TEXT"),
    ("shadow", "INTEGER NOT NULL DEFAULT 0"),
    ("grant_expires_at", "TEXT"),
    ("cooldown_until", "TEXT"),
    ("escalated_at", "TEXT"),
    ("release_candidate_id", "TEXT"),
    # 0127_access_request_legacy_origin: rows written by the retired report-only intake.
    ("legacy_origin", "INTEGER NOT NULL DEFAULT 0"),
)
# ``release_candidate_id`` sentinel while an Agent release approval is being started (the
# request is claimed as ``approved_pending`` before publishing, so withdraw / deny / expiry
# cannot race the publish).
RELEASE_CLAIM = ""

_LIFECYCLE_INDEX = "CREATE INDEX IF NOT EXISTS idx_acr_pending_expiry ON access_requests(status, expires_at)"

AccessRequestStatus = Literal["pending", "approved", "denied", "stale", "approve_failed", "withdrawn", "expired", "approved_pending"]


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass
class AccessRequest:
    id: str
    team_id: str
    kind: str
    resource_id: str
    requester_id: str
    owner_id: str | None
    message: str | None
    status: str
    decided_by: str | None
    decided_at: str | None
    created_at: str
    updated_at: str
    decision_note: str | None = None
    expires_at: str | None = None
    requested_role: str | None = None
    granted_permission: str | None = None
    withdrawn_at: str | None = None
    target: str | None = None
    shadow: bool = False
    grant_expires_at: str | None = None
    cooldown_until: str | None = None
    escalated_at: str | None = None
    release_candidate_id: str | None = None
    legacy_origin: bool = False


@dataclass(frozen=True)
class DecisionDetails:
    """Expansion-only fields written in the same UPDATE as the decision."""

    note: str | None = None
    granted_permission: str | None = None
    grant_expires_at: str | None = None
    cooldown_until: str | None = None
    release_candidate_id: str | None = None


@dataclass
class AccessRequestStore:
    db_path: str

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_shared_ctx(self.db_path) as db:
            await db.executescript(SCHEMA)
            columns = {str(row[1]) for row in await db.execute_fetchall("PRAGMA table_info(access_requests)")}
            for name, ddl in _LIFECYCLE_COLUMNS:
                if name not in columns:
                    await db.execute(f"ALTER TABLE access_requests ADD COLUMN {name} {ddl}")
            await db.execute(_LIFECYCLE_INDEX)
            await db.commit()

    async def enqueue_once(
        self,
        *,
        team_id: str,
        kind: str,
        resource_id: str,
        requester_id: str,
        owner_id: str | None,
        message: str | None,
        shadow: bool = False,
        expires_at: str | None = None,
    ) -> tuple[AccessRequest, bool]:
        """Insert a pending request; if one already pending for
        (team, kind, resource, requester), return it with created=False.

        The intake always passes ``shadow=True`` with an ``expires_at``: an ``approved_pending``
        request for the same resource also counts as the active one, so it is returned instead
        of a duplicate. The defaults (a regular row without expiry, marked ``legacy_origin``)
        reproduce the rows written by the retired report-only intake, which still exist in
        upgraded databases.

        ``legacy_origin`` rows are never shown in the requester's views (their presence would
        reveal that the resource existed). When the intake meets such a pending row it takes
        it over as a fresh request (message, created / expiry times), so the requester sees
        the same thing as for a brand-new placeholder."""
        existing = await self._get_active(team_id, kind, resource_id, requester_id)
        if existing is None and shadow:
            existing = await self._get_release_pending(team_id, kind, resource_id, requester_id)
        if existing is not None and existing.legacy_origin and shadow and existing.status == "pending":
            taken = await self._take_over_legacy(existing.id, message=message, expires_at=expires_at)
            if taken is not None:
                return taken, False
        if existing is not None:
            return existing, False
        rid = f"acr_{uuid4().hex[:12]}"
        now = _now()
        legacy_origin = not shadow and expires_at is None
        try:
            async with open_shared_ctx(self.db_path) as db:
                await db.execute(
                    "INSERT INTO access_requests(id, team_id, kind, resource_id, requester_id, "
                    "owner_id, message, created_at, updated_at, shadow, expires_at, legacy_origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rid,
                        team_id,
                        kind,
                        resource_id,
                        requester_id,
                        owner_id,
                        message,
                        now,
                        now,
                        1 if shadow else 0,
                        expires_at,
                        1 if legacy_origin else 0,
                    ),
                )
                await db.commit()
        except aiosqlite.IntegrityError:
            existing = await self._get_active(team_id, kind, resource_id, requester_id)
            if existing is not None:
                return existing, False
            raise
        return AccessRequest(
            id=rid,
            team_id=team_id,
            kind=kind,
            resource_id=resource_id,
            requester_id=requester_id,
            owner_id=owner_id,
            message=message,
            status="pending",
            decided_by=None,
            decided_at=None,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            shadow=shadow,
            legacy_origin=legacy_origin,
        ), True

    async def get(self, request_id: str, *, team_id: str | None = None) -> AccessRequest | None:
        sql = "SELECT * FROM access_requests WHERE id=?"
        params: list[str] = [request_id]
        if team_id is not None:
            sql += " AND team_id=?"
            params.append(team_id)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                row = await cur.fetchone()
        return _row(row) if row else None

    async def _take_over_legacy(self, request_id: str, *, message: str | None, expires_at: str | None) -> AccessRequest | None:
        """The intake meets a pending legacy-origin row: restate it as a new-flow request."""
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE access_requests SET legacy_origin=0, message=?, created_at=?, expires_at=?, "
                "escalated_at=COALESCE(escalated_at, ?), updated_at=? WHERE id=? AND status='pending' AND legacy_origin=1",
                (message, now, expires_at, now, now, request_id),
            )
            await db.commit()
        return await self.get(request_id) if cur.rowcount == 1 else None

    async def _get_release_pending(self, team_id: str, kind: str, resource_id: str, requester_id: str) -> AccessRequest | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM access_requests WHERE team_id=? AND kind=? AND resource_id=? AND requester_id=? AND status='approved_pending' LIMIT 1",
                (team_id, kind, resource_id, requester_id),
            ) as cur:
                row = await cur.fetchone()
        return _row(row) if row else None

    async def _get_active(
        self,
        team_id: str,
        kind: str,
        resource_id: str,
        requester_id: str,
    ) -> AccessRequest | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM access_requests WHERE team_id=? AND kind=? AND resource_id=? AND requester_id=? AND status='pending'",
                (team_id, kind, resource_id, requester_id),
            ) as cur:
                row = await cur.fetchone()
        return _row(row) if row else None

    async def set_decision_once(
        self,
        *,
        request_id: str,
        team_id: str,
        status: AccessRequestStatus,
        decided_by: str,
        decision: DecisionDetails | None = None,
    ) -> tuple[AccessRequest | None, bool]:
        """Atomic pending→terminal transition (single serialization point). team_id in the
        WHERE closes cross-team decide. transitioned = rowcount==1 (idempotency guard).

        ``decision`` persists the approver note, granted permission, grant
        expiry and requester cooldown in the same UPDATE; it never applies to shadow rows."""
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            if decision is None:
                cur = await db.execute(
                    "UPDATE access_requests SET status=?, decided_by=?, decided_at=?, updated_at=? WHERE id=? AND team_id=? AND status='pending'",
                    (status, decided_by, now, now, request_id, team_id),
                )
            else:
                cur = await db.execute(
                    "UPDATE access_requests SET status=?, decided_by=?, decided_at=?, updated_at=?, decision_note=?, "
                    "granted_permission=?, grant_expires_at=?, cooldown_until=?, release_candidate_id=? "
                    "WHERE id=? AND team_id=? AND status='pending' AND shadow=0",
                    (
                        status,
                        decided_by,
                        now,
                        now,
                        decision.note,
                        decision.granted_permission,
                        decision.grant_expires_at,
                        decision.cooldown_until,
                        decision.release_candidate_id,
                        request_id,
                        team_id,
                    ),
                )
            await db.commit()
        return await self.get(request_id, team_id=team_id), cur.rowcount == 1

    async def mark_approve_failed(self, *, request_id: str, team_id: str) -> None:
        """Recovery: an approved request whose add_grant raised → approve_failed
        (so the requester's CTA, keyed off the live grant, lets them re-request)."""
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE access_requests SET status='approve_failed', updated_at=? WHERE id=? AND team_id=? AND status='approved'",
                (_now(), request_id, team_id),
            )
            await db.commit()

    async def resolve_pending_for_grant(
        self,
        *,
        team_id: str,
        kind: str,
        resource_id: str,
        requester_id: str,
        decided_by: str,
        status: AccessRequestStatus = "approved",
    ) -> AccessRequest | None:
        """Out-of-band close: a grant arrived (direct share / team_wide / admin console) →
        flip that requester's pending request terminal. Returns the row if one matched."""
        existing = await self._get_active(team_id, kind, resource_id, requester_id)
        if existing is None or existing.shadow:
            # Shadow rows only end through withdraw / expiry / member removal, so their
            # requester-visible lifecycle never depends on what happens to the resource.
            return None
        row, _ = await self.set_decision_once(
            request_id=existing.id,
            team_id=team_id,
            status=status,
            decided_by=decided_by,
        )
        return row

    async def stale_pending_for_resource(
        self,
        *,
        team_id: str,
        kind: str,
        resource_id: str,
    ) -> list[AccessRequest]:
        """Resource deleted / went team_wide → all its pending requests become 'stale'.
        Returns the (now-stale) rows so the caller can resolve owner notifications +
        notify each requester (已不存在 for delete / 已可访问 for team_wide)."""
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM access_requests WHERE team_id=? AND kind=? AND resource_id=? AND status='pending' AND shadow=0",
                (team_id, kind, resource_id),
            ) as cur:
                rows = [_row(r) for r in await cur.fetchall()]
            if rows:
                await db.execute(
                    "UPDATE access_requests SET status='stale', updated_at=? WHERE team_id=? AND kind=? AND resource_id=? AND status='pending' AND shadow=0",
                    (now, team_id, kind, resource_id),
                )
                await db.commit()
        return rows

    async def list_for_approver(
        self,
        *,
        team_id: str,
        user_id: str,
        is_admin: bool,
        scope: str = "pending",
        limit: int = 100,
    ) -> list[AccessRequest]:
        """Requests this user can decide: ones they own, plus (if team-admin) all team requests.
        scope=pending → only pending; else recent history."""
        status_clause = "status='pending'" if scope == "pending" else "status != 'pending'"
        # Shadow rows are never shown to approvers. team_subagent requests are decided by the
        # current Agent Owner only (team roles are not an Owner bypass), so admins don't list them.
        sql = f"SELECT * FROM access_requests WHERE team_id=? AND {status_clause} AND shadow=0"  # noqa: S608 — status_clause is a literal
        params: list = [team_id]
        if not is_admin:
            sql += " AND owner_id=?"
            params.append(user_id)
        else:
            sql += " AND (kind != 'team_subagent' OR owner_id=?)"
            params.append(user_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def prior_denied_count(
        self,
        *,
        team_id: str,
        kind: str,
        resource_id: str,
        requester_id: str,
    ) -> int:
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT COUNT(*) FROM access_requests WHERE team_id=? AND kind=? AND resource_id=? AND requester_id=? AND status='denied' AND shadow=0",
                (team_id, kind, resource_id, requester_id),
            ) as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_pending_for_resources(
        self,
        *,
        team_id: str,
        kind: str,
        resource_ids: list[str],
    ) -> dict[str, int]:
        """Batch pending count per resource (share dialog 'N 待处理' badge)."""
        if not resource_ids:
            return {}
        ph = ",".join("?" for _ in resource_ids)
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                f"SELECT resource_id, COUNT(*) FROM access_requests WHERE team_id=? AND kind=? "  # noqa: S608 — ph is placeholders
                f"AND status='pending' AND shadow=0 AND resource_id IN ({ph}) GROUP BY resource_id",
                (team_id, kind, *resource_ids),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return {r[0]: int(r[1]) for r in rows}

    # ── requester views, lifecycle and release settlement ─────────────────────────

    async def promote_shadow(self, *, request_id: str, team_id: str, owner_id: str | None) -> bool:
        """Turn a still-pending shadow placeholder into a real request routed to ``owner_id``.
        Returns False when the row was withdrawn/expired/voided meanwhile."""
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE access_requests SET shadow=0, owner_id=?, updated_at=? WHERE id=? AND team_id=? AND status='pending' AND shadow=1",
                (owner_id, _now(), request_id, team_id),
            )
            await db.commit()
        return cur.rowcount == 1

    async def withdraw_once(self, *, request_id: str, team_id: str, requester_id: str) -> tuple[AccessRequest | None, bool]:
        """Requester-only pending→withdrawn. Returns (row, transitioned); row is None when the
        request does not exist in this team or belongs to someone else."""
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE access_requests SET status='withdrawn', withdrawn_at=?, updated_at=? "
                "WHERE id=? AND team_id=? AND requester_id=? AND status='pending'",
                (now, now, request_id, team_id, requester_id),
            )
            await db.commit()
        row = await self.get(request_id, team_id=team_id)
        if row is None or row.requester_id != requester_id:
            return None, False
        return row, cur.rowcount == 1

    async def expire_due(self, *, now: str, limit: int = 500) -> list[AccessRequest]:
        """pending rows whose ``expires_at`` has passed → expired. Returns the transitioned rows.
        Rows without ``expires_at`` (created by the legacy intake) never expire."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM access_requests WHERE status='pending' AND expires_at IS NOT NULL AND expires_at <= ? ORDER BY expires_at LIMIT ?",
                (now, limit),
            ) as cur:
                due = [_row(r) for r in await cur.fetchall()]
            expired: list[AccessRequest] = []
            for row in due:
                upd = await db.execute(
                    "UPDATE access_requests SET status='expired', updated_at=? WHERE id=? AND status='pending'",
                    (now, row.id),
                )
                if upd.rowcount == 1:
                    row.status = "expired"
                    row.updated_at = now
                    expired.append(row)
            await db.commit()
        return expired

    async def latest_for_requester(self, *, team_id: str, kind: str, resource_id: str, requester_id: str) -> AccessRequest | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM access_requests WHERE team_id=? AND kind=? AND resource_id=? AND requester_id=? "
                "AND legacy_origin=0 ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (team_id, kind, resource_id, requester_id),
            ) as cur:
                row = await cur.fetchone()
        return _row(row) if row else None

    async def active_cooldown_until(self, *, team_id: str, kind: str, resource_id: str, requester_id: str, now: str) -> str | None:
        """Latest still-running cooldown for (requester, resource); only real denied rows carry one."""
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT MAX(cooldown_until) FROM access_requests WHERE team_id=? AND kind=? AND resource_id=? "
                "AND requester_id=? AND status='denied' AND cooldown_until IS NOT NULL AND cooldown_until > ?",
                (team_id, kind, resource_id, requester_id, now),
            ) as cur,
        ):
            row = await cur.fetchone()
        return str(row[0]) if row and row[0] else None

    async def list_for_requester(self, *, team_id: str, requester_id: str, limit: int = 100) -> list[AccessRequest]:
        """The requester's own requests (scope=mine), shadow rows included."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM access_requests WHERE team_id=? AND requester_id=? AND legacy_origin=0 ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (team_id, requester_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def void_pending_for_requester(self, *, requester_id: str, team_id: str | None = None) -> list[AccessRequest]:
        """The requester left the team (or deleted the account) → their pending requests become
        stale. ``team_id=None`` voids across every team. Returns the voided rows."""
        sql = "SELECT * FROM access_requests WHERE requester_id=? AND status='pending'"
        params: list[str] = [requester_id]
        if team_id is not None:
            sql += " AND team_id=?"
            params.append(team_id)
        now = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = [_row(r) for r in await cur.fetchall()]
            voided: list[AccessRequest] = []
            for row in rows:
                upd = await db.execute(
                    "UPDATE access_requests SET status='stale', updated_at=? WHERE id=? AND status='pending'",
                    (now, row.id),
                )
                if upd.rowcount == 1:
                    row.status = "stale"
                    voided.append(row)
            await db.commit()
        return voided

    async def due_escalations(self, *, created_before: str, kinds: tuple[str, ...], limit: int = 200) -> list[AccessRequest]:
        """Real pending requests created before ``created_before`` that were never escalated."""
        if not kinds:
            return []
        ph = ",".join("?" for _ in kinds)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM access_requests WHERE status='pending' AND shadow=0 AND escalated_at IS NULL "  # noqa: S608 — ph is placeholders
                f"AND created_at <= ? AND kind IN ({ph}) ORDER BY created_at LIMIT ?",
                (created_before, *kinds, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def mark_escalated(self, *, request_id: str, now: str) -> bool:
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE access_requests SET escalated_at=? WHERE id=? AND status='pending' AND escalated_at IS NULL",
                (now, request_id),
            )
            await db.commit()
        return cur.rowcount == 1

    async def list_pending_real(
        self,
        *,
        kind: str,
        team_id: str | None = None,
        resource_id: str | None = None,
        requester_id: str | None = None,
        limit: int = 500,
    ) -> list[AccessRequest]:
        """Real (non-shadow) pending requests of one kind, optionally narrowed."""
        sql = "SELECT * FROM access_requests WHERE status='pending' AND shadow=0 AND kind=?"
        params: list[Any] = [kind]
        for column, value in (("team_id", team_id), ("resource_id", resource_id), ("requester_id", requester_id)):
            if value is not None:
                sql += f" AND {column}=?"
                params.append(value)
        sql += " ORDER BY created_at LIMIT ?"
        params.append(limit)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def list_stale_release_claims(self, *, decided_before: str) -> list[AccessRequest]:
        """Claims (``approved_pending`` + claim sentinel) older than ``decided_before``."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM access_requests WHERE status='approved_pending' AND release_candidate_id=? AND decided_at < ?",
                (RELEASE_CLAIM, decided_before),
            ) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def list_release_pending(
        self,
        *,
        team_id: str | None = None,
        resource_id: str | None = None,
        requester_id: str | None = None,
        limit: int = 500,
    ) -> list[AccessRequest]:
        """``approved_pending`` Agent requests waiting for a release to go live. Rows whose
        release is still being started (claim sentinel ``release_candidate_id=''``) are excluded."""
        sql = "SELECT * FROM access_requests WHERE status='approved_pending' AND COALESCE(release_candidate_id,'x') != ?"
        params: list[Any] = [RELEASE_CLAIM]
        for column, value in (("team_id", team_id), ("resource_id", resource_id), ("requester_id", requester_id)):
            if value is not None:
                sql += f" AND {column}=?"
                params.append(value)
        sql += " ORDER BY decided_at LIMIT ?"
        params.append(limit)
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def finish_release_pending(self, *, request_id: str, status: AccessRequestStatus) -> AccessRequest | None:
        """``approved_pending`` → ``approved`` / ``approve_failed`` exactly once; None if it already moved."""
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE access_requests SET status=?, updated_at=? WHERE id=? AND status='approved_pending' AND COALESCE(release_candidate_id,'x') != ?",
                (status, _now(), request_id, RELEASE_CLAIM),
            )
            await db.commit()
        return await self.get(request_id) if cur.rowcount == 1 else None

    async def finalize_release_claim(self, *, request_id: str, status: AccessRequestStatus, candidate_id: str | None) -> AccessRequest | None:
        """Claimed request (``approved_pending`` + claim sentinel) → its release outcome."""
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE access_requests SET status=?, release_candidate_id=?, updated_at=? WHERE id=? AND status='approved_pending' AND release_candidate_id=?",
                (status, candidate_id, _now(), request_id, RELEASE_CLAIM),
            )
            await db.commit()
        return await self.get(request_id) if cur.rowcount == 1 else None

    async def revert_release_claim(self, *, request_id: str) -> bool:
        """Undo a claim whose release could not be started: back to ``pending``."""
        async with open_shared_ctx(self.db_path) as db:
            cur = await db.execute(
                "UPDATE access_requests SET status='pending', decided_by=NULL, decided_at=NULL, decision_note=NULL, "
                "granted_permission=NULL, release_candidate_id=NULL, updated_at=? "
                "WHERE id=? AND status='approved_pending' AND release_candidate_id=?",
                (_now(), request_id, RELEASE_CLAIM),
            )
            await db.commit()
        return cur.rowcount == 1

    async def demote_to_shadow_for_resource(self, *, team_id: str, kind: str, resource_id: str) -> list[AccessRequest]:
        """Expansion: the resource is gone. Its real pending requests become shadow rows so the
        requester sees exactly what a missing resource shows (pending until the original
        ``expires_at``, then expired). Rows without an expiry (legacy) are left to the caller."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM access_requests WHERE team_id=? AND kind=? AND resource_id=? AND status='pending' AND shadow=0 AND expires_at IS NOT NULL",
                (team_id, kind, resource_id),
            ) as cur:
                rows = [_row(r) for r in await cur.fetchall()]
            if rows:
                await db.execute(
                    f"UPDATE access_requests SET shadow=1, updated_at=? WHERE id IN ({','.join('?' for _ in rows)}) AND status='pending'",  # noqa: S608 — placeholders
                    (_now(), *[r.id for r in rows]),
                )
                await db.commit()
        return rows

    async def mark_legacy_rows(self, *, now: str) -> int:
        """Sweep: legacy-flow rows keep their old semantics and never expire.

        Real rows without ``expires_at`` can only come from the legacy intake, so they are
        marked ``legacy_origin`` (covers rows written by an image predating that marker), and
        pending ones are marked escalated because the legacy flow already notified every admin.
        Returns the number of pending rows newly marked escalated."""
        async with open_shared_ctx(self.db_path) as db:
            await db.execute(
                "UPDATE access_requests SET legacy_origin=1 WHERE expires_at IS NULL AND shadow=0 AND legacy_origin=0",
            )
            cur = await db.execute(
                "UPDATE access_requests SET escalated_at=?, updated_at=? WHERE status='pending' AND legacy_origin=1 AND escalated_at IS NULL",
                (now, now),
            )
            await db.commit()
        return cur.rowcount

    async def pending_ids_for_owner(self, *, team_id: str, owner_id: str) -> list[str]:
        """Real pending requests routed to ``owner_id`` (digest bookkeeping)."""
        async with (
            open_shared_ctx(self.db_path) as db,
            db.execute(
                "SELECT id FROM access_requests WHERE team_id=? AND owner_id=? AND status='pending' AND shadow=0 ORDER BY created_at, rowid",
                (team_id, owner_id),
            ) as cur,
        ):
            rows = await cur.fetchall()
        return [str(r[0]) for r in rows]


def _row(row: aiosqlite.Row) -> AccessRequest:
    return AccessRequest(
        id=row["id"],
        team_id=row["team_id"],
        kind=row["kind"],
        resource_id=row["resource_id"],
        requester_id=row["requester_id"],
        owner_id=row["owner_id"],
        message=row["message"],
        status=row["status"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        decision_note=row["decision_note"],
        expires_at=row["expires_at"],
        requested_role=row["requested_role"],
        granted_permission=row["granted_permission"],
        withdrawn_at=row["withdrawn_at"],
        target=row["target"],
        shadow=bool(row["shadow"]),
        grant_expires_at=row["grant_expires_at"],
        cooldown_until=row["cooldown_until"],
        escalated_at=row["escalated_at"],
        release_candidate_id=row["release_candidate_id"],
        legacy_origin=bool(row["legacy_origin"]),
    )
