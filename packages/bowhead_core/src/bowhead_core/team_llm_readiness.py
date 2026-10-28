"""Pure required-capability expansion and authoritative readiness decisions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from bowhead_router.agent_tiers import resolve_tiers
from bowhead_router.model_capabilities import (
    inferred_provider_capabilities,
)

from bowhead_core.team_llm_evidence import (
    Capability,
    FactState,
    canonical_json,
    compute_config_signature,
)
from bowhead_router import CompatibleProbeRegistry

# v2 (feedback-triage-2026-08-24): the semantic hash no longer includes evidence_id; see
# the rationale above semantic_payload in `compute_team_llm_readiness`.
# Increment the version too: the hash shape changed, so any already-persisted old draft_semantic_hash and
# risk-acceptance tokens bound to it must become invalid. Without a version bump they report a hash mismatch,
# recreating the very 409 this fix addresses and confusing users further. A version bump gives a one-time,
# explainable invalidation: affected users simply review again.
READINESS_CONTRACT_VERSION = "team-llm-readiness-v2"
ReadinessStatus = Literal[
    "ready",
    "warning",
    "blocked",
    "risk_acceptance_required",
    "risk_accepted",
    "grandfathered",
]


@dataclass(frozen=True, slots=True)
class RequiredAssignment:
    role_key: str
    provider_id: str
    provider_runtime_key: str
    provider_display_name: str
    model_id: str
    capability: Capability
    config_signature: str
    testability_contract_version: str
    probe_contract_version: str | None
    probe_execution_policy_version: str
    routing_critical: bool
    declared: bool

    @property
    def scope_key(self) -> tuple[str, str, Capability, str]:
        return (
            self.provider_id,
            self.model_id,
            self.capability,
            self.config_signature,
        )

    def acceptance_key_for(
        self,
        risk_code: str,
    ) -> tuple[str, str, Capability, str, str, str, str]:
        """认领键**含 risk_code** —— 认领「测不了」不等于认领「测了但失败」。

        `team_llm_risk_acceptances` 本就有 risk_code 列，此前只是没进匹配键；
        补进来不需要迁移，但会让旧的 not_testable 认领不再顺带覆盖新增的失败类风险
        （这正是想要的：换了一种风险就要重新知情）。
        """
        return (
            self.provider_id,
            self.model_id,
            self.capability,
            self.role_key,
            self.config_signature,
            self.testability_contract_version,
            risk_code,
        )


@dataclass(frozen=True, slots=True)
class ReadinessFact:
    state: FactState = "unknown"
    evidence_id: str | None = None
    safe_code: str | None = None
    observed_at: str | None = None
    fresh_until: str | None = None
    attempt_state: str | None = None
    attempt_safe_code: str | None = None
    #: Provider-reported model ID. If different from the configured name, it records what the provider actually returned;
    #: since 2026-08-25 a name mismatch no longer means failure (gateway aliases are common); this observation is the sole basis for judgment.
    actual_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    code: str
    field_path: str
    role_key: str
    provider_id: str
    model_id: str
    capability: Capability
    action_code: str


@dataclass(frozen=True, slots=True)
class ReadinessRow:
    assignment: RequiredAssignment
    fact: ReadinessFact
    status: ReadinessStatus
    changed: bool
    issue_code: str | None = None
    acceptance_active: bool = False
    grandfathered: bool = False


RiskCode = Literal[
    "capability_not_testable",
    "capability_failed",
    "capability_stale",
    "capability_unknown",
]

#: fact_state → persisted `risk_code` for accepting this risk. Separate codes are intentional: untestable and
#: tested-but-failed are different declarations; accepting one must not automatically cover the other (the acceptance key includes risk_code).
_RISK_CODE_BY_FACT_STATE: dict[str, RiskCode] = {
    "failed": "capability_failed",
    "stale": "capability_stale",
    "unknown": "capability_unknown",
}


@dataclass(frozen=True, slots=True)
class RiskAcceptanceRequest:
    assignment: RequiredAssignment
    risk_code: RiskCode = "capability_not_testable"


@dataclass(frozen=True, slots=True)
class ExternalReadinessRow:
    subject_kind: Literal[
        "media_image_generation",
        "web_search",
    ]
    configured: bool
    status: Literal["configured", "unavailable"]


@dataclass(frozen=True, slots=True)
class ReadinessDecision:
    draft_semantic_hash: str
    rows: tuple[ReadinessRow, ...]
    blocking: tuple[ReadinessIssue, ...]
    warnings: tuple[ReadinessIssue, ...]
    required_acceptances: tuple[RiskAcceptanceRequest, ...]
    external_rows: tuple[ExternalReadinessRow, ...]
    baseline_will_invalidate: bool
    may_apply: bool


def _provider_config(provider: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(provider)
    extra = dict(normalized.get("extra") or {})
    for key in (
        "messages_path",
        "anthropic_version",
        "sanitize_tool_dots",
    ):
        if key in normalized:
            extra[key] = normalized[key]
    normalized["extra"] = extra
    return normalized


def _declared_capabilities(
    provider: dict[str, Any],
    model_id: str,
) -> frozenset[str]:
    models = provider.get("models")
    if not isinstance(models, list):
        return frozenset()
    for model in models:
        if isinstance(model, str) and model == model_id:
            return inferred_provider_capabilities(provider.get("supports"))
        if not isinstance(model, dict) or str(model.get("id") or "") != model_id:
            continue
        capabilities = model.get("capabilities")
        if not isinstance(capabilities, list):
            return frozenset()
        return frozenset(str(capability) for capability in capabilities if isinstance(capability, str))
    return frozenset()


def build_required_assignments(
    config: dict[str, Any],
    *,
    provider_ids: dict[str, str],
) -> tuple[RequiredAssignment, ...]:
    """Expand concrete Tier and KB references into capability rows."""

    providers = config.get("providers") or {}
    tier_config = config
    if not isinstance(config.get("tiers"), dict):
        agents = config.get("agents") or {}
        lead = agents.get("lead") if isinstance(agents, dict) else {}
        echo = agents.get("echo") if isinstance(agents, dict) else {}
        judge = agents.get("judge") if isinstance(agents, dict) else {}
        tier_config = {
            **config,
            "tiers": {
                "primary": (lead.get("primary") if isinstance(lead, dict) else None),
                "primary_fallback": (lead.get("fallback") if isinstance(lead, dict) else []),
                "fast": (echo.get("primary") if isinstance(echo, dict) else None),
                "judge": (judge.get("primary") if isinstance(judge, dict) else None),
            },
        }
    resolved = resolve_tiers(tier_config)
    #: Last tuple field = declaration-source override. Tier rows use None (continue consulting provider["models"]);
    #: KB rows supply their own capability set: KB declarations already live in `kb.models[*].capability`;
    #: see the comment below.
    raw: list[tuple[str, str, str, tuple[Capability, ...], bool, frozenset[str] | None]] = [
        (
            "tier.primary",
            resolved.primary.provider,
            resolved.primary.model,
            ("chat", "tool"),
            True,
            None,
        ),
        *[
            (
                "tier.primary_fallback",
                ref.provider,
                ref.model,
                ("chat", "tool"),
                True,
                None,
            )
            for ref in resolved.primary_fallbacks
        ],
        (
            "tier.fast",
            resolved.fast.provider,
            resolved.fast.model,
            ("chat",),
            True,
            None,
        ),
        (
            "tier.judge",
            resolved.judge.provider,
            resolved.judge.model,
            ("chat",),
            True,
            None,
        ),
    ]
    kb = config.get("kb") or {}
    kb_models = kb.get("models") or {}
    for capability, model_name in (kb.get("active") or {}).items():
        model = kb_models.get(model_name)
        if not isinstance(model, dict):
            continue
        provider_key = str(model.get("provider") or "")
        model_id = str(model.get("real_id") or model.get("model") or "")
        required: tuple[Capability, ...]
        if capability == "row_extractor":
            required = ("chat", "tool")
        elif capability in {"embed", "rerank", "vision"}:
            required = (capability,)  # type: ignore[assignment]
        else:
            continue
        # Evidence from 116 on 2026-08-15: KB model declarations live in `kb.models[*].capability`, not
        # `provider["models"]`. `_validate_kb` (the authoritative configuration validator) also only reads the former.
        # Previously readiness also searched the provider list for KB rows: embed/vision/rerank on 116
        # always became `required_capability_not_declared` and hard-blocked, preventing the team from saving configuration;
        # meanwhile the adjacent configuration check reported no blocking issues, contradicting the same screen.
        # Two implementations answered whether a capability was declared with opposite conclusions; use KB's own declarations.
        raw.append(
            (
                f"kb.{capability}",
                provider_key,
                model_id,
                required,
                False,
                frozenset(required),
            )
        )

    return _build_capability_assignments(raw, providers, provider_ids)


def build_catalog_assignments(
    config: dict[str, Any],
    *,
    provider_ids: dict[str, str],
) -> tuple[RequiredAssignment, ...]:
    """Project every declared model capability without assigning production roles."""
    providers = config.get("providers") or {}
    raw = []
    for provider_key, provider in providers.items():
        for model in provider.get("models") or []:
            model_id = model if isinstance(model, str) else model.get("id")
            if not model_id:
                continue
            capabilities = tuple(
                sorted(cap for cap in _declared_capabilities(provider, model_id) if cap in {"chat", "tool", "vision", "embed", "rerank"})
            )
            raw.append(("catalog", provider_key, model_id, capabilities, False, None))
    return _build_capability_assignments(raw, providers, provider_ids)


def _build_capability_assignments(
    raw: list[tuple[str, str, str, tuple[Capability, ...], bool, frozenset[str] | None]],
    providers: dict[str, Any],
    provider_ids: dict[str, str],
) -> tuple[RequiredAssignment, ...]:
    registry = CompatibleProbeRegistry()
    rows: list[RequiredAssignment] = []
    for role_key, provider_key, model_id, capabilities, routing, declared_override in raw:
        provider = providers.get(provider_key)
        provider_id = provider_ids.get(provider_key)
        if not isinstance(provider, dict) or not provider_id:
            raise ValueError(f"{role_key} references a Provider without stable identity")
        normalized_provider = _provider_config(provider)
        declared = declared_override if declared_override is not None else _declared_capabilities(provider, model_id)
        display_name = str(provider.get("display_name") or provider_key)
        for capability in capabilities:
            plan = registry.plan(
                normalized_provider,
                provider_id=provider_id,
                provider_display_name=display_name,
                model_id=model_id,
                capability=capability,
            )
            signature = compute_config_signature(
                plan.transport,
                provider_id=provider_id,
                model_id=model_id,
                capability=capability,
                testability_contract_version=(plan.testability_contract_version),
                probe_contract_version=plan.probe_contract_version,
                probe_execution_policy_version=(plan.probe_execution_policy_version),
            )
            rows.append(
                RequiredAssignment(
                    role_key=role_key,
                    provider_id=provider_id,
                    provider_runtime_key=provider_key,
                    provider_display_name=display_name,
                    model_id=model_id,
                    capability=capability,
                    config_signature=signature,
                    testability_contract_version=(plan.testability_contract_version),
                    probe_contract_version=plan.probe_contract_version,
                    probe_execution_policy_version=(plan.probe_execution_policy_version),
                    routing_critical=routing,
                    declared=capability in declared,
                )
            )
    return tuple(rows)


def compute_team_llm_readiness(
    *,
    expected_version: int,
    assignments: tuple[RequiredAssignment, ...],
    facts: dict[tuple[str, str, Capability, str], ReadinessFact],
    previous_scopes: frozenset[tuple[str, str, Capability, str]] = frozenset(),
    active_acceptances: frozenset[tuple[str, str, Capability, str, str, str, str]] = frozenset(),
    grandfather_scopes: frozenset[tuple[str, str, Capability, str]] = frozenset(),
    baseline_will_invalidate: bool = False,
    external_rows: tuple[ExternalReadinessRow, ...] = (),
) -> ReadinessDecision:
    """Apply the decision matrix without trusting frontend status."""

    rows: list[ReadinessRow] = []
    blocking: list[ReadinessIssue] = []
    warnings: list[ReadinessIssue] = []
    required_acceptances: list[RiskAcceptanceRequest] = []
    for assignment in assignments:
        fact = facts.get(assignment.scope_key, ReadinessFact())
        changed = assignment.scope_key not in previous_scopes
        issue_code: str | None = None
        status: ReadinessStatus = "ready"
        grandfathered = not baseline_will_invalidate and assignment.scope_key in grandfather_scopes

        if not assignment.declared:
            # The sole remaining hard block outside verification: the model never declares this capability in configuration, so routing inevitably fails;
            # applying with acknowledged risk has no valid risk to accept. Humans can explicitly accept every other state (below).
            status = "blocked"
            issue_code = "required_capability_not_declared"
        elif fact.state in {"failed", "stale", "unknown"} and not (fact.state == "unknown" and grandfathered):
            # 2026-08-14: these three states formerly hard-blocked with no override; failed probes, expired evidence,
            # or never-verified models prevented teams from ever saving their own configurations. Evidence from 116 shows the danger: a plain-HTTP
            # frontend crash left the evidence table empty indefinitely, all 17 configurations unknown, and the administrator permanently locked out;
            # the page reported a network failure. The criterion itself can also err: two Doubao models in that batch had
            # `model_identity_mismatch` despite HTTP 200, solely because the returned model string did not exactly match
            # configuration; vendors commonly return versioned IDs.
            #
            # Therefore downgrade to the existing explicit risk-acceptance channel: the page lists issues, and administrators must check
            # each acceptance before applying. Persist `risk_code` to record who accepted which risk and when,
            # while ensuring errors in automated criteria cannot permanently lock people out.
            risk_code = _RISK_CODE_BY_FACT_STATE[fact.state]
            if assignment.acceptance_key_for(risk_code) in active_acceptances:
                status = "risk_accepted"
                issue_code = f"{risk_code}_risk_accepted"
            else:
                status = "risk_acceptance_required"
                issue_code = risk_code
                required_acceptances.append(
                    RiskAcceptanceRequest(assignment=assignment, risk_code=risk_code),
                )
        elif fact.state == "unknown":
            status = "grandfathered"
            issue_code = "migration_baseline_no_evidence"
        elif fact.state == "aged":
            if changed:
                status = "blocked"
                issue_code = "capability_aged_after_change"
            else:
                status = "warning"
                issue_code = "capability_evidence_aged"
        elif fact.state == "not_testable":
            if assignment.routing_critical:
                status = "blocked"
                issue_code = "routing_capability_not_testable"
            elif assignment.acceptance_key_for("capability_not_testable") in active_acceptances:
                status = "risk_accepted"
                issue_code = "capability_not_testable_risk_accepted"
            else:
                status = "risk_acceptance_required"
                issue_code = "capability_not_testable"
                required_acceptances.append(RiskAcceptanceRequest(assignment=assignment))

        if issue_code:
            issue = ReadinessIssue(
                code=issue_code,
                field_path=assignment.role_key,
                role_key=assignment.role_key,
                provider_id=assignment.provider_id,
                model_id=assignment.model_id,
                capability=assignment.capability,
                action_code=(
                    "accept_risk"
                    if status == "risk_acceptance_required"
                    else "reverify"
                    if issue_code
                    in {
                        "capability_unknown",
                        "capability_aged_after_change",
                        "capability_stale",
                    }
                    else "declare_capability"
                    if issue_code == "required_capability_not_declared"
                    else "change_assignment"
                    if status == "blocked"
                    else "review_warning"
                ),
            )
            if status == "blocked":
                blocking.append(issue)
            elif status in {
                "warning",
                "grandfathered",
                "risk_acceptance_required",
            }:
                warnings.append(issue)
        rows.append(
            ReadinessRow(
                assignment=assignment,
                fact=fact,
                status=status,
                changed=changed,
                issue_code=issue_code,
                # Acceptance effectiveness is now expressed directly by status (each risk matches its own risk_code),
                # rather than a separately calculated boolean that could diverge from status.
                acceptance_active=status == "risk_accepted",
                grandfathered=grandfathered,
            )
        )

    # This hash locks the conclusion the user reviewed; a mismatch on recomputation at Apply returns 409
    # readiness_review_stale. Thus include only conclusion-related fields.
    #
    # ⚠ feedback-triage-2026-08-24: customers reported a 409 after adding a model and applying, then returning to
    # the Apply button after rechecking. The hash formerly included `evidence_id`, an evidence-row bookkeeping ID, not a conclusion;
    # users see verified / expired / failed statuses, not `lve_xxx`.
    # New models necessarily run probes, and persisting a probe necessarily creates a fresh evidence_id ⇒ identical conclusions,
    # different hashes ⇒ inevitable 409, potentially repeated after loading the latest configuration and rechecking, producing an endless loop.
    # Remove it. Actual conclusion changes remain protected by `fact_state` and `status`
    # (`verified → aged` changes status to blocked/warning; that 409 is correct);
    # model/configuration changes are covered by `config_signature`, `model_id`, and `declared`.
    semantic_payload = {
        "contract_version": READINESS_CONTRACT_VERSION,
        "expected_version": expected_version,
        "baseline_will_invalidate": baseline_will_invalidate,
        "rows": [
            {
                "role_key": row.assignment.role_key,
                "provider_id": row.assignment.provider_id,
                "model_id": row.assignment.model_id,
                "capability": row.assignment.capability,
                "config_signature": row.assignment.config_signature,
                "declared": row.assignment.declared,
                "fact_state": row.fact.state,
                "status": row.status,
                "acceptance_active": row.acceptance_active,
                "grandfathered": row.grandfathered,
            }
            for row in rows
        ],
        "external_rows": [
            {
                "subject_kind": row.subject_kind,
                "configured": row.configured,
                "status": row.status,
            }
            for row in external_rows
        ],
    }
    draft_hash = hashlib.sha256(canonical_json(semantic_payload).encode()).hexdigest()
    return ReadinessDecision(
        draft_semantic_hash=draft_hash,
        rows=tuple(rows),
        blocking=tuple(blocking),
        warnings=tuple(warnings),
        required_acceptances=tuple(required_acceptances),
        external_rows=external_rows,
        baseline_will_invalidate=baseline_will_invalidate,
        may_apply=not blocking and not required_acceptances,
    )


def external_readiness_rows(
    config: dict[str, Any],
) -> tuple[ExternalReadinessRow, ...]:
    media_configured = bool(((config.get("media") or {}).get("active") or {}).get("image_gen"))
    search_configured = bool(((config.get("search") or {}).get("active") or {}).get("web_search"))
    return (
        ExternalReadinessRow(
            subject_kind="media_image_generation",
            configured=media_configured,
            status=("configured" if media_configured else "unavailable"),
        ),
        ExternalReadinessRow(
            subject_kind="web_search",
            configured=search_configured,
            status=("configured" if search_configured else "unavailable"),
        ),
    )
