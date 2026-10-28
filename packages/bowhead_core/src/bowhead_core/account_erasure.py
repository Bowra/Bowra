"""Reviewed ownership closure and exact-row account erasure.

The catalog is deliberately closed: an unreviewed table or column blocks erasure
instead of silently retaining personal data or guessing that a creator owns a
Team resource. Source/provenance links never imply ownership of an independent
copy. Foreign-key checks remain enabled through the final atomic transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiosqlite

from bowhead_core.account_deletion import AccountDeletionError

_POLICY = json.loads(Path(__file__).with_name("account_erasure_policy.json").read_text())
_INFRASTRUCTURE = {
    "alembic_version",
    "_phase0_probe",
    "account_deletions",
    "account_deletion_rows",
    "account_deletion_objects",
    "account_protocol_erasure_receipts",
}
# These fields identify a containing object. `source_*`, `derived_from`,
# `parent_turn_id` and creator fields intentionally do not propagate ownership.
_PARENTS = {
    "session_id": ("sessions", "id"),
    "scope_session_id": ("sessions", "id"),
    "turn_id": ("turns", "id"),
    "created_by_turn": ("turns", "id"),
    "canvas_id": ("canvases", "id"),
    "artifact_id": ("artifacts", "id"),
    "bundle_artifact_id": ("artifacts", "id"),
    "plan_id": ("plans", "id"),
    "job_id": ("document_jobs", "id"),
    "candidate_id": ("turn_terminal_candidates", "candidate_id"),
    "receipt_id": ("turn_terminal_receipts", "receipt_id"),
    "decision_id": ("turn_control_decisions", "decision_id"),
    "request_id": ("clarification_requests", "request_id"),
    "card_id": ("clarification_requests", "request_id"),
    "ledger_id": ("credit_ledger", "id"),
    "execution_id": ("turn_agent_execution", "id"),
    "subagent_id": ("team_subagent", "id"),
    "agent_id": ("team_subagent", "id"),
}
_ROOTS = {
    "admin_audit_log": "actor_user_id=? OR (target_kind='user' AND target_id=?)",
    "invite_tokens": "consumed_by_user_id=?",
    "team_invite_tokens": "consumed_by_user_id=?",
    "memory_entries": "subject_type='user' AND subject_id=?",
    "memory_notes": "subject_type='user' AND subject_id=?",
    "resource_grants": "principal_type='user' AND principal_id=?",
    "group_memberships": "principal_type='user' AND principal_id=?",
    "access_requests": "requester_id=?",
    "team_subagent": """(CASE WHEN EXISTS(SELECT 1 FROM team_subagent_schema_meta WHERE singleton=1 AND owner_semantics_activated_at IS NOT NULL)
        THEN owner_user_id ELSE created_by END)=?
        AND (CASE WHEN EXISTS(SELECT 1 FROM team_subagent_schema_meta WHERE singleton=1 AND audience_semantics_activated_at IS NOT NULL)
          THEN audience_mode WHEN visibility='team_wide' THEN 'teamwide'
          WHEN EXISTS(SELECT 1 FROM resource_grants g WHERE g.team_id=team_subagent.team_id AND g.resource_kind='team_subagent'
            AND g.resource_id=team_subagent.id AND g.effect='allow' AND g.revoked_at IS NULL
            AND g.principal_type IN ('user','group') AND g.permission IN ('use','write','admin')
            AND (g.expires_at IS NULL OR g.expires_at>strftime('%Y-%m-%dT%H:%M:%f+00:00','now')))
          THEN 'custom' ELSE 'private' END)='private'""",
    "team_skill": "created_by=? AND visibility IN ('creator_only','private')",
    "business_rules": "user_id=?",
    "registered_facts": "user_id=?",
    "agent_command_receipt": "actor_user_id=?",
    "resource_change_receipt": "actor_user_id=?",
    "resource_refresh_candidate": "actor_user_id=?",
    "resource_file_cleanup_receipt": "actor_user_id=?",
    "notification_items": "actor_user_id=? OR id IN (SELECT notification_id FROM notification_recipients GROUP BY notification_id HAVING SUM(user_id<>?)=0)",
    "turn_effect_evidence_links": "evidence_link_id IN (SELECT e.evidence_link_id FROM turn_effect_settlements e JOIN turn_effect_attempts a ON a.attempt_id=e.attempt_id JOIN turns t ON t.id=a.turn_id JOIN sessions s ON s.id=t.session_id WHERE s.user_id=?)",
}


def _q(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError("invalid reviewed SQL identifier")
    return f'"{value}"'


def key_sql(primary: list[str], *, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return "json_array(" + ",".join(prefix + _q(column) for column in primary) + ")"


async def reviewed_schema(db: aiosqlite.Connection) -> dict[str, list[str]]:
    async with db.execute("SELECT name FROM sqlite_schema WHERE type='table'") as cur:
        names = {row[0] for row in await cur.fetchall()}
    unknown = names - set(_POLICY["tables"]) - _INFRASTRUCTURE
    unknown = {name for name in unknown if not name.startswith(("sqlite_", "canvas_fts_"))}
    if unknown:
        raise AccountDeletionError("account_erasure_unreviewed_table")
    primary: dict[str, list[str]] = {}
    for table, policy in _POLICY["tables"].items():
        if table not in names:
            raise AccountDeletionError("account_erasure_schema_incomplete")
        async with db.execute(f"PRAGMA table_info({_q(table)})") as cur:
            info = await cur.fetchall()
        if [row[1] for row in info] != policy["columns"]:
            raise AccountDeletionError("account_erasure_unreviewed_column")
        keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        # FTS logical row IDs survive database VACUUM; ordinary rowid does not.
        if table == "canvas_fts":
            keys = ["rowid"]
        if keys:
            primary[table] = keys
    return primary


async def freeze_rows(db: aiosqlite.Connection, operation_id: str, user_id: str) -> dict[str, list[str]]:
    """Freeze ownership to exact primary keys under the caller's writer lock."""
    primary = await reviewed_schema(db)

    for table, keys in primary.items():
        if table == "users":
            continue
        policy = _POLICY["tables"][table]
        predicates = [f"{_q(column)}=?" for column in policy["owner_columns"]]
        if table in _ROOTS:
            predicates.append(_ROOTS[table])
        if not predicates:
            continue
        predicate = " OR ".join(f"({part})" for part in predicates)
        if table == "artifacts":
            predicate = f"({predicate}) AND scope!='team' AND id NOT IN (SELECT artifact_id FROM team_brand_asset_metadata)"
        await db.execute(
            f"INSERT OR IGNORE INTO account_deletion_rows SELECT ?,?,{key_sql(keys)} FROM {_q(table)} WHERE {predicate}",
            (operation_id, table, *(user_id for _ in range(predicate.count("?")))),
        )
    # Follow explicit containment and actual FK tuples to a fixed point. Never
    # follow users/Teams/Spaces: creator and membership are not resource ownership.
    changed = True
    while changed:
        changed = False
        for table, keys in primary.items():
            if table in {"users", "teams", "workspaces"}:
                continue
            columns = _POLICY["tables"][table]["columns"]
            edges: list[tuple[str, list[tuple[str, str]]]] = []
            async with db.execute(f"PRAGMA foreign_key_list({_q(table)})") as cur:
                grouped: dict[int, list[Any]] = defaultdict(list)
                for row in await cur.fetchall():
                    grouped[row[0]].append(row)
            for rows in grouped.values():
                parent = rows[0][2]
                if parent in primary and parent not in {"users", "teams", "workspaces"} and all(row[4] for row in rows):
                    edges.append((parent, [(row[3], row[4]) for row in rows]))
            for column, (parent, parent_column) in _PARENTS.items():
                if column in columns and parent in primary and parent != table:
                    if table == "canvas_sections" and column == "turn_id":
                        continue  # A copied section retains provenance, not ownership.
                    # `job_id` is also used by schedule runs, so bind it separately.
                    if column == "job_id" and table.startswith("scheduled_"):
                        parent = "scheduled_jobs"
                    edges.append((parent, [(column, parent_column)]))
            if table == "notification_items":
                for parent in ("sessions", "turns", "artifacts", "canvases"):
                    edges.append((parent, [("subject_id", "id")]))
            if table in {"resource_grants", "catalog_localized_listing"}:
                for parent in ("artifacts", "canvases", "sessions", "team_subagent", "team_skill"):
                    edges.append((parent, [("resource_id", "id")]))
            for parent, joins in edges:
                join = " AND ".join(f"child.{_q(child)}=parent.{_q(target)}" for child, target in joins)
                parent_key = " AND ".join(
                    f"parent.{_q(column)} IS json_extract(selected.row_key,'$[{index}]')" for index, column in enumerate(primary[parent])
                )
                source = f"account_deletion_rows selected JOIN {_q(parent)} parent ON {parent_key} JOIN {_q(table)} child ON {join}"
                predicate = f"selected.operation_id=? AND selected.table_name='{parent}'"
                if table in {"resource_grants", "catalog_localized_listing"} and joins == [("resource_id", "id")]:
                    kind = {
                        "artifacts": "artifact",
                        "canvases": "canvas",
                        "sessions": "report",
                        "team_subagent": "team_subagent",
                        "team_skill": "team_skill",
                    }[parent]
                    predicate += f" AND child.resource_kind='{kind}'"
                if table == "artifacts":
                    predicate += " AND child.scope!='team' AND child.id NOT IN (SELECT artifact_id FROM team_brand_asset_metadata)"
                    predicate += " AND (child.created_by IS NULL OR child.created_by=(SELECT user_id FROM account_deletions WHERE operation_id=selected.operation_id))"
                cur = await db.execute(
                    f"INSERT OR IGNORE INTO account_deletion_rows SELECT ?,?,{key_sql(keys, alias='child')} FROM {source} WHERE {predicate}",
                    (operation_id, table, operation_id),
                )
                changed |= cur.rowcount > 0
    return primary


async def protocol_receipts(db: aiosqlite.Connection, user_id: str, *, secret: str, erased_at: str) -> None:
    """Retain actual outcomes, including unknown, without payload or credentials.

    This erasure path never fabricates a settlement or enables execution. The
    original generation is fenced before erasure; callbacks on removed scopes
    fail closed. Receipts remain separate from both the account and Turn graph.
    """
    if not secret:
        raise AccountDeletionError("account_erasure_key_unavailable")
    async with db.execute(
        """SELECT w.team_id,e.attempt_id,e.state,e.result_digest
           FROM turn_effect_attempts e JOIN turns t ON t.id=e.turn_id
           JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id WHERE s.user_id=?""",
        (user_id,),
    ) as cur:
        effects = await cur.fetchall()
    async with db.execute(
        """SELECT w.team_id,g.turn_id||':'||g.generation,g.state,g.usage_digest
           FROM turn_generations g JOIN turns t ON t.id=g.turn_id
           JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id WHERE s.user_id=?""",
        (user_id,),
    ) as cur:
        generations = await cur.fetchall()
    for kind, rows in (("effect", effects), ("generation", generations)):
        for team_id, identity, state, outcome in rows:
            digest = hmac.new(secret.encode(), json.dumps(["account-erasure-v1", team_id, kind, identity]).encode(), hashlib.sha256).hexdigest()
            await db.execute(
                "INSERT OR IGNORE INTO account_protocol_erasure_receipts VALUES (?,?,?,?,?,?)",
                (digest, team_id, kind, state, outcome, erased_at),
            )


async def erase_rows(db: aiosqlite.Connection, operation_id: str, primary: dict[str, list[str]]) -> None:
    """Only the due-account transaction can exercise exact-row guard exceptions."""
    await db.execute("PRAGMA defer_foreign_keys=ON")
    for table, keys in primary.items():
        if table in {"users", "admin_audit_log"}:
            continue
        columns = ",".join(_q(column) for column in keys)
        values = ",".join(f"json_extract(row_key,'$[{index}]')" for index in range(len(keys)))
        await db.execute(
            f"DELETE FROM {_q(table)} WHERE ({columns}) IN (SELECT {values} FROM account_deletion_rows WHERE operation_id=? AND table_name=?)",
            (operation_id, table),
        )
