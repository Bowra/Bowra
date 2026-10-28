"""Published, bounded opt-in policy for explicit read-only Skill selections.

This describes the Agent's optional scope. It grants no user access, does not
resolve content, and never adds Skill invocation, tools, or connections.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from bowhead_core.agent_behavior_contract import canonical_fingerprint
from bowhead_core.chat_references import ContentHash, Identity, SkillResourceRef, _bounded_json

MAX_EXTENSION_POLICY_BYTES = 16 * 1024
MAX_EXTENSION_SELECTORS = 32


class AgentRuntimeExtensionError(ValueError):
    """Finite policy failure; callers must not expose hidden selector details."""

    def __init__(self, code: str = "agent_runtime_extensions_invalid"):
        self.code = code
        super().__init__(code)


class _PolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SkillExtensionSelector(_PolicyRecord):
    source_kind: Literal["builtin", "team", "user"]
    id: Identity
    version: ContentHash | None = None

    def matches(self, resource: SkillResourceRef) -> bool:
        return (self.source_kind, self.id) == (resource.source_kind, resource.id) and self.version in {None, resource.version}


class ReadonlySkillExtensionPolicy(_PolicyRecord):
    enabled: StrictBool
    selection_sources: tuple[Literal["user_explicit"], ...] = Field(min_length=1, max_length=1)
    allowed_sources: tuple[Literal["builtin", "team", "user"], ...] = Field(min_length=1, max_length=3)
    resource_allowlist: tuple[SkillExtensionSelector, ...] = Field(max_length=MAX_EXTENSION_SELECTORS)
    require_read_only_contract: StrictBool
    max_selected: StrictInt = Field(ge=1, le=3)

    @model_validator(mode="after")
    def bounded_selection(self) -> Self:
        if not self.require_read_only_contract or len(set(self.allowed_sources)) != len(self.allowed_sources):
            raise ValueError("read-only policy requires unique source kinds")
        identities = [(item.source_kind, item.id) for item in self.resource_allowlist]
        if len(set(identities)) != len(identities) or any(kind not in self.allowed_sources for kind, _ in identities):
            raise ValueError("ambiguous or undeclared Skill selector")
        if self.enabled and not identities:
            raise ValueError("enabled extensions require a finite allowlist")
        return self

    def permits(self, resource: SkillResourceRef) -> bool:
        return self.enabled and resource.source_kind in self.allowed_sources and any(item.matches(resource) for item in self.resource_allowlist)


class AgentRuntimeExtensions(_PolicyRecord):
    schema_version: StrictInt = Field(ge=1, le=1)
    skill_read: ReadonlySkillExtensionPolicy

    def canonical_value(self) -> dict:
        value = self.model_dump(mode="json", exclude_none=True)
        rule = value["skill_read"]
        rule["allowed_sources"] = sorted(rule["allowed_sources"])
        rule["resource_allowlist"] = sorted(rule["resource_allowlist"], key=lambda item: (item["source_kind"], item["id"]))
        return value

    @property
    def digest(self) -> str:
        return canonical_fingerprint(self.canonical_value())

    def require_private_audience(self, audience_mode: str) -> None:
        if audience_mode != "private" and any(item.source_kind == "user" for item in self.skill_read.resource_allowlist):
            raise AgentRuntimeExtensionError("agent_runtime_extension_private_source")

    def require_selection(self, resources: Sequence[SkillResourceRef]) -> None:
        if not resources or len(resources) > self.skill_read.max_selected or any(not self.skill_read.permits(resource) for resource in resources):
            raise AgentRuntimeExtensionError("reference_skill_extension_denied")


def read_agent_runtime_extensions(definition: Mapping) -> AgentRuntimeExtensions | None:
    """Missing/null is closed; malformed or future contracts never fall back."""
    raw = definition.get("runtime_extensions")
    if raw is None:
        return None
    try:
        _bounded_json(raw, max_depth=6, max_bytes=MAX_EXTENSION_POLICY_BYTES)
        return AgentRuntimeExtensions.model_validate_json(json.dumps(raw, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        raise AgentRuntimeExtensionError() from None


def extension_policy_summary(definition: Mapping) -> dict | None:
    """Review metadata only; do not expose private selector IDs or source text."""
    try:
        policy = read_agent_runtime_extensions(definition)
    except AgentRuntimeExtensionError:
        return {"state": "unsupported"}
    if policy is None:
        return None
    rule = policy.skill_read
    return {
        "state": "supported",
        "enabled": rule.enabled,
        "source_kinds": sorted(rule.allowed_sources),
        "selector_count": len(rule.resource_allowlist),
        "pinned_count": sum(item.version is not None for item in rule.resource_allowlist),
        "max_selected": rule.max_selected,
        "read_only": True,
    }


def require_extension_policy_preserved(previous: Mapping, candidate: Mapping) -> None:
    if previous.get("runtime_extensions") is not None and "runtime_extensions" not in candidate:
        raise AgentRuntimeExtensionError("agent_runtime_extension_preservation_required")
