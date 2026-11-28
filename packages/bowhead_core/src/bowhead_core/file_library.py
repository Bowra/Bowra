"""Owner-scoped Files metadata and persisted delivery evidence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import timedelta

import aiosqlite
from pydantic import ValidationError

from bowhead_core.artifact_presentation import PRESENTATION_EVENT_SQL, PresentationContractUnsupported, project_declaration
from bowhead_core.artifact_store import Artifact, _parse_iso_utc, _row_to_artifact
from bowhead_core.results_artifact_policy import results_internal_artifact_sql

TURN_GENERATION = "CASE WHEN t.terminal_protocol_version=1 THEN t.current_generation ELSE COALESCE(NULLIF(t.execution_generation,0),1) END"
TRASH_DAYS = 30
RUNTIME = frozenset({"tool_data", "sandbox_output", "web_clip"})
FILE_KINDS = (*sorted(RUNTIME), "deliverable", "user_upload", "canvas_export")


def enabled() -> bool:
    return os.getenv("FEATURE_FILE_LIBRARY", "").strip().lower() in {"1", "true", "yes", "on"}


def recover_until(stamp: str | None) -> str | None:
    deleted = _parse_iso_utc(stamp or "")
    return (deleted + timedelta(days=TRASH_DAYS)).isoformat() if deleted else None


@dataclass(frozen=True)
class Delivery:
    artifact_id: str
    root: str
    session_id: str
    workspace_id: str
    title: str | None
    delivered_at: str
    pruned: bool = False


class UnverifiableDeliveryHistory(ValueError):
    """A saved delivery cannot be interpreted by this reader."""


async def deliveries(db: aiosqlite.Connection, owner: str, *, for_retention: bool = False) -> list[Delivery]:
    """Read only authoritative file cards, native presentations and promoted document outputs."""
    result: list[Delivery] = []
    source = """FROM sessions s JOIN turns t ON t.session_id=s.id
        JOIN workspaces w ON w.id=s.workspace_id
        JOIN turn_events e ON e.turn_id=t.id WHERE s.user_id=? AND t.status='completed'"""
    if not for_retention:
        source += " AND s.deleted_at IS NULL"
    select = f"SELECT s.id,s.workspace_id,s.title,e.occurred_at,e.payload_json,t.id,{TURN_GENERATION},w.team_id "
    finals = f""" AND e.event_type='lead.final.payload' AND e.seq=(
        SELECT x.seq FROM turn_events x WHERE x.turn_id=t.id AND x.event_type='lead.final.payload'
        AND (x.generation={TURN_GENERATION} OR (x.generation IS NULL AND {TURN_GENERATION}=1))
        ORDER BY x.seq DESC LIMIT 1)"""
    async with db.execute(select + source + finals, (owner,)) as cur:
        async for sid, ws, title, stamp, raw, _, _, _ in cur:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                if for_retention:
                    raise UnverifiableDeliveryHistory("Delivery history cannot be verified") from None
                continue
            cards = payload.get("inline_artifacts") if isinstance(payload, dict) else None
            if for_retention and (not isinstance(payload, dict) or (cards is not None and not isinstance(cards, list))):
                raise UnverifiableDeliveryHistory("Invalid final file cards")
            for card in cards if isinstance(cards, list) else ():
                if not isinstance(card, dict) or card.get("kind") not in ("file", "image"):
                    continue
                aid, root = card.get("id"), card.get("lineage_id")
                if isinstance(aid, str) and aid.startswith("art_"):
                    result.append(Delivery(aid, root if isinstance(root, str) else "", sid, ws, title, stamp))
    async with db.execute(
        select + source + f" AND (e.generation IS NULL OR e.generation={TURN_GENERATION}) AND " + PRESENTATION_EVENT_SQL, (owner,)
    ) as cur:
        async for sid, ws, title, stamp, raw, turn, generation, team in cur:
            data = json.loads(raw)
            if data.get("ok") is not True or data.get("tool_name") != "present_artifacts":
                continue
            meta = data.get("result_meta") or {}
            receipt = meta.get("artifact_presentation_receipt") or {}
            if not isinstance(receipt, dict) or (
                receipt.get("user_id"),
                receipt.get("workspace_id"),
                receipt.get("session_id"),
                receipt.get("turn_id"),
            ) != (owner, ws, sid, turn):
                continue
            if receipt.get("generation") != generation or receipt.get("team_id") != team or receipt.get("tool_name") != "present_artifacts":
                continue
            try:
                declaration = project_declaration(meta)
            except (ValidationError, PresentationContractUnsupported):
                if for_retention:
                    raise UnverifiableDeliveryHistory("Unrecognized presentation receipt") from None
                continue
            if declaration.origin == "explicit_tool":
                result.extend(Delivery(item.artifact_id, "", sid, ws, title, stamp) for item in declaration.items)
    async with db.execute(
        """SELECT a.id,s.id,j.workspace_id,s.title,c.created_at FROM document_confirmations c
        JOIN document_jobs j ON j.id=c.job_id AND j.requested_by=c.user_id AND j.team_id=c.team_id AND j.workspace_id=c.workspace_id
        JOIN document_executions e ON e.job_id=j.id AND e.state='published' AND e.draft_digest=c.draft_digest
        JOIN document_draft_files f ON f.job_id=j.id JOIN artifacts a ON a.id=f.artifact_id
        JOIN sessions s ON s.id=j.session_id AND s.user_id=j.requested_by AND s.deleted_at IS NULL
        WHERE j.requested_by=? AND a.created_by=j.requested_by AND a.producer_kind='template_document_v1'
        AND a.kind='deliverable' AND a.status='ready' AND json_valid(e.draft_json)
        AND (f.name=COALESCE(json_extract(e.draft_json,'$.descriptor.source_name'),'candidate.pptx')
        OR (f.name='candidate.pdf' AND (json_extract(e.draft_json,'$.descriptor') IS NULL
        OR EXISTS(SELECT 1 FROM json_each(e.draft_json,'$.descriptor.published_outputs') WHERE value='pdf'))))""",
        (owner,),
    ) as cur:
        async for aid, sid, ws, title, stamp in cur:
            result.append(Delivery(aid, "", sid, ws, title, stamp))
    return result


@dataclass
class LibraryVersion:
    art: Artifact
    session_id: str | None
    session_title: str | None
    origin_space: str | None
    sent_spaces: list[str]
    kept: bool
    pinned: bool


async def versions(db: aiosqlite.Connection, owner: str, *, roots: set[str] | None = None, trash: bool | None = None) -> list[LibraryVersion]:
    internal, params = results_internal_artifact_sql("a")
    where = [
        "a.created_by=?",
        "a.status='ready'",
        "a.kind IN (SELECT value FROM json_each(?))",
        f"NOT {internal}",
        "NOT EXISTS(SELECT 1 FROM artifact_purge_claims p WHERE p.artifact_id=a.id)",
    ]
    args = [owner, json.dumps(FILE_KINDS), *params]
    if roots is not None:
        if not roots:
            return []
        where.append("(a.id IN (SELECT value FROM json_each(?)) OR a.lineage_id IN (SELECT value FROM json_each(?)))")
        args.extend([json.dumps(sorted(roots))] * 2)
    if trash is not None:
        where.append("a.deleted_at IS NOT NULL" if trash else "a.deleted_at IS NULL")
    db.row_factory = aiosqlite.Row
    async with db.execute(
        f"""SELECT a.*,s.id AS origin_session,s.title AS origin_title,s.workspace_id AS origin_space,
        (SELECT json_group_array(DISTINCT ss.workspace_id) FROM turn_attachments ta
            JOIN turns sent_turn ON sent_turn.id=ta.turn_id JOIN sessions ss ON ss.id=sent_turn.session_id
            WHERE ta.artifact_id=a.id AND ss.user_id=a.created_by AND ss.deleted_at IS NULL AND ss.workspace_id IS NOT NULL) AS sent_spaces,
        COALESCE(r.kept,0) AS kept,EXISTS(SELECT 1 FROM user_result_pins rp WHERE rp.user_id=a.created_by
            AND rp.target_kind='file' AND rp.target_id=COALESCE(a.lineage_id,a.id)) AS pinned
        FROM artifacts a LEFT JOIN file_retention_state r ON r.artifact_id=a.id
        LEFT JOIN turns t ON t.id=a.created_by_turn LEFT JOIN canvases c ON c.id=a.canvas_id
        LEFT JOIN artifact_upload_bindings b ON b.artifact_id=a.id
        LEFT JOIN sessions s ON s.id=COALESCE(t.session_id,c.session_id,NULLIF(b.session_id,''),
            (SELECT sa.session_id FROM session_attachments sa WHERE sa.artifact_id=a.id ORDER BY sa.pinned_at LIMIT 1))
            AND s.user_id=a.created_by AND s.deleted_at IS NULL
        WHERE {" AND ".join(where)}""",
        args,
    ) as cur:
        return [
            LibraryVersion(
                _row_to_artifact(row),
                row["origin_session"],
                row["origin_title"],
                row["origin_space"],
                json.loads(row["sent_spaces"]),
                bool(row["kept"]),
                bool(row["pinned"]),
            )
            for row in await cur.fetchall()
        ]


async def delivery_roots(db, owner, occurrences):
    """Resolve stored references in one query, retaining trusted roots of pruned versions."""
    ids = json.dumps([item.artifact_id for item in occurrences])
    async with db.execute("SELECT id,COALESCE(lineage_id,id),created_by FROM artifacts WHERE id IN (SELECT value FROM json_each(?))", (ids,)) as cur:
        originals = {aid: (root, creator) for aid, root, creator in await cur.fetchall()}
    async with db.execute(
        "SELECT artifact_id FROM artifact_purge_claims WHERE created_by=? AND completed_at IS NOT NULL AND artifact_id IN (SELECT value FROM json_each(?))",
        (owner, ids),
    ) as cur:
        purged = {row[0] for row in await cur.fetchall()}
    result = {}
    for item in occurrences:
        if item.artifact_id in originals:
            root, creator = originals[item.artifact_id]
            if creator != owner:
                continue
        elif item.artifact_id in purged:
            root = item.root or item.artifact_id
            item = replace(item, pruned=True)
        else:
            continue
        result.setdefault(root, []).append(item)
    return result
