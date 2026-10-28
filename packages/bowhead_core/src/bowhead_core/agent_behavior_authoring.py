"""Owner-safe authoring projection for revision-bound Agent behavior.

The projection is advisory UI state. Exact dependency compilation and the
immutable behavior descriptor remain the publication authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Literal

from bowhead_core.agent_behavior_contract import LeadRoleId, canonical_fingerprint
from bowhead_core.agent_capability_catalog import CapabilityCatalog, default_catalog
from bowhead_core.agent_model_policy import (
    TEAM_SUBAGENT_ALLOWED_MODEL_ROLES,
    TEAM_SUBAGENT_DEFAULT_MODEL_ROLE,
    TEAM_SUBAGENT_MODEL_POLICY_VERSION,
)

AGENT_AUTHORING_CATALOG_SCHEMA_VERSION = 2
AGENT_MODEL_POLICY_VERSION = TEAM_SUBAGENT_MODEL_POLICY_VERSION
_EXACT_CAPABILITY_RE = re.compile(r"^cap_[a-z][a-z0-9_]{1,63}@[1-9][0-9]*$")
_DERIVED_BEHAVIOR_FIELDS = frozenset(
    {
        "adapter_refs",
        "behavior_fingerprint",
        "capability_fingerprint",
        "dependency_fingerprint",
        "effective_model_role",
        "effective_capabilities",
        "effective_capability_refs",
        "eligible_lead_role_ids",
        "implementation_refs",
        "output_contract_refs",
        "prompt_module_fingerprint",
        "prompt_module_refs",
        "source_definition_hash",
        "verification_state",
        "extension_policy_digest",
    },
)


class AgentBehaviorAuthoringError(ValueError):
    """An Owner-authored behavior intent is malformed or outside policy."""

    def __init__(self, code: str, *, reason_codes: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.reason_codes = reason_codes


@dataclass(frozen=True, slots=True)
class AgentPrimaryCapabilityOption:
    exact_ref: str
    capability_id: str
    version: int
    category: str
    display_name: str
    description: str
    display_name_en: str
    description_en: str
    selectable: bool
    eligible_role_ids: tuple[LeadRoleId, ...]
    verification_state: Literal["blocked", "shadow", "reviewed"]
    reason_codes: tuple[str, ...]
    output_contract_refs: tuple[str, ...]

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AgentModelPolicy:
    allowed_roles: tuple[str, ...]
    effective_role: str
    locked: bool
    policy_version: str = AGENT_MODEL_POLICY_VERSION

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "allowed_roles": list(self.allowed_roles),
            "effective_role": self.effective_role,
            "locked": self.locked,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class AgentBehaviorAuthoringSnapshot:
    primary_capabilities: tuple[AgentPrimaryCapabilityOption, ...]
    model_policy: AgentModelPolicy
    fingerprint: str
    schema_version: int = AGENT_AUTHORING_CATALOG_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class AgentBehaviorIntentValidation:
    primary_capability_ref: str | None
    effective_model_role: str
    status: Literal["not_requested", "catalog_eligible", "incomplete"]
    reason_codes: tuple[str, ...]

    def working_copy_validation(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "behavior": {
                "effective_model_role": self.effective_model_role,
                "primary_capability_ref": self.primary_capability_ref,
                "reason_codes": list(self.reason_codes),
                "status": self.status,
            },
            "status": "incomplete" if self.status == "incomplete" else "not_validated",
        }
        if self.reason_codes:
            value["reason_codes"] = list(self.reason_codes)
        return value


class AgentBehaviorAuthoringService:
    """Project the platform catalog and validate Owner-only behavior intent."""

    def __init__(self, catalog: CapabilityCatalog | None = None) -> None:
        resolved = catalog or default_catalog()
        if resolved is None:
            raise AgentBehaviorAuthoringError("behavior_compiler_unsupported")
        self._catalog = resolved
        allowed_roles = tuple(sorted(TEAM_SUBAGENT_ALLOWED_MODEL_ROLES))
        self._model_policy = AgentModelPolicy(
            allowed_roles=allowed_roles,
            effective_role=TEAM_SUBAGENT_DEFAULT_MODEL_ROLE,
            locked=len(allowed_roles) == 1,
        )

    def snapshot(self) -> AgentBehaviorAuthoringSnapshot:
        latest = {
            definition.id: definition
            for definition in self._catalog.definitions
            if definition.lifecycle != "retired"
            and definition.authoring_selectable
            and not definition.system_only
            and definition == self._catalog.latest_definition(definition.id)
        }
        options = tuple(
            sorted(
                (self._option(definition.id, definition.version) for definition in latest.values()),
                key=lambda item: (item.category, item.display_name_en, item.exact_ref),
            ),
        )
        payload = {
            "catalog_fingerprint": self._catalog.fingerprint,
            "model_policy": self._model_policy.to_public_dict(),
            "primary_capabilities": [item.to_public_dict() for item in options],
            "schema_version": AGENT_AUTHORING_CATALOG_SCHEMA_VERSION,
        }
        return AgentBehaviorAuthoringSnapshot(
            primary_capabilities=options,
            model_policy=self._model_policy,
            fingerprint=canonical_fingerprint(payload),
        )

    def validate_definition_intent(
        self,
        definition: Mapping[str, Any],
    ) -> AgentBehaviorIntentValidation:
        forbidden = tuple(sorted(_DERIVED_BEHAVIOR_FIELDS.intersection(definition)))
        if forbidden:
            raise AgentBehaviorAuthoringError(
                "agent_behavior_derived_fields_forbidden",
                reason_codes=tuple(f"derived_field:{item}" for item in forbidden),
            )

        from bowhead_core.agent_runtime_extensions import read_agent_runtime_extensions

        read_agent_runtime_extensions(definition)
        model = definition.get("model")
        if model not in (None, "", *self._model_policy.allowed_roles):
            raise AgentBehaviorAuthoringError(
                "agent_model_role_not_allowed",
                reason_codes=("model_role_not_allowed",),
            )

        raw_primary = definition.get("primary_capability_ref")
        if raw_primary is None:
            return AgentBehaviorIntentValidation(
                primary_capability_ref=None,
                effective_model_role=self._model_policy.effective_role,
                status="not_requested",
                reason_codes=(),
            )
        if not isinstance(raw_primary, str) or _EXACT_CAPABILITY_RE.fullmatch(raw_primary) is None:
            raise AgentBehaviorAuthoringError(
                "primary_capability_unknown",
                reason_codes=("primary_capability_malformed",),
            )
        capability_id, raw_version = raw_primary.rsplit("@", 1)
        try:
            definition_contract = self._catalog.get_definition(capability_id, int(raw_version))
        except (KeyError, ValueError) as exc:
            raise AgentBehaviorAuthoringError(
                "primary_capability_unknown",
                reason_codes=("primary_capability_unknown",),
            ) from exc
        if definition_contract.lifecycle != "active":
            raise AgentBehaviorAuthoringError(
                "primary_capability_inactive",
                reason_codes=("primary_capability_inactive",),
            )
        if not definition_contract.authoring_selectable or definition_contract.system_only:
            raise AgentBehaviorAuthoringError(
                "primary_capability_not_selectable",
                reason_codes=("primary_capability_not_selectable",),
            )
        option = self._option(definition_contract.id, definition_contract.version)
        if not option.selectable:
            return AgentBehaviorIntentValidation(
                primary_capability_ref=raw_primary,
                effective_model_role=self._model_policy.effective_role,
                status="incomplete",
                reason_codes=tuple(sorted({"primary_capability_unproven", *option.reason_codes})),
            )
        return AgentBehaviorIntentValidation(
            primary_capability_ref=raw_primary,
            effective_model_role=self._model_policy.effective_role,
            status="catalog_eligible",
            reason_codes=(),
        )

    def _option(self, capability_id: str, version: int) -> AgentPrimaryCapabilityOption:
        definition = self._catalog.get_definition(capability_id, version)
        implementations = tuple(item for item in self._catalog.implementations_for(definition) if item.lifecycle == "active")
        reviewed = tuple(item for item in implementations if item.review_state == "reviewed")
        roles = tuple(sorted({role for item in reviewed for role in item.lead_role_ids}))
        reasons: set[str] = set()
        if definition.lifecycle != "active":
            reasons.add("primary_capability_inactive")
        if not definition.authoring_selectable or definition.system_only:
            reasons.add("primary_capability_not_selectable")
        if not reviewed:
            reasons.add(f"no_reviewed_implementation:{definition.exact_ref}")
        elif not roles:
            reasons.add(f"no_eligible_role:{definition.exact_ref}")
        selectable = not reasons
        if selectable:
            state: Literal["blocked", "shadow", "reviewed"] = "reviewed"
        elif implementations and not reviewed:
            state = "shadow"
        else:
            state = "blocked"
        zh = definition.listing["zh-CN"]
        en = definition.listing["en-US"]
        return AgentPrimaryCapabilityOption(
            exact_ref=definition.exact_ref,
            capability_id=definition.id,
            version=definition.version,
            category=definition.category,
            display_name=zh.name,
            description=zh.description,
            display_name_en=en.name,
            description_en=en.description,
            selectable=selectable,
            eligible_role_ids=roles,
            verification_state=state,
            reason_codes=tuple(sorted(reasons)),
            output_contract_refs=tuple(item.exact_ref for item in definition.outputs),
        )


@lru_cache(maxsize=1)
def default_agent_behavior_authoring_service() -> AgentBehaviorAuthoringService:
    return AgentBehaviorAuthoringService()


__all__ = [
    "AGENT_AUTHORING_CATALOG_SCHEMA_VERSION",
    "AGENT_MODEL_POLICY_VERSION",
    "AgentBehaviorAuthoringError",
    "AgentBehaviorAuthoringService",
    "AgentBehaviorAuthoringSnapshot",
    "AgentBehaviorIntentValidation",
    "AgentModelPolicy",
    "AgentPrimaryCapabilityOption",
    "default_agent_behavior_authoring_service",
]
