"""M3-8 durable exact Capability implementation attribution.

The compiler decides which reviewed implementation is eligible.  This store
fixes that decision under an already-admitted Agent execution before the
implementation can cause an effect.  It persists identifiers, fingerprints,
and Artifact references only; prompt contents, credentials, Tool arguments,
and business payloads stay out of this ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx

if TYPE_CHECKING:
    from bowhead_core.agent_capability_composition import ImplementationExecutionPlan

CapabilityExecutionStatus = Literal[
    "selected",
    "running",
    "completed",
    "failed",
    "blocked",
    "cancelled",
]
CapabilityTerminalStatus = Literal["completed", "failed", "blocked", "cancelled"]

_EXACT_REF_RE = re.compile(r"^([a-z][a-z0-9_.-]{1,127})@([1-9][0-9]*)$")
_OUTPUT_CONTRACT_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}\.v[1-9][0-9]*$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_EMPTY_PROMPT_FINGERPRINT = f"sha256:{hashlib.sha256(b'').hexdigest()}"


class CapabilityExecutionError(RuntimeError):
    """Finite, consumer-safe Capability execution failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CapabilityExecutionSelection:
    composition_node_id: str
    attempt_generation: int
    capability_id: str
    capability_version: int
    implementation_id: str
    implementation_version: int
    adapter_id: str | None
    prompt_module_fingerprint: str
    dependency_fingerprint: str
    input_artifact_refs: tuple[str, ...]
    output_contract: str

    def __post_init__(self) -> None:
        if _IDENTITY_RE.fullmatch(self.composition_node_id) is None:
            raise ValueError("Capability composition node identity is invalid")
        if self.attempt_generation < 1:
            raise ValueError("Capability attempt generation must be positive")
        if not self.capability_id.startswith("cap_") or not self.implementation_id.startswith("capi_"):
            raise ValueError("Capability implementation identity is invalid")
        if self.capability_version < 1 or self.implementation_version < 1:
            raise ValueError("Capability implementation version is invalid")
        if self.adapter_id is not None and _EXACT_REF_RE.fullmatch(self.adapter_id) is None:
            raise ValueError("Capability Adapter identity is invalid")
        if _HASH_RE.fullmatch(self.prompt_module_fingerprint) is None or _HASH_RE.fullmatch(self.dependency_fingerprint) is None:
            raise ValueError("Capability execution fingerprint is invalid")
        if _OUTPUT_CONTRACT_RE.fullmatch(self.output_contract) is None:
            raise ValueError("Capability output contract is invalid")
        if len(self.input_artifact_refs) != len(set(self.input_artifact_refs)) or any(
            _IDENTITY_RE.fullmatch(item) is None for item in self.input_artifact_refs
        ):
            raise ValueError("Capability input Artifact references are invalid")

    @classmethod
    def from_plan(
        cls,
        *,
        composition_node_id: str,
        plan: ImplementationExecutionPlan,
        output_contract: str,
        input_artifact_refs: tuple[str, ...] = (),
        attempt_generation: int = 1,
    ) -> CapabilityExecutionSelection:
        capability_id, capability_version = _parse_exact_ref(plan.capability_ref)
        implementation_id, implementation_version = _parse_exact_ref(
            plan.implementation_ref,
        )
        return cls(
            composition_node_id=composition_node_id,
            attempt_generation=attempt_generation,
            capability_id=capability_id,
            capability_version=capability_version,
            implementation_id=implementation_id,
            implementation_version=implementation_version,
            adapter_id=plan.artifact_adapter_ref,
            prompt_module_fingerprint=(plan.prompt_bundle_fingerprint or _EMPTY_PROMPT_FINGERPRINT),
            dependency_fingerprint=plan.fingerprint,
            input_artifact_refs=input_artifact_refs,
            output_contract=output_contract,
        )


@dataclass(frozen=True, slots=True)
class ExactCapabilityExecution:
    execution_id: str
    team_id: str
    turn_id: str
    agent_execution_id: str
    composition_node_id: str
    attempt_generation: int
    capability_id: str
    capability_version: int
    implementation_id: str
    implementation_version: int
    adapter_id: str | None
    prompt_module_fingerprint: str
    dependency_fingerprint: str
    input_artifact_refs: tuple[str, ...]
    output_contract: str
    status: CapabilityExecutionStatus


class CapabilityExecutionStore:
    """Persist and transition exact Capability attempts."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def select(
        self,
        *,
        agent_execution_id: str,
        expected_agent_capability_fingerprint: str,
        selection: CapabilityExecutionSelection,
    ) -> ExactCapabilityExecution:
        """Fix one compiler-selected implementation before its first effect."""

        if _HASH_RE.fullmatch(expected_agent_capability_fingerprint) is None:
            raise ValueError("Agent Capability fingerprint is invalid")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                result = await apply_capability_execution_conn(
                    db,
                    agent_execution_id=agent_execution_id,
                    expected_agent_capability_fingerprint=(expected_agent_capability_fingerprint),
                    selection=selection,
                )
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def mark_running(self, execution_id: str) -> None:
        await self._transition(execution_id, to_status="running")

    async def mark_terminal(
        self,
        execution_id: str,
        *,
        status: CapabilityTerminalStatus,
        failure_code: str | None = None,
    ) -> None:
        if failure_code is not None and _IDENTITY_RE.fullmatch(failure_code) is None:
            raise ValueError("Capability failure code is invalid")
        if status not in {"failed", "blocked"} and failure_code is not None:
            raise ValueError("Capability failure code requires failed or blocked status")
        await self._transition(
            execution_id,
            to_status=status,
            failure_code=failure_code,
        )

    async def read(self, execution_id: str) -> ExactCapabilityExecution | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await _execution_by_id(db, execution_id)
        return _execution_from_row(row) if row is not None else None

    async def read_for_agent(
        self,
        agent_execution_id: str,
    ) -> tuple[ExactCapabilityExecution, ...]:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM turn_capability_execution
                WHERE agent_execution_id=?
                ORDER BY created_at,id
                """,
                (agent_execution_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return tuple(_execution_from_row(row) for row in rows)

    async def _transition(
        self,
        execution_id: str,
        *,
        to_status: CapabilityExecutionStatus,
        failure_code: str | None = None,
    ) -> None:
        timestamp = _now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await _execution_by_id(db, execution_id)
                if row is None:
                    raise CapabilityExecutionError("capability_execution_not_found")
                current = str(row["status"])
                if current == to_status:
                    if (row["failure_code"] or None) != failure_code:
                        raise CapabilityExecutionError(
                            "capability_execution_terminal_conflict",
                        )
                    await db.commit()
                    return
                if to_status == "running":
                    if current != "selected":
                        raise CapabilityExecutionError(
                            "capability_execution_not_startable",
                        )
                    started_at = timestamp
                    ended_at = None
                else:
                    if to_status not in {"completed", "failed", "blocked", "cancelled"} or current not in {
                        "selected",
                        "running",
                    }:
                        raise CapabilityExecutionError(
                            "capability_execution_not_terminal",
                        )
                    started_at = row["started_at"]
                    ended_at = timestamp
                await db.execute(
                    """
                    UPDATE turn_capability_execution
                    SET status=?,started_at=?,ended_at=?,failure_code=?
                    WHERE id=?
                    """,
                    (
                        to_status,
                        started_at,
                        ended_at,
                        failure_code,
                        execution_id,
                    ),
                )
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                if "parent is not running" in str(exc):
                    raise CapabilityExecutionError(
                        "capability_agent_execution_not_running",
                    ) from exc
                raise
            except BaseException:
                await db.rollback()
                raise


async def apply_capability_execution_conn(
    db: aiosqlite.Connection,
    *,
    agent_execution_id: str,
    expected_agent_capability_fingerprint: str,
    selection: CapabilityExecutionSelection,
) -> ExactCapabilityExecution:
    """Insert one exact selection inside an existing admission transaction."""

    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT * FROM turn_agent_execution WHERE id=?",
        (agent_execution_id,),
    ) as cursor:
        parent = await cursor.fetchone()
    if (
        parent is None
        or str(parent["capability_fingerprint"]) != expected_agent_capability_fingerprint
        or str(parent["status"]) not in {"admitted", "running"}
    ):
        raise CapabilityExecutionError("capability_agent_execution_unavailable")

    existing = await _execution_by_attempt(
        db,
        agent_execution_id=agent_execution_id,
        composition_node_id=selection.composition_node_id,
        attempt_generation=selection.attempt_generation,
    )
    if existing is not None:
        if _selection_identity(existing) != _selection_identity(selection):
            raise CapabilityExecutionError("capability_execution_identity_conflict")
        if str(existing["status"]) not in {"selected", "running"}:
            raise CapabilityExecutionError("capability_execution_attempt_terminal")
        return _execution_from_row(existing)

    execution_id = f"capx_{uuid4().hex}"
    await db.execute(
        """
        INSERT INTO turn_capability_execution(
          id,team_id,turn_id,agent_execution_id,composition_node_id,
          attempt_generation,capability_id,capability_version,
          implementation_id,implementation_version,adapter_id,
          prompt_module_fingerprint,dependency_fingerprint,
          input_artifact_refs_json,output_contract,status,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'selected',?)
        """,
        (
            execution_id,
            str(parent["team_id"]),
            str(parent["turn_id"]),
            agent_execution_id,
            selection.composition_node_id,
            selection.attempt_generation,
            selection.capability_id,
            selection.capability_version,
            selection.implementation_id,
            selection.implementation_version,
            selection.adapter_id,
            selection.prompt_module_fingerprint,
            selection.dependency_fingerprint,
            json.dumps(
                selection.input_artifact_refs,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            selection.output_contract,
            _now(),
        ),
    )
    row = await _execution_by_id(db, execution_id)
    if row is None:
        raise CapabilityExecutionError("capability_execution_persist_failed")
    return _execution_from_row(row)


async def _execution_by_id(
    db: aiosqlite.Connection,
    execution_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM turn_capability_execution WHERE id=?",
        (execution_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _execution_by_attempt(
    db: aiosqlite.Connection,
    *,
    agent_execution_id: str,
    composition_node_id: str,
    attempt_generation: int,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT * FROM turn_capability_execution
        WHERE agent_execution_id=? AND composition_node_id=?
          AND attempt_generation=?
        """,
        (
            agent_execution_id,
            composition_node_id,
            attempt_generation,
        ),
    ) as cursor:
        return await cursor.fetchone()


def _selection_identity(
    value: aiosqlite.Row | CapabilityExecutionSelection,
) -> tuple[object, ...]:
    if isinstance(value, CapabilityExecutionSelection):
        return (
            value.capability_id,
            value.capability_version,
            value.implementation_id,
            value.implementation_version,
            value.adapter_id,
            value.prompt_module_fingerprint,
            value.dependency_fingerprint,
            value.input_artifact_refs,
            value.output_contract,
        )
    return (
        str(value["capability_id"]),
        int(value["capability_version"]),
        str(value["implementation_id"]),
        int(value["implementation_version"]),
        str(value["adapter_id"]) if value["adapter_id"] is not None else None,
        str(value["prompt_module_fingerprint"]),
        str(value["dependency_fingerprint"]),
        _decode_artifact_refs(str(value["input_artifact_refs_json"])),
        str(value["output_contract"]),
    )


def _execution_from_row(row: aiosqlite.Row) -> ExactCapabilityExecution:
    return ExactCapabilityExecution(
        execution_id=str(row["id"]),
        team_id=str(row["team_id"]),
        turn_id=str(row["turn_id"]),
        agent_execution_id=str(row["agent_execution_id"]),
        composition_node_id=str(row["composition_node_id"]),
        attempt_generation=int(row["attempt_generation"]),
        capability_id=str(row["capability_id"]),
        capability_version=int(row["capability_version"]),
        implementation_id=str(row["implementation_id"]),
        implementation_version=int(row["implementation_version"]),
        adapter_id=(str(row["adapter_id"]) if row["adapter_id"] is not None else None),
        prompt_module_fingerprint=str(row["prompt_module_fingerprint"]),
        dependency_fingerprint=str(row["dependency_fingerprint"]),
        input_artifact_refs=_decode_artifact_refs(
            str(row["input_artifact_refs_json"]),
        ),
        output_contract=str(row["output_contract"]),
        status=str(row["status"]),  # type: ignore[arg-type]
    )


def _decode_artifact_refs(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise CapabilityExecutionError("capability_execution_malformed") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise CapabilityExecutionError("capability_execution_malformed")
    return tuple(decoded)


def _parse_exact_ref(value: str) -> tuple[str, int]:
    match = _EXACT_REF_RE.fullmatch(value)
    if match is None:
        raise ValueError("Capability exact reference is invalid")
    return match.group(1), int(match.group(2))


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "CapabilityExecutionError",
    "CapabilityExecutionSelection",
    "CapabilityExecutionStore",
    "ExactCapabilityExecution",
    "apply_capability_execution_conn",
]
