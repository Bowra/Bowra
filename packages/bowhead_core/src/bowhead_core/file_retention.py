"""Evaluate and apply the same Team file-retention rule at review and cleanup."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import aiosqlite

from bowhead_core import file_library
from bowhead_core._db import open_pooled_read_ctx, run_sqlite_authority_write
from bowhead_core.artifact_purge import SNAPSHOT_PIN_SQL
from bowhead_core.artifact_references import active_reference_sql, canvas_referenced_artifact_ids
from bowhead_core.artifact_store import ARTIFACT_WORKSPACE_PROVENANCE_SQL, TEAM_SCOPE_WORKSPACE_ID, _parse_iso_utc
from bowhead_core.sqlite_domains import domain_database_path, domain_layout_active
from bowhead_core.user_store import _read_team_role_in_tx

PROVENANCE = re.sub(r"\ba\.", "artifacts.", ARTIFACT_WORKSPACE_PROVENANCE_SQL)


class PolicyError(ValueError):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)


def deployment_days():
    try:
        return max(0, int(os.getenv("BOWHEAD_RUN_DATA_TTL_DAYS", "30")))
    except ValueError:
        return 30


async def admin(db, team, owner):
    role = await _read_team_role_in_tx(db, team_id=team, user_id=owner, require_active_principal=True)
    if role not in {"owner", "team_admin"}:
        raise PolicyError("file_retention_forbidden", 403)


async def policy(db, team):
    async with db.execute("SELECT version,days,effective_at FROM team_file_retention WHERE team_id=? ORDER BY version DESC LIMIT 1", (team,)) as cur:
        r = await cur.fetchone()
    version, days, since = r or (0, None, None)
    return {"version": version, "days": days, "effective_days": deployment_days() if days is None else days, "effective_at": since}


@dataclass(frozen=True)
class Decision:
    id: str
    size: int
    owner: str
    team: str | None
    reason: str
    version: int
    days: int


async def evaluate(db, kb, *, team=None, owner=None, ids=None, proposal=False, days=None, now=None, after="", limit=None, include_trash=False):
    now = now or datetime.now(UTC)
    db.row_factory = aiosqlite.Row
    refs = await active_reference_sql(db)
    conditions = ["artifacts.kind IN ('sandbox_output','tool_data','web_clip')", "artifacts.status='ready'", "artifacts.id>?"]
    args = [after]
    if not include_trash:
        conditions.append("artifacts.deleted_at IS NULL")
    if ids is not None:
        if not ids:
            return []
        conditions.append("artifacts.id IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(list(ids)))
    if team:
        conditions.append("w.team_id=?")
        args.append(team)
    if owner:
        conditions.append("artifacts.created_by=?")
        args.append(owner)
    sql = f"""SELECT artifacts.id,artifacts.size,artifacts.created_by,artifacts.created_at,artifacts.source,artifacts.canvas_id,
        COALESCE(artifacts.lineage_id,artifacts.id) AS root, w.team_id,t.status AS task,t.ended_at,
        artifacts.created_by_turn, producer.user_id AS producer_owner, origin.team_id AS producer_team,
        EXISTS(SELECT 1 FROM turns busy WHERE busy.session_id=COALESCE(t.session_id,b.session_id)
            AND busy.status IN ('running','pending','queued','paused','suspended','awaiting_user','waiting_for_user')) AS busy,
        EXISTS(SELECT 1 FROM turn_events e WHERE e.turn_id=t.id AND e.event_type='lead.final.payload'
              AND (e.generation={file_library.TURN_GENERATION} OR (e.generation IS NULL AND {file_library.TURN_GENERATION}=1))) AS has_final,
        st.kept, {refs} AS referenced,{SNAPSHOT_PIN_SQL} AS snapshot,
        artifacts.id IN (SELECT artifact_id FROM turn_attachments) AS sent,
        EXISTS(SELECT 1 FROM resource_grants g WHERE g.resource_kind='artifact' AND g.revoked_at IS NULL
            AND (g.expires_at IS NULL OR julianday(g.expires_at)>julianday('now')) AND
            (g.resource_id=artifacts.id OR g.resource_id=COALESCE(artifacts.lineage_id,artifacts.id) OR g.resource_id IN (
              SELECT sibling.id FROM artifacts sibling WHERE sibling.lineage_id=COALESCE(artifacts.lineage_id,artifacts.id)))) AS shared
        FROM artifacts LEFT JOIN turns t ON t.id=artifacts.created_by_turn
        LEFT JOIN artifact_upload_bindings b ON b.artifact_id=artifacts.id
        LEFT JOIN sessions producer ON producer.id=t.session_id
        LEFT JOIN workspaces origin ON origin.id=producer.workspace_id
        LEFT JOIN workspaces w ON w.id=CASE WHEN artifacts.workspace_id='{TEAM_SCOPE_WORKSPACE_ID}'
            THEN COALESCE(producer.workspace_id,CASE WHEN b.user_id=artifacts.created_by THEN b.workspace_id END)
            ELSE COALESCE(NULLIF(artifacts.workspace_id,''),{PROVENANCE}) END
        LEFT JOIN file_retention_state st ON st.artifact_id=artifacts.id
        WHERE {" AND ".join(conditions)} ORDER BY artifacts.id"""
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    async with db.execute(sql, args) as cur:
        rows = await cur.fetchall()
    if not rows:
        return []
    memberships = {}
    for tid, who in {(r["team_id"], r["created_by"]) for r in rows if r["team_id"] and r["created_by"]}:
        memberships[tid, who] = await _read_team_role_in_tx(db, team_id=tid, user_id=who, require_active_principal=True)
    canvas = await canvas_referenced_artifact_ids(db)
    in_kb = set()
    if kb:
        for start in range(0, len(rows), 500):
            in_kb.update(await kb.artifact_ids_in_kb([r["id"] for r in rows[start : start + 500]]))
    delivery = {}
    unknown_history = set()
    for who in {r["created_by"] for r in rows if r["created_by"]}:
        try:
            delivery[who] = await file_library.delivery_roots(db, who, await file_library.deliveries(db, who, for_retention=True))
        except file_library.UnverifiableDeliveryHistory:
            unknown_history.add(who)
    history = {}
    row_ids = {r["id"] for r in rows}
    async with db.execute("SELECT team_id,version,days,effective_at,existing_candidates FROM team_file_retention ORDER BY version") as cur:
        async for tid, ver, span, since, existing in cur:
            history.setdefault(tid, []).append((ver, span, since, row_ids.intersection(json.loads(existing))))
    result = []
    for r in rows:
        aid, who, tid = r["id"], r["created_by"], r["team_id"]
        rev, span = 0, deployment_days()
        for v, d, since, reviewed in history.get(tid, ()):
            period = deployment_days() if d is None else d
            if since <= r["created_at"] or aid in reviewed:
                rev, span = v, period
            elif period == 0 or (span and period > span):
                # Preserve relief granted to older files until explicitly reviewed.
                span = period
        if proposal:
            span = deployment_days() if days is None else days
        clock = _parse_iso_utc(r["ended_at"] or "") if r["task"] == "completed" else None
        if not clock and rev == 0 and not proposal and not r["created_by_turn"]:
            clock = _parse_iso_utc(r["created_at"])
        holds = (
            (
                not memberships.get((tid, who)) or bool(r["created_by_turn"] and (r["producer_owner"] != who or r["producer_team"] != tid)),
                "unknown_owner",
            ),
            (r["kept"], "kept"),
            (r["source"] in ("kb_upload", "kb_bundle_upload") or aid in in_kb, "knowledge"),
            (not kb or who in unknown_history or (r["task"] == "completed" and not r["has_final"]), "unknown_reference"),
            (r["root"] in delivery.get(who, {}), "delivered"),
            (r["shared"], "shared"),
            (r["sent"], "attachment"),
            (r["snapshot"], "export_snapshot"),
            (r["referenced"], "reference"),
            (r["canvas_id"] or aid in canvas, "report"),
            (
                r["busy"] or r["task"] in {"running", "suspended", "paused", "queued", "pending", "waiting", "awaiting_user", "waiting_for_user"},
                "active_task",
            ),
            (not clock, "unknown_age"),
            (span == 0, "disabled"),
            (clock and clock > now - timedelta(days=min(span, 365000)), "within_period"),
        )
        reason = next((label for value, label in holds if value), "eligible")
        result.append(Decision(aid, r["size"], who, tid, reason, rev, span))
    return result


def totals(decisions):
    counts, sizes = Counter(), Counter()
    for row in decisions:
        counts[row.reason] += 1
        sizes[row.reason] += row.size
    return {key: {"count": counts[key], "bytes": sizes[key]} for key in counts}


async def review(path, kb, team, owner, days, version):
    core = domain_database_path(path, "core")
    async with open_pooled_read_ctx(core) as db:
        await db.execute("BEGIN")
        await admin(db, team, owner)
        if (await policy(db, team))["version"] != version:
            raise PolicyError("file_retention_conflict", 409)
        decisions = await evaluate(db, kb, team=team, proposal=True, days=days)
    candidates = [r.id for r in decisions if r.reason == "eligible"]
    token = uuid4().hex
    now = datetime.now(UTC)
    effective = deployment_days() if days is None else days

    async def save(db):
        await admin(db, team, owner)
        if (await policy(db, team))["version"] != version:
            raise PolicyError("file_retention_conflict", 409)
        await db.execute(
            "DELETE FROM file_retention_reviews WHERE applied_version IS NULL AND created_at<?", ((now - timedelta(hours=1)).isoformat(),)
        )
        await db.execute(
            """INSERT INTO file_retention_reviews(id,team_id,user_id,version,days,effective_days,created_at,candidates)
            VALUES(?,?,?,?,?,?,?,?)""",
            (token, team, owner, version, days, effective, now.isoformat(), json.dumps(candidates)),
        )

    await run_sqlite_authority_write(core, save)
    return {"review_id": token, "impact": totals(decisions), "effective_days": effective}


async def apply(path, team, owner, token, historical):
    async def save(db):
        await admin(db, team, owner)
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM file_retention_reviews WHERE id=? AND team_id=? AND user_id=?", (token, team, owner)) as cur:
            r = await cur.fetchone()
        if not r:
            raise PolicyError("file_retention_review_missing", 404)
        current = await policy(db, team)
        if r["applied_version"] is not None:
            if current["version"] != r["applied_version"] or bool(r["applied_existing"]) != historical:
                raise PolicyError("file_retention_review_stale", 409)
            return current
        if (
            current["version"] != r["version"]
            or (r["days"] is None and r["effective_days"] != deployment_days())
            or _parse_iso_utc(r["created_at"]) < datetime.now(UTC) - timedelta(hours=1)
        ):
            raise PolicyError("file_retention_review_stale", 409)
        ver = current["version"] + 1
        await db.execute(
            "INSERT INTO team_file_retention(team_id,version,days,effective_at,existing_candidates) VALUES(?,?,?,?,?)",
            (team, ver, r["days"], datetime.now(UTC).isoformat(), r["candidates"] if historical else "[]"),
        )
        await db.execute(
            "UPDATE file_retention_reviews SET applied_version=?,applied_existing=?,candidates='[]' WHERE id=?", (ver, historical, token)
        )
        return await policy(db, team)

    return await run_sqlite_authority_write(domain_database_path(path, "core"), save)


async def collect(store, kb):
    """Short transactions, stable ID scan and an independent recheck before trashing."""
    if kb is None:
        return {"files": 0, "bytes": 0}
    after = ""
    count = size = 0
    while True:
        async with open_pooled_read_ctx(store.db_path) as db:
            await db.execute("BEGIN")
            batch = await evaluate(db, kb, after=after, limit=500)
        if not batch:
            break
        after = batch[-1].id
        ids = [d.id for d in batch if d.reason == "eligible"]
        if not ids:
            continue

        async def move(db, candidates=tuple(ids)):
            if domain_layout_active(store.db_path):
                await db.execute("SELECT bowhead_domain_authority_v1('artifacts')")
            current = await evaluate(db, kb, ids=candidates)
            stamp = datetime.now(UTC).isoformat()
            moved = []
            for d in current:
                if d.reason != "eligible":
                    continue
                cur = await db.execute("UPDATE artifacts SET deleted_at=? WHERE id=? AND deleted_at IS NULL AND created_by=?", (stamp, d.id, d.owner))
                if cur.rowcount:
                    await db.execute(
                        """INSERT INTO file_retention_state(artifact_id,trashed_at,policy_version) VALUES(?,?,?)
                        ON CONFLICT(artifact_id) DO UPDATE SET trashed_at=excluded.trashed_at,policy_version=excluded.policy_version""",
                        (d.id, stamp, d.version),
                    )
                    moved.append(d)
            if moved and await kb.artifact_ids_in_kb([d.id for d in moved]):
                raise PolicyError("file_retention_reference_changed", 409)
            return len(moved), sum(d.size for d in moved)

        n, b = await run_sqlite_authority_write(store.db_path, move)
        count += n
        size += b
    return {"files": count, "bytes": size}


async def auto_purge_allowed(db, path, aid):
    """Re-evaluate automatic trash only. Explicit deletion uses its existing guards."""
    from bowhead_core.kb.files_store import KbFilesStore
    from bowhead_core.sqlite_readable_schema import readable_table_exists

    if not await readable_table_exists(db, "file_retention_state"):
        return True
    async with db.execute(
        "SELECT 1 FROM file_retention_state st JOIN artifacts a ON a.id=st.artifact_id WHERE a.id=? AND st.trashed_at=a.deleted_at", (aid,)
    ) as cur:
        if not await cur.fetchone():
            return True
    if not file_library.enabled():
        return False
    kb = KbFilesStore(domain_database_path(path, "files"))
    rows = await evaluate(db, kb, ids=[aid], include_trash=True)
    return len(rows) == 1 and rows[0].reason == "eligible"


async def legacy_ttl_allowed(store):
    """An operator rollback pauses managed cleanup instead of bypassing saved decisions."""
    from bowhead_core.sqlite_readable_schema import readable_table_exists

    async with open_pooled_read_ctx(store.db_path) as db:
        if not await readable_table_exists(db, "file_retention_state"):
            return True
        async with db.execute("SELECT EXISTS(SELECT 1 FROM file_retention_state) OR EXISTS(SELECT 1 FROM team_file_retention)") as cur:
            return not (await cur.fetchone())[0]
