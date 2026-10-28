"""Versioned system profile Skill constraints; no source discovery or grants."""

from __future__ import annotations

from bowhead_skill.runtime_scope import ExactSkillRuntimeScope

from bowhead_core.agent_dependency_compiler import ExactSkillGrant, parse_exact_skill_grants

FIELDS = {"skill_runtime_scope", "skill_grants"}
MIGRATION_TRIGGER = "trg_system_precise_skill_snapshot"
OUTPUT_BUDGET_TRIGGER = "trg_system_output_budget_snapshot"


def has_ordinary_system_skill_scope(definition: dict) -> bool:
    """Recognize the existing system profile without a precise Skill baseline."""
    return decode_system_skill_snapshot(definition) == (None, None)


def parse_system_manifest_skill_grants(raw: object) -> tuple[ExactSkillGrant, ...]:
    """Global system manifests can name only code-shipped reference packages."""
    grants = parse_exact_skill_grants(raw)
    if any(grant.kind != "builtin" or grant.distribution != "reference" for grant in grants):
        raise ValueError("system_skill_grants_require_builtin_reference")
    return grants


def encode_system_skill_snapshot(scope: ExactSkillRuntimeScope | None, grants: tuple[ExactSkillGrant, ...] | None) -> dict:
    if scope is None and grants is None:
        return {}
    if scope is not None and type(scope) is not ExactSkillRuntimeScope:
        raise ValueError("invalid system Skill scope")
    if grants is not None and (type(grants) is not tuple or any(type(grant) is not ExactSkillGrant for grant in grants)):
        raise ValueError("invalid system Skill grants")
    result = {
        "schema_version": 2,
        "skill_runtime_scope": None
        if scope is None
        else {"schema_version": scope.schema_version, "read_names": list(scope.read_names), "invoke_names": list(scope.invoke_names)},
        "skill_grants": None if grants is None else [grant.to_canonical_dict() for grant in grants],
    }
    # Apply the same bounded field reader used for retained definitions.
    decode_system_skill_snapshot(result)
    return result


def decode_system_skill_snapshot(definition: dict) -> tuple[ExactSkillRuntimeScope | None, tuple[ExactSkillGrant, ...] | None]:
    version = definition.get("schema_version")
    if type(version) is not int or version not in (1, 2, 3):
        raise ValueError("unsupported system Skill snapshot")
    if version == 1:
        if FIELDS.intersection(definition):
            raise ValueError("precise system Skill scope requires snapshot v2")
        return None, None
    if version == 3 and not FIELDS.intersection(definition):
        return None, None
    if not FIELDS.issubset(definition):
        raise ValueError("incomplete system Skill snapshot")
    raw_scope, raw_grants = definition["skill_runtime_scope"], definition["skill_grants"]
    if raw_scope is None and raw_grants is None:
        raise ValueError("empty system Skill snapshot v2")
    scope = None
    if raw_scope is not None:
        if (
            type(raw_scope) is not dict
            or set(raw_scope) != {"schema_version", "read_names", "invoke_names"}
            or type(raw_scope["schema_version"]) is not int
            or type(raw_scope["read_names"]) is not list
            or type(raw_scope["invoke_names"]) is not list
            or any(type(name) is not str for name in (*raw_scope["read_names"], *raw_scope["invoke_names"]))
        ):
            raise ValueError("invalid system Skill scope")
        scope = ExactSkillRuntimeScope(
            schema_version=raw_scope["schema_version"], read_names=tuple(raw_scope["read_names"]), invoke_names=tuple(raw_scope["invoke_names"])
        )
    grants = None if raw_grants is None else parse_exact_skill_grants(raw_grants)
    return scope, grants
