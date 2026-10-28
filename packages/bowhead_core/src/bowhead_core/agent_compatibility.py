"""Deterministic compatibility-major decisions for immutable Agent revisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx

AGENT_COMPATIBILITY_COMPILER_VERSION = "agent-compatibility-v1"
CompatibilityVerdict = Literal["compatible", "breaking", "unknown"]

_COMPLEX_SCHEMA_KEYS = {
    "$dynamicRef",
    "$ref",
    "allOf",
    "anyOf",
    "dependentRequired",
    "dependentSchemas",
    "if",
    "not",
    "oneOf",
    "patternProperties",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
}
_BOUND_KEYS = {
    "exclusiveMaximum",
    "exclusiveMinimum",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "multipleOf",
}


class AgentCompatibilityError(RuntimeError):
    """Stable failure raised while compiling or persisting compatibility."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentCompatibilityDecision:
    revision_id: str
    base_revision_id: str | None
    compatibility_major: int
    verdict: CompatibilityVerdict
    compiler_version: str
    contract_hash: str
    reason_codes: tuple[str, ...]


class AgentCompatibilityCompiler:
    """Compare only published contracts; prompt and implementation text are ignored."""

    compiler_version = AGENT_COMPATIBILITY_COMPILER_VERSION

    def compile(
        self,
        *,
        revision_id: str,
        candidate_definition: Mapping[str, Any],
        base_revision_id: str | None,
        base_definition: Mapping[str, Any] | None,
        base_compatibility_major: int | None,
    ) -> AgentCompatibilityDecision:
        candidate_contract, candidate_reasons = _contract(candidate_definition)
        contract_hash = _hash_document(candidate_contract)
        if base_revision_id is None:
            if base_definition is not None or base_compatibility_major is not None:
                raise ValueError("Initial Agent compatibility cannot include a base")
            verdict: CompatibilityVerdict = "unknown" if candidate_reasons else "compatible"
            reasons = candidate_reasons or {"initial_revision"}
            return AgentCompatibilityDecision(
                revision_id=revision_id,
                base_revision_id=None,
                compatibility_major=1,
                verdict=verdict,
                compiler_version=self.compiler_version,
                contract_hash=contract_hash,
                reason_codes=tuple(sorted(reasons)),
            )
        if base_definition is None or base_compatibility_major is None or base_compatibility_major < 1:
            raise ValueError("Agent compatibility base identity is incomplete")

        base_contract, base_reasons = _contract(base_definition)
        breaking: set[str] = set()
        unknown = set(candidate_reasons) | set(base_reasons)
        _compare_contracts(
            base=base_contract,
            candidate=candidate_contract,
            breaking=breaking,
            unknown=unknown,
        )
        if breaking:
            verdict = "breaking"
            major = base_compatibility_major + 1
            reasons = breaking | unknown
        elif unknown:
            verdict = "unknown"
            major = base_compatibility_major
            reasons = unknown
        else:
            verdict = "compatible"
            major = base_compatibility_major
            reasons = {"published_contract_compatible"}
        return AgentCompatibilityDecision(
            revision_id=revision_id,
            base_revision_id=base_revision_id,
            compatibility_major=major,
            verdict=verdict,
            compiler_version=self.compiler_version,
            contract_hash=contract_hash,
            reason_codes=tuple(sorted(reasons)),
        )


class AgentCompatibilityStore:
    """Compile once from exact revision rows and append the immutable decision."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        compiler: AgentCompatibilityCompiler | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self._compiler = compiler or AgentCompatibilityCompiler()

    async def compile_and_store(
        self,
        *,
        team_id: str,
        agent_id: str,
        revision_id: str,
        base_revision_id: str | None,
        created_at: str,
    ) -> AgentCompatibilityDecision:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                decision = await self.compile_and_store_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    revision_id=revision_id,
                    base_revision_id=base_revision_id,
                    created_at=created_at,
                )
                await db.commit()
                return decision
            except BaseException:
                await db.rollback()
                raise

    async def compile_and_store_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        revision_id: str,
        base_revision_id: str | None,
        created_at: str,
    ) -> AgentCompatibilityDecision:
        db.row_factory = aiosqlite.Row
        candidate = await _revision_row(
            db,
            team_id=team_id,
            agent_id=agent_id,
            revision_id=revision_id,
        )
        if candidate is None:
            raise AgentCompatibilityError("agent_compatibility_revision_not_found")
        base = None
        base_major = None
        if base_revision_id is not None:
            base = await _revision_row(
                db,
                team_id=team_id,
                agent_id=agent_id,
                revision_id=base_revision_id,
            )
            if base is None:
                raise AgentCompatibilityError("agent_compatibility_base_revision_not_found")
            async with db.execute(
                """
                SELECT compatibility_major FROM agent_revision_compatibility
                WHERE team_id=? AND agent_id=? AND revision_id=?
                """,
                (team_id, agent_id, base_revision_id),
            ) as cursor:
                base_compatibility = await cursor.fetchone()
            if base_compatibility is None:
                raise AgentCompatibilityError("agent_compatibility_base_missing")
            base_major = int(base_compatibility["compatibility_major"])

        decision = self._compiler.compile(
            revision_id=revision_id,
            candidate_definition=_object(candidate["definition_json"]),
            base_revision_id=base_revision_id,
            base_definition=(_object(base["definition_json"]) if base is not None else None),
            base_compatibility_major=base_major,
        )
        async with db.execute(
            "SELECT * FROM agent_revision_compatibility WHERE revision_id=?",
            (revision_id,),
        ) as cursor:
            existing = await cursor.fetchone()
        if existing is not None:
            persisted = _decision_from_row(existing)
            if persisted != decision:
                raise AgentCompatibilityError("agent_compatibility_decision_conflict")
            return persisted
        await db.execute(
            """
            INSERT INTO agent_revision_compatibility(
              revision_id,team_id,agent_id,base_revision_id,compatibility_major,
              verdict,compiler_version,contract_hash,reason_codes_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                revision_id,
                team_id,
                agent_id,
                base_revision_id,
                decision.compatibility_major,
                decision.verdict,
                decision.compiler_version,
                decision.contract_hash,
                _canonical_json(list(decision.reason_codes)),
                created_at,
            ),
        )
        return decision

    async def get(
        self,
        *,
        team_id: str,
        agent_id: str,
        revision_id: str,
    ) -> AgentCompatibilityDecision | None:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM agent_revision_compatibility
                WHERE team_id=? AND agent_id=? AND revision_id=?
                """,
                (team_id, agent_id, revision_id),
            ) as cursor:
                row = await cursor.fetchone()
        return _decision_from_row(row) if row is not None else None


def _contract(definition: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
    reasons: set[str] = set()
    output_schema = definition.get("output_schema")
    if output_schema is not None and not isinstance(output_schema, Mapping):
        reasons.add("output_schema_malformed")
    primary = definition.get("primary_capability_ref")
    if primary is not None and not isinstance(primary, str):
        reasons.add("primary_capability_malformed")
        primary = None
    delivery = _string_set(definition.get("required_delivery_formats", ()))
    if delivery is None:
        reasons.add("delivery_format_contract_malformed")
        delivery = ()
    callable_contracts = _string_set(definition.get("callable_contracts", ()))
    if callable_contracts is None:
        reasons.add("callable_contract_malformed")
        callable_contracts = ()
    return (
        {
            "callable_contracts": list(callable_contracts),
            "output_schema": _json_value(output_schema),
            "primary_capability_ref": primary,
            "required_delivery_formats": list(delivery),
            "schema_version": 1,
        },
        reasons,
    )


def _compare_contracts(
    *,
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
    breaking: set[str],
    unknown: set[str],
) -> None:
    base_primary = base.get("primary_capability_ref")
    candidate_primary = candidate.get("primary_capability_ref")
    if base_primary is not None and base_primary != candidate_primary:
        breaking.add("primary_capability_removed_or_changed")
    for key, reason in (
        ("required_delivery_formats", "required_delivery_format_removed"),
        ("callable_contracts", "published_callable_contract_removed"),
    ):
        if not set(base.get(key, ())).issubset(set(candidate.get(key, ()))):
            breaking.add(reason)

    base_schema = base.get("output_schema")
    candidate_schema = candidate.get("output_schema")
    if base_schema is None:
        return
    if candidate_schema is None:
        breaking.add("output_schema_removed")
        return
    if not isinstance(base_schema, Mapping) or not isinstance(candidate_schema, Mapping):
        unknown.add("output_schema_not_comparable")
        return
    _compare_schema(base_schema, candidate_schema, breaking=breaking, unknown=unknown)


def _compare_schema(
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    breaking: set[str],
    unknown: set[str],
) -> None:
    for key in _COMPLEX_SCHEMA_KEYS:
        if base.get(key) != candidate.get(key):
            unknown.add("output_schema_complex_constraint_changed")
    base_types = _schema_types(base)
    candidate_types = _schema_types(candidate)
    if base_types is None or candidate_types is None:
        unknown.add("output_schema_type_unknown")
    elif candidate_types != base_types:
        if candidate_types < base_types or (base_types == {"number"} and candidate_types == {"integer"}):
            breaking.add("output_schema_type_narrowed")
        else:
            unknown.add("output_schema_type_changed")

    base_enum = base.get("enum")
    candidate_enum = candidate.get("enum")
    if base_enum != candidate_enum and (base_enum is not None or candidate_enum is not None):
        if isinstance(base_enum, list) and isinstance(candidate_enum, list):
            try:
                if {_canonical_json(item) for item in candidate_enum} < {_canonical_json(item) for item in base_enum}:
                    breaking.add("output_schema_enum_narrowed")
                else:
                    unknown.add("output_schema_enum_changed")
            except (TypeError, ValueError):
                unknown.add("output_schema_enum_unknown")
        else:
            unknown.add("output_schema_enum_unknown")

    for key in _BOUND_KEYS:
        if base.get(key) != candidate.get(key):
            if _constraint_tightened(key, base.get(key), candidate.get(key)):
                breaking.add("output_schema_constraint_tightened")
            else:
                unknown.add("output_schema_constraint_changed")

    base_properties = base.get("properties", {})
    candidate_properties = candidate.get("properties", {})
    if not isinstance(base_properties, Mapping) or not isinstance(candidate_properties, Mapping):
        unknown.add("output_schema_properties_unknown")
        return
    for name, base_property in base_properties.items():
        if name not in candidate_properties:
            breaking.add("output_schema_field_removed")
            continue
        candidate_property = candidate_properties[name]
        if not isinstance(base_property, Mapping) or not isinstance(candidate_property, Mapping):
            if base_property != candidate_property:
                unknown.add("output_schema_field_unknown")
            continue
        _compare_schema(base_property, candidate_property, breaking=breaking, unknown=unknown)

    base_required = _string_set(base.get("required", ()))
    candidate_required = _string_set(candidate.get("required", ()))
    if base_required is None or candidate_required is None:
        unknown.add("output_schema_required_unknown")
    elif not set(base_required).issubset(candidate_required):
        breaking.add("output_schema_required_field_removed")

    base_items = base.get("items")
    candidate_items = candidate.get("items")
    if isinstance(base_items, Mapping):
        if not isinstance(candidate_items, Mapping):
            breaking.add("output_schema_array_items_removed")
        else:
            _compare_schema(base_items, candidate_items, breaking=breaking, unknown=unknown)
    elif base_items != candidate_items:
        unknown.add("output_schema_array_items_changed")

    base_additional = base.get("additionalProperties", True)
    candidate_additional = candidate.get("additionalProperties", True)
    if base_additional is True and candidate_additional is False:
        breaking.add("output_schema_additional_properties_narrowed")
    elif base_additional != candidate_additional and not (isinstance(base_additional, bool) and isinstance(candidate_additional, bool)):
        unknown.add("output_schema_additional_properties_changed")


def _schema_types(schema: Mapping[str, Any]) -> set[str] | None:
    value = schema.get("type")
    if value is None and "properties" in schema:
        return {"object"}
    if value is None:
        return None
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return set(value)
    return None


def _constraint_tightened(key: str, base: object, candidate: object) -> bool:
    if not isinstance(base, int | float) or isinstance(base, bool):
        return False
    if not isinstance(candidate, int | float) or isinstance(candidate, bool):
        return False
    if key in {"minimum", "exclusiveMinimum", "minItems", "minLength"}:
        return candidate > base
    if key in {"maximum", "exclusiveMaximum", "maxItems", "maxLength"}:
        return candidate < base
    return key == "multipleOf" and candidate != base


def _string_set(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list | tuple) or any(not isinstance(item, str) or not item for item in value):
        return None
    if len(set(value)) != len(value):
        return None
    return tuple(sorted(value))


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


async def _revision_row(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    revision_id: str,
) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT * FROM team_subagent_revision
        WHERE team_id=? AND subagent_id=? AND id=?
        """,
        (team_id, agent_id, revision_id),
    ) as cursor:
        return await cursor.fetchone()


def _decision_from_row(row: Mapping[str, Any]) -> AgentCompatibilityDecision:
    reasons = json.loads(str(row["reason_codes_json"]))
    if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
        raise AgentCompatibilityError("agent_compatibility_reason_codes_malformed")
    verdict = str(row["verdict"])
    if verdict not in {"compatible", "breaking", "unknown"}:
        raise AgentCompatibilityError("agent_compatibility_verdict_malformed")
    return AgentCompatibilityDecision(
        revision_id=str(row["revision_id"]),
        base_revision_id=(str(row["base_revision_id"]) if row["base_revision_id"] is not None else None),
        compatibility_major=int(row["compatibility_major"]),
        verdict=verdict,
        compiler_version=str(row["compiler_version"]),
        contract_hash=str(row["contract_hash"]),
        reason_codes=tuple(reasons),
    )


def _object(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise AgentCompatibilityError("agent_compatibility_definition_malformed") from exc
    if not isinstance(decoded, dict):
        raise AgentCompatibilityError("agent_compatibility_definition_malformed")
    return decoded


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash_document(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode()).hexdigest()}"


__all__ = [
    "AGENT_COMPATIBILITY_COMPILER_VERSION",
    "AgentCompatibilityCompiler",
    "AgentCompatibilityDecision",
    "AgentCompatibilityError",
    "AgentCompatibilityStore",
]
