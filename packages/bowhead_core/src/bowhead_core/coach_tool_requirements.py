"""Private Tool requirements and observations; never a Tool installer or grant.

A handoff retains its original parent and intent. An onboarding operation records
only a request to open the existing authenticated workflow. Observations identify
an actual selectable Tool; they do not prove who created it or a business trial.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope

Authorize = Callable[[aiosqlite.Connection, dict | None, bool], Awaitable[dict]]
Observe = Callable[[aiosqlite.Connection, dict], Awaitable[None]]


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CoachToolRequirements:
    def __init__(self, db_path: str, scope: ReferenceRequestScope, authorize: Authorize) -> None:
        self.db_path, self.scope, self.authorize = db_path, scope, authorize

    async def _get(self, db: aiosqlite.Connection, requirement_id: str) -> dict:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM coach_tool_requirement WHERE id=? AND team_id=? AND user_id=? AND workspace_id=?",
            (requirement_id, self.scope.team_id, self.scope.user_id, self.scope.workspace_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("tool_requirement_unavailable")
        return dict(row)

    @staticmethod
    def receipt(row: dict, parent_status: dict) -> dict:
        return {
            "ok": True,
            "requirement_id": row["id"],
            "original_parent": json.loads(row["parent_json"]),
            "original_parent_context": json.loads(row["parent_context_json"]),
            "parent_status": parent_status,
            "requirement": json.loads(row["requirement_json"]),
            "onboarding_operation_id": row["onboarding_operation_id"],
            "onboarding_state": "view_requested" if row["onboarding_operation_id"] else "not_requested",
            "resource_created": False,
            "parent_bound": False,
            "business_trial": "not_run",
        }

    async def create(self, *, parent: dict | None, requirement: dict, request_id: str, turn_id: str) -> dict:
        request_hash = _hash({"parent": parent, "requirement": requirement})
        if len(_json(requirement).encode()) > 64 * 1024:
            raise ValueError("tool_requirement_too_large")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            parent_status = await self.authorize(db, parent, False)
            cursor = await db.execute(
                "SELECT * FROM coach_tool_requirement WHERE team_id=? AND user_id=? AND origin_turn_id=? AND key_hash=?",
                (self.scope.team_id, self.scope.user_id, turn_id, _hash(request_id)),
            )
            existing = await cursor.fetchone()
            if existing:
                if existing["request_hash"] != request_hash or existing["workspace_id"] != self.scope.workspace_id:
                    raise ValueError("tool_requirement_idempotency_conflict")
                return self.receipt(dict(existing), parent_status)
            parent_status = await self.authorize(db, parent, True)
            identity = "ctrq_" + uuid4().hex
            await db.execute(
                """INSERT INTO coach_tool_requirement(id,team_id,user_id,workspace_id,origin_session_id,origin_turn_id,
                   key_hash,request_hash,parent_json,parent_context_json,requirement_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity,
                    self.scope.team_id,
                    self.scope.user_id,
                    self.scope.workspace_id,
                    self.scope.session_id or "",
                    turn_id,
                    _hash(request_id),
                    request_hash,
                    _json(parent),
                    _json(parent_status),
                    _json(requirement),
                    _now(),
                ),
            )
            await db.commit()
            return self.receipt(await self._get(db, identity), parent_status)

    async def get(self, *, requirement_id: str, observation_id: str | None = None) -> dict:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN")
            row = await self._get(db, requirement_id)
            parent_status = await self.authorize(db, json.loads(row["parent_json"]), False)
            result = self.receipt(row, parent_status)
            if observation_id is not None:
                cursor = await db.execute("SELECT * FROM coach_tool_observation WHERE id=? AND requirement_id=?", (observation_id, requirement_id))
                observation = await cursor.fetchone()
                if observation is None:
                    raise ValueError("tool_observation_unavailable")
                result["observation"] = dict(observation)
            return result

    async def request_open(self, *, requirement_id: str, authorize_open: Callable[[aiosqlite.Connection], Awaitable[None]]) -> dict:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._get(db, requirement_id)
            parent_status = await self.authorize(db, json.loads(row["parent_json"]), False)
            requirement = json.loads(row["requirement_json"])
            if requirement["workflow"] == "engineering":
                raise ValueError("tool_engineering_delivery_required")
            await authorize_open(db)
            if not row["onboarding_operation_id"]:
                await db.execute(
                    "UPDATE coach_tool_requirement SET onboarding_operation_id=?,onboarding_requested_at=? WHERE id=?",
                    ("ctop_" + uuid4().hex, _now(), requirement_id),
                )
            await db.commit()
            return self.receipt(await self._get(db, requirement_id), parent_status)

    async def observe(self, *, requirement_id: str, reference: dict, request_id: str, turn_id: str, verify: Observe) -> dict:
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._get(db, requirement_id)
            parent_status = await self.authorize(db, json.loads(row["parent_json"]), False)
            key_hash = _hash([turn_id, request_id])
            cursor = await db.execute(
                "SELECT * FROM coach_tool_observation WHERE requirement_id=? AND request_key_hash=?", (requirement_id, key_hash)
            )
            existing = await cursor.fetchone()
            if existing:
                if existing["reference_json"] != _json(reference):
                    raise ValueError("tool_observation_idempotency_conflict")
                result = self.receipt(row, parent_status)
                result["observation"] = dict(existing)
                return result
            await verify(db, reference)
            identity, observed_at = "ctro_" + uuid4().hex, _now()
            await db.execute(
                "INSERT INTO coach_tool_observation(id,requirement_id,request_key_hash,reference_json,observed_at) VALUES(?,?,?,?,?)",
                (identity, requirement_id, key_hash, _json(reference), observed_at),
            )
            await db.commit()
            result = self.receipt(row, parent_status)
            result["observation"] = {"id": identity, "requirement_id": requirement_id, "reference_json": _json(reference), "observed_at": observed_at}
            return result
