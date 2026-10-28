"""Immutable reader-side contracts for Terminal Protocol generic effects.

This module deliberately contains no writer selector or provider dispatch.  It
lets old and rollback readers validate future contract-v2 facts before any
producer is enabled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from bowhead_core.rate_card import (
    RATE_CARD_VERSION,
    SUPPORTED_RATE_CARD_VERSIONS,
)
from bowhead_core.terminal_protocol import (
    EffectClass,
    EffectSettlementOutcome,
    EffectSettlementSource,
    TerminalProtocolFutureVersionError,
    TerminalProtocolMalformedError,
    canonical_digest_v1,
)

EFFECT_CONTRACT_VERSION = 2
SUPPORTED_BILLING_RATE_CARD_VERSIONS = SUPPORTED_RATE_CARD_VERSIONS
if RATE_CARD_VERSION not in SUPPORTED_BILLING_RATE_CARD_VERSIONS:
    raise RuntimeError("current billing rate card is missing from durable reader registry")


class EffectDisclosureKind(StrEnum):
    ACCOUNTING = "accounting"
    INFERENCE_INTERNAL = "inference_internal"
    USER_ACTION = "user_action"


class EffectUsageEvidence(StrEnum):
    AUTHORITATIVE = "authoritative"
    DETERMINISTIC_LOCAL = "deterministic_local"
    UNSUPPORTED = "unsupported"


class EffectIntentDisposition(StrEnum):
    LEGACY = "legacy"
    RECOVERY_ONLY = "recovery_only"


class ToolEffectIdempotency(StrEnum):
    NONE = "none"
    STABLE_TOKEN = "stable_token"
    NON_RETRIABLE = "non_retriable"


@dataclass(frozen=True, slots=True)
class ToolEffectSpec:
    """Bowhead-owned dispatch contract attached to one canonical ToolSpec."""

    effect_class: EffectClass
    idempotency: ToolEffectIdempotency
    effect_contract_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.effect_class, EffectClass) or not isinstance(
            self.idempotency,
            ToolEffectIdempotency,
        ):
            raise TypeError("tool effect contract enums are invalid")
        if self.effect_class is EffectClass.SIDE_EFFECT_POSSIBLE:
            if self.idempotency is ToolEffectIdempotency.NONE:
                raise ValueError("side-effect tool requires an idempotency disposition")
            if not isinstance(self.effect_contract_key, str) or not self.effect_contract_key:
                raise ValueError("side-effect tool requires an effect contract key")
            return
        if self.idempotency is not ToolEffectIdempotency.NONE or self.effect_contract_key is not None:
            raise ValueError("non-mutating tool cannot declare a provider contract")


TOOL_EFFECT_NONE = ToolEffectSpec(
    effect_class=EffectClass.NONE,
    idempotency=ToolEffectIdempotency.NONE,
)
TOOL_EFFECT_READ_ONLY = ToolEffectSpec(
    effect_class=EffectClass.READ_ONLY,
    idempotency=ToolEffectIdempotency.NONE,
)


@dataclass(frozen=True, slots=True)
class EffectProviderContract:
    key: str
    contract_version: int
    disclosure_kind: EffectDisclosureKind
    usage_evidence: EffectUsageEvidence
    outcome_classifier_key: str
    probe_adapter_key: str
    allowed_settlement_sources: frozenset[EffectSettlementSource]
    allowed_outcomes: frozenset[EffectSettlementOutcome]
    allows_control_probe: bool
    allows_manual_settlement: bool


@dataclass(frozen=True, slots=True)
class EffectExecutionContract:
    disposition: EffectIntentDisposition
    contract_version: int | None
    billing_rate_card_version: str | None


@dataclass(frozen=True, slots=True)
class EffectProbeEvidence:
    outcome: EffectSettlementOutcome
    settlement_code: str
    evidence_digest: str
    object_version_digest: str
    audit_object_ref: str
    provider_external_ref_digest: str
    provider_external_ref_key_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, EffectSettlementOutcome):
            raise TypeError("effect probe outcome is invalid")
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.settlement_code,
                self.evidence_digest,
                self.object_version_digest,
                self.audit_object_ref,
                self.provider_external_ref_digest,
                self.provider_external_ref_key_version,
            )
        ):
            raise ValueError("effect probe evidence is invalid")


@dataclass(frozen=True, slots=True)
class ClassifiedToolEffectResult:
    """Display result paired with Bowhead-owned, versioned outcome evidence."""

    result: object
    evidence: EffectProbeEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EffectProbeEvidence):
            raise TypeError("classified tool effect evidence is invalid")


_ALL_OUTCOMES = frozenset(EffectSettlementOutcome)
_PROVIDER_ONLY = frozenset({EffectSettlementSource.PROVIDER})
_PROVIDER_OR_MANUAL = frozenset({EffectSettlementSource.PROVIDER, EffectSettlementSource.MANUAL})

_PROVIDER_CONTRACTS = MappingProxyType(
    {
        "approval_decision/v1": EffectProviderContract(
            key="approval_decision/v1",
            contract_version=EFFECT_CONTRACT_VERSION,
            disclosure_kind=EffectDisclosureKind.USER_ACTION,
            usage_evidence=EffectUsageEvidence.UNSUPPORTED,
            outcome_classifier_key="approval_decision_result/v1",
            probe_adapter_key="approval_decision_projection/v1",
            allowed_settlement_sources=_PROVIDER_ONLY,
            allowed_outcomes=_ALL_OUTCOMES,
            allows_control_probe=False,
            allows_manual_settlement=False,
        ),
        "credit_ledger/v1": EffectProviderContract(
            key="credit_ledger/v1",
            contract_version=EFFECT_CONTRACT_VERSION,
            disclosure_kind=EffectDisclosureKind.ACCOUNTING,
            usage_evidence=EffectUsageEvidence.DETERMINISTIC_LOCAL,
            outcome_classifier_key="credit_ledger_row/v1",
            probe_adapter_key="credit_ledger_row/v1",
            allowed_settlement_sources=_PROVIDER_ONLY,
            allowed_outcomes=frozenset(
                {
                    EffectSettlementOutcome.CONFIRMED_APPLIED,
                    EffectSettlementOutcome.CONFIRMED_NOT_APPLIED,
                }
            ),
            allows_control_probe=False,
            allows_manual_settlement=False,
        ),
        "credit_ledger/v2": EffectProviderContract(
            key="credit_ledger/v2",
            contract_version=EFFECT_CONTRACT_VERSION,
            disclosure_kind=EffectDisclosureKind.ACCOUNTING,
            usage_evidence=EffectUsageEvidence.DETERMINISTIC_LOCAL,
            outcome_classifier_key="credit_ledger_row/v1",
            probe_adapter_key="credit_ledger_row/v1",
            allowed_settlement_sources=_PROVIDER_ONLY,
            allowed_outcomes=frozenset(
                {
                    EffectSettlementOutcome.CONFIRMED_APPLIED,
                    EffectSettlementOutcome.CONFIRMED_NOT_APPLIED,
                }
            ),
            allows_control_probe=False,
            allows_manual_settlement=False,
        ),
        "llm_provider/v1": EffectProviderContract(
            key="llm_provider/v1",
            contract_version=EFFECT_CONTRACT_VERSION,
            disclosure_kind=EffectDisclosureKind.INFERENCE_INTERNAL,
            usage_evidence=EffectUsageEvidence.AUTHORITATIVE,
            outcome_classifier_key="llm_provider_receipt/v1",
            probe_adapter_key="llm_provider_receipt/v1",
            allowed_settlement_sources=_PROVIDER_ONLY,
            allowed_outcomes=_ALL_OUTCOMES,
            # Generic probe/manual control stays disabled. D2b.3b admits this
            # provider only through its atomic settlement+usage Store seam.
            allows_control_probe=False,
            allows_manual_settlement=False,
        ),
        "tool_external/v1": EffectProviderContract(
            key="tool_external/v1",
            contract_version=EFFECT_CONTRACT_VERSION,
            disclosure_kind=EffectDisclosureKind.USER_ACTION,
            usage_evidence=EffectUsageEvidence.UNSUPPORTED,
            outcome_classifier_key="tool_external_result/v1",
            probe_adapter_key="tool_external_probe/v1",
            allowed_settlement_sources=_PROVIDER_OR_MANUAL,
            allowed_outcomes=_ALL_OUTCOMES,
            allows_control_probe=True,
            allows_manual_settlement=True,
        ),
    }
)
_BILLING_PROVIDER_VERSIONS = frozenset(key.removeprefix("credit_ledger/") for key in _PROVIDER_CONTRACTS if key.startswith("credit_ledger/"))
if _BILLING_PROVIDER_VERSIONS != SUPPORTED_BILLING_RATE_CARD_VERSIONS:
    raise RuntimeError(
        "durable billing provider registry and rate-card registry differ",
    )


def effect_provider_contract_keys() -> tuple[str, ...]:
    """Return the exact immutable reader registry manifest."""

    return tuple(sorted(_PROVIDER_CONTRACTS))


def effect_control_identity_digest(
    *,
    attempt_id: str,
    provider_contract_key: str,
    source: str,
    outcome: str,
    settlement_code: str,
    evidence_digest: str,
    object_version_digest: str,
    actor_audit_ref: str,
    audit_object_ref: str | None,
    provider_external_ref_digest: str | None,
    provider_external_ref_key_version: str | None,
) -> str:
    """Bind every exact contract-v2 control fact without new schema."""

    values = (
        attempt_id,
        provider_contract_key,
        source,
        outcome,
        settlement_code,
        evidence_digest,
        object_version_digest,
        actor_audit_ref,
    )
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("effect control identity is incomplete")
    optional = (
        audit_object_ref,
        provider_external_ref_digest,
        provider_external_ref_key_version,
    )
    if any(value is not None and (not isinstance(value, str) or not value) for value in optional) or (
        (provider_external_ref_digest is None) != (provider_external_ref_key_version is None)
    ):
        raise ValueError("effect control identity reference is malformed")
    return canonical_digest_v1(
        {
            "attempt_id": attempt_id,
            "actor_audit_ref": actor_audit_ref,
            "audit_object_ref": audit_object_ref,
            "effect_contract_version": EFFECT_CONTRACT_VERSION,
            "evidence_digest": evidence_digest,
            "object_version_digest": object_version_digest,
            "outcome": outcome,
            "provider_contract_key": provider_contract_key,
            "provider_external_ref_digest": provider_external_ref_digest,
            "provider_external_ref_key_version": (provider_external_ref_key_version),
            "settlement_code": settlement_code,
            "source": source,
        }
    )


def get_effect_provider_contract(key: str) -> EffectProviderContract:
    if not isinstance(key, str) or not key:
        raise TerminalProtocolMalformedError("effect provider contract key is malformed")
    contract = _PROVIDER_CONTRACTS.get(key)
    if contract is not None:
        return contract
    family, separator, version = key.rpartition("/v")
    if separator and family and version.isdigit() and int(version) > 1:
        raise TerminalProtocolFutureVersionError("effect provider contract is from a future version")
    raise TerminalProtocolMalformedError("effect provider contract is unknown")


def resolve_effect_execution_contract(
    execution_intent: Mapping[str, object],
) -> EffectExecutionContract:
    if not isinstance(execution_intent, Mapping):
        raise TerminalProtocolMalformedError("effect execution intent is malformed")
    has_contract = "effect_contract_version" in execution_intent
    has_rate_card = "billing_rate_card_version" in execution_intent
    if not has_contract and not has_rate_card:
        return EffectExecutionContract(
            disposition=EffectIntentDisposition.LEGACY,
            contract_version=None,
            billing_rate_card_version=None,
        )
    if has_contract != has_rate_card:
        raise TerminalProtocolMalformedError("effect execution intent version pair is incomplete")
    version = execution_intent["effect_contract_version"]
    rate_card = execution_intent["billing_rate_card_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise TerminalProtocolMalformedError("effect contract version is malformed")
    if version > EFFECT_CONTRACT_VERSION:
        raise TerminalProtocolFutureVersionError("effect contract version is from the future")
    if version != EFFECT_CONTRACT_VERSION:
        raise TerminalProtocolMalformedError("effect contract version is unsupported")
    if not isinstance(rate_card, str) or not rate_card:
        raise TerminalProtocolMalformedError("billing rate card version is malformed")
    if rate_card not in SUPPORTED_BILLING_RATE_CARD_VERSIONS:
        raise TerminalProtocolMalformedError("billing rate card version is unknown")
    return EffectExecutionContract(
        disposition=EffectIntentDisposition.RECOVERY_ONLY,
        contract_version=version,
        billing_rate_card_version=rate_card,
    )
