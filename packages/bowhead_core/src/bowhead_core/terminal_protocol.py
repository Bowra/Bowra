"""Terminal Protocol v1 的纯领域合同。

本模块不访问数据库、网络或用户文案。legacy consumer 的历史终态集合被显式分开，
v1 则只允许带 receipt identity 的 root ``done`` 或声明 durability 的 root transport
``error`` 关闭流。任何 future/malformed v1 输入都 fail closed。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from bowhead_core.chat_turn_source import (
    ChatTurnUserSource,
    chat_turn_user_source_projection,
)

TERMINAL_PROTOCOL_VERSION = 1
TERMINAL_READER_RESERVED_EVENT_TYPES = frozenset(
    {
        "turn.effect_disclosure_settled",
    }
)
TERMINAL_PROTOCOL_ENUM_REGISTRY_VERSION = 1
# Web markers cross the JavaScript boundary as decimal strings but originate
# from a JSON-safe integer. Keep the accepted lookup range exact in every layer.
WEB_ACTION_MAX_EXECUTION_GENERATION = 9_007_199_254_740_991
TERMINAL_PROTOCOL_TABLES = frozenset(
    {
        "session_lifecycle_operations",
        "session_lifecycle_targets",
        "terminal_auth_scope_epochs",
        "terminal_protocol_deployment_attestations",
        "terminal_protocol_writer_lease",
        "turn_control_decisions",
        "turn_decision_effects",
        "turn_delivery_intents",
        "turn_effect_attempts",
        "turn_effect_evidence_links",
        "turn_effect_retention_member_keys",
        "turn_effect_retention_tombstones",
        "turn_effect_settlements",
        "turn_event_proposals",
        "turn_generations",
        "turn_recovery_records",
        "turn_terminal_candidate_events",
        "turn_terminal_candidate_variants",
        "turn_terminal_candidates",
        "turn_terminal_deletion_ledgers",
        "turn_terminal_receipts",
        "turn_terminal_session_mutations",
    }
)

_EXPECTED_ENUM_NAMES = frozenset(
    {
        "actor_scope",
        "auth_fence_scope_kind",
        "candidate_origin",
        "candidate_outcome",
        "candidate_state",
        "decision_state",
        "delivery_state",
        "effect_class",
        "effect_retention_state",
        "effect_settlement_outcome",
        "effect_settlement_source",
        "effect_state",
        "event_proposal_state",
        "evidence_retention_state",
        "final_answer_mode",
        "generation_kind",
        "generation_state",
        "lifecycle_discovery_source_kind",
        "phase_scope",
        "recovery_kind",
        "recovery_resolution",
        "recovery_scope",
        "recovery_state",
        "root_durability",
        "session_lifecycle_action",
        "session_lifecycle_state",
        "session_lifecycle_target_state",
        "session_mutation_disposition",
        "session_recovery_lock_reason",
        "session_variant",
        "steer_buffer_state",
        "terminal_status",
        "writer_admission_state",
    }
)

_EXPECTED_REASON_CODES = (
    "worker_start_failed",
    "terminal_producer_timeout",
    "terminal_projection_missing",
    "terminal_projection_not_committed",
    "terminal_commit_outcome_unknown",
    "claim_owner_lost",
    "effect_outcome_unknown",
    "terminal_dependency_missing",
    "terminal_protocol_malformed",
    "terminal_protocol_future_version",
    "clarification_dismissed",
    "clarification_resolved",
    "approval_rejected",
    "user_cancelled",
    "decision_expired",
    "access_revoked",
)


class TerminalProtocolError(RuntimeError):
    """Terminal Protocol reader/validator 的安全基类。"""


class TerminalProtocolMalformedError(TerminalProtocolError):
    """持久化 v1 shape 与冻结合同不一致。"""


class TerminalProtocolFutureVersionError(TerminalProtocolError):
    """持久化协议版本高于当前 reader 能力。"""


@dataclass(frozen=True, slots=True)
class TerminalScope:
    """一个不可歧义的 Terminal Protocol v1 generation identity。"""

    team_id: str
    session_id: str
    turn_id: str
    generation: int
    protocol_version: int = TERMINAL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (self.team_id, self.session_id, self.turn_id)):
            raise ValueError("terminal scope identities must be non-empty")
        if not _is_int(self.generation) or self.generation < 1:
            raise ValueError("terminal scope generation must be positive")
        if not _is_int(self.protocol_version) or self.protocol_version != TERMINAL_PROTOCOL_VERSION:
            raise ValueError("terminal scope protocol version must be 1")


@dataclass(frozen=True, slots=True)
class AdmissionContext:
    """Gateway preflight 后交给 shared Session admission CAS 的不可变 fence。"""

    team_id: str
    session_id: str
    request_id: str
    surface: str
    cohort: str
    actor: str
    writer_instance_id: str
    writer_lease_epoch: int
    writer_admission_epoch: int
    rollout_config_generation: str
    auth_fence_vector_digest: str

    def __post_init__(self) -> None:
        text_fields = (
            self.team_id,
            self.session_id,
            self.request_id,
            self.surface,
            self.cohort,
            self.actor,
            self.writer_instance_id,
            self.rollout_config_generation,
            self.auth_fence_vector_digest,
        )
        if any(not isinstance(value, str) or not value for value in text_fields):
            raise ValueError("terminal admission context fields must be non-empty")
        if len(self.surface) > 64 or len(self.cohort) > 256:
            raise ValueError("terminal admission snapshot exceeds schema limits")
        if (
            not _is_int(self.writer_lease_epoch)
            or self.writer_lease_epoch < 1
            or not _is_int(self.writer_admission_epoch)
            or self.writer_admission_epoch < 1
        ):
            raise ValueError("terminal admission writer epochs must be positive")


@dataclass(frozen=True, slots=True)
class TerminalAdmissionRequest:
    """S1 admission 所需的完整请求；execution_intent 会在入库前复制并 canonicalize。"""

    context: AdmissionContext
    turn_id: str
    user_text: str
    execution_intent: Mapping[str, object]
    presentation_locale: str
    message_catalog_version: int
    allow_steer: bool = True
    user_source: ChatTurnUserSource | None = None
    create_session_if_missing: bool = False
    require_existing_v1_history: bool = False
    expected_execution_generation: int | None = None
    user_message_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, AdmissionContext):
            raise TypeError("terminal admission context is invalid")
        if not isinstance(self.turn_id, str) or not self.turn_id:
            raise ValueError("terminal admission turn id must be non-empty")
        if not isinstance(self.user_text, str):
            raise TypeError("terminal admission user text must be a string")
        if not isinstance(self.execution_intent, Mapping):
            raise TypeError("terminal admission execution intent must be a mapping")
        if not isinstance(self.presentation_locale, str) or not self.presentation_locale or len(self.presentation_locale) > 35:
            raise ValueError("terminal admission locale is invalid")
        if not _is_int(self.message_catalog_version) or self.message_catalog_version < 1:
            raise ValueError("terminal admission catalog version must be positive")
        if not isinstance(self.allow_steer, bool):
            raise TypeError("terminal admission allow_steer must be a boolean")
        steer_identity = (
            self.expected_execution_generation,
            self.user_message_id,
        )
        if any(value is None for value in steer_identity) and any(value is not None for value in steer_identity):
            raise ValueError("terminal steer identity is incomplete")
        if self.expected_execution_generation is not None:
            if (
                not _is_int(self.expected_execution_generation)
                or self.expected_execution_generation < 1
                or self.expected_execution_generation > WEB_ACTION_MAX_EXECUTION_GENERATION
                or not isinstance(self.user_message_id, str)
                or not self.user_message_id
                or len(self.user_message_id) > 128
            ):
                raise ValueError("terminal steer identity is invalid")
            if not self.allow_steer or self.user_source is not None:
                raise ValueError("terminal steer identity is not allowed for start")
        if self.user_source is not None:
            if not isinstance(self.user_source, ChatTurnUserSource):
                raise TypeError("terminal admission user source is invalid")
            if self.allow_steer:
                raise ValueError("typed terminal start cannot allow steer")
            if (
                self.user_source.turn_id != self.turn_id
                or self.user_source.session_id != self.context.session_id
                or self.user_source.user_id != self.context.actor
                or self.user_source.user_text != self.user_text
            ):
                raise ValueError("terminal admission user source identity changed")
            source_envelope = chat_turn_user_source_projection(self.user_source)
            source_attachments = self.execution_intent.get("attachments")
            source_attachment_refs = self.execution_intent.get("attachment_refs")
            source_parts = self.execution_intent.get("parts")
            if (
                not isinstance(source_attachments, list)
                or not isinstance(source_attachment_refs, list)
                or (source_parts is not None and not isinstance(source_parts, list))
            ):
                raise ValueError("terminal admission user source envelope changed")
            request_envelope = {
                "attachment_ids": source_attachments,
                "attachment_refs": source_attachment_refs,
                "parts": source_parts,
                "session_id": self.context.session_id,
                "turn_id": self.turn_id,
                "user_id": self.context.actor,
                "user_text": self.execution_intent.get("text"),
                "workspace_id": self.execution_intent.get("workspace_id"),
            }
            if canonical_json_v1(request_envelope) != canonical_json_v1(source_envelope):
                raise ValueError("terminal admission user source envelope changed")
        if not isinstance(self.create_session_if_missing, bool):
            raise TypeError("terminal admission Session creation flag is invalid")
        if self.create_session_if_missing and self.user_source is None:
            raise ValueError("terminal admission Session creation requires a user source")
        if not isinstance(self.require_existing_v1_history, bool):
            raise TypeError("terminal admission protocol history fence is invalid")
        if self.create_session_if_missing and self.require_existing_v1_history:
            raise ValueError("new Session cannot require v1 history")


@dataclass(frozen=True, slots=True)
class GenerationClaimContext:
    """opened generation -> running 的唯一 execution ownership CAS input。"""

    scope: TerminalScope
    expected_generation_row_version: int
    expected_session_claim_epoch: int
    writer_lease_epoch: int
    owner_instance_id: str
    auth_principal_ref: str | None = None
    current_authz_snapshot_digest: str | None = None
    require_open_writer: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("generation claim scope is invalid")
        if not _is_int(self.expected_generation_row_version) or self.expected_generation_row_version < 1:
            raise ValueError("expected generation row version must be positive")
        if not _is_int(self.expected_session_claim_epoch) or self.expected_session_claim_epoch < 0:
            raise ValueError("expected Session claim epoch must be non-negative")
        if (
            not _is_int(self.writer_lease_epoch)
            or self.writer_lease_epoch < 1
            or not isinstance(self.owner_instance_id, str)
            or not self.owner_instance_id
        ):
            raise ValueError("generation claim writer identity is invalid")
        if (self.auth_principal_ref is None) != (self.current_authz_snapshot_digest is None):
            raise ValueError("generation claim authorization fence is incomplete")
        if self.auth_principal_ref is not None and (not self.auth_principal_ref or not self.current_authz_snapshot_digest):
            raise ValueError("generation claim authorization fence is invalid")
        if not isinstance(self.require_open_writer, bool):
            raise TypeError("generation claim writer state requirement is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionGenerationContext:
    """claim commit 后才可构造的 execution capability。"""

    scope: TerminalScope
    owner_instance_id: str
    claim_epoch: int
    generation_row_version: int
    writer_lease_epoch: int
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("execution generation scope is invalid")
        if (
            not isinstance(self.owner_instance_id, str)
            or not self.owner_instance_id
            or not isinstance(self.lease_expires_at, str)
            or not self.lease_expires_at
        ):
            raise ValueError("execution generation ownership fields must be non-empty")
        if (
            not _is_int(self.claim_epoch)
            or self.claim_epoch < 1
            or not _is_int(self.generation_row_version)
            or self.generation_row_version < 1
            or not _is_int(self.writer_lease_epoch)
            or self.writer_lease_epoch < 1
        ):
            raise ValueError("execution generation epochs must be positive")


class DecisionSourceKind(StrEnum):
    CLARIFICATION = "clarification"
    APPROVAL = "approval"
    SYSTEM_CONTROL = "system_control"


def is_narrow_system_decision(context: DecisionContext) -> bool:
    """Validate the only decision identities that carry system authority."""

    if not isinstance(context, DecisionContext):
        return False
    principals = context.principals
    same_human_subject = principals.requester_ref == principals.effect_principal_ref and not principals.requester_ref.startswith("system:")
    if not same_human_subject:
        return False
    if context.source_kind is DecisionSourceKind.SYSTEM_CONTROL and context.action is DecisionAction.REVOKE:
        return principals.actor_ref == "system:authorization-revocation" and context.source_id == (
            f"{context.scope.turn_id}:{context.scope.generation}:control"
        )
    expected_expiry_actor = {
        DecisionSourceKind.CLARIFICATION: ("system:clarification-expiry"),
        DecisionSourceKind.APPROVAL: "system:approval-expiry",
    }.get(context.source_kind)
    return context.action is DecisionAction.EXPIRE and expected_expiry_actor is not None and principals.actor_ref == expected_expiry_actor


def is_web_cancel_decision(context: DecisionContext) -> bool:
    """Validate the actor-owned control identity emitted by the Web stop API."""

    if not isinstance(context, DecisionContext):
        return False
    principals = context.principals
    return (
        context.source_kind is DecisionSourceKind.SYSTEM_CONTROL
        and context.action is DecisionAction.CANCEL
        and context.source_id == (f"{context.scope.turn_id}:{context.scope.generation}:control")
        and principals.actor_ref == "system:web-cancel"
        and principals.requester_ref == principals.effect_principal_ref
        and not principals.requester_ref.startswith("system:")
    )


class DecisionAction(StrEnum):
    RESUME = "resume"
    APPROVE = "approve"
    RESOLVE_WITHOUT_RESUME = "resolve_without_resume"
    DISMISS = "dismiss"
    REJECT = "reject"
    CANCEL = "cancel"
    EXPIRE = "expire"
    REVOKE = "revoke"


class DecisionState(StrEnum):
    PENDING = "pending"
    DECISION_RECORDED = "decision_recorded"
    GENERATION_OPENED = "generation_opened"
    TERMINAL_CONTROL_COMMITTED = "terminal_control_committed"


class DecisionOpenDisposition(StrEnum):
    OPENED = "opened"
    REPLAY = "replay"
    BUSY = "busy"
    ALREADY_TERMINAL = "already_terminal"
    ACCESS_REVOKED = "access_revoked"
    RECOVERY_LOCKED = "recovery_locked"


_DECISION_SOURCE_TRANSITIONS: dict[
    tuple[DecisionSourceKind, DecisionAction],
    tuple[str, str | None],
] = {
    (DecisionSourceKind.CLARIFICATION, DecisionAction.RESUME): ("pending", "resolved"),
    (DecisionSourceKind.CLARIFICATION, DecisionAction.RESOLVE_WITHOUT_RESUME): (
        "pending",
        "resolved",
    ),
    (DecisionSourceKind.CLARIFICATION, DecisionAction.DISMISS): ("pending", "dismissed"),
    (DecisionSourceKind.CLARIFICATION, DecisionAction.EXPIRE): ("pending", "expired"),
    (DecisionSourceKind.APPROVAL, DecisionAction.APPROVE): ("pending", "approved"),
    (DecisionSourceKind.APPROVAL, DecisionAction.REJECT): ("pending", "rejected"),
    (DecisionSourceKind.APPROVAL, DecisionAction.EXPIRE): ("pending", "expired"),
    (DecisionSourceKind.SYSTEM_CONTROL, DecisionAction.CANCEL): ("pending", None),
    (DecisionSourceKind.SYSTEM_CONTROL, DecisionAction.EXPIRE): ("pending", None),
    (DecisionSourceKind.SYSTEM_CONTROL, DecisionAction.REVOKE): ("pending", None),
}

_SYSTEM_CONTROL_DENIAL_REASON_CODES = frozenset(
    {
        "access_revoked",
        "dependency_missing",
        "entitlement_denied",
    }
)


@dataclass(frozen=True, slots=True)
class DecisionPrincipalSet:
    """Keep requester, decision actor, and effect principal unambiguous."""

    requester_ref: str
    actor_ref: str
    effect_principal_ref: str

    def __post_init__(self) -> None:
        values = (self.requester_ref, self.actor_ref, self.effect_principal_ref)
        if any(not isinstance(value, str) or not value or len(value) > 512 for value in values):
            raise ValueError("decision principal identity is invalid")


@dataclass(frozen=True, slots=True)
class DecisionPreflightSnapshot:
    """Persisted pause truth and current authorization vector for one decision."""

    scope: TerminalScope
    generation_transition_epoch: int
    prior_receipt_id: str
    authz_snapshot_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("decision preflight scope is invalid")
        if not _is_int(self.generation_transition_epoch) or self.generation_transition_epoch < 1:
            raise ValueError("decision preflight transition epoch is invalid")
        if any(not isinstance(value, str) or not value for value in (self.prior_receipt_id, self.authz_snapshot_digest)):
            raise ValueError("decision preflight identity is invalid")


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """A stable decision identity plus audit-only authorization snapshot."""

    scope: TerminalScope
    decision_id: str
    source_kind: DecisionSourceKind
    source_id: str
    action: DecisionAction
    principals: DecisionPrincipalSet
    authz_snapshot_digest: str
    prior_receipt_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("decision scope is invalid")
        if not isinstance(self.source_kind, DecisionSourceKind) or not isinstance(self.action, DecisionAction):
            raise TypeError("decision enum is invalid")
        text_fields = (
            self.decision_id,
            self.source_id,
            self.authz_snapshot_digest,
            self.prior_receipt_id,
        )
        if any(not isinstance(value, str) or not value or len(value) > 512 for value in text_fields):
            raise ValueError("decision identity is invalid")

    @property
    def actor_ref(self) -> str:
        return self.principals.actor_ref


@dataclass(frozen=True, slots=True)
class DecisionSideEffectPlan:
    """An idempotent post-decision projection registered with the decision CAS."""

    effect_id: str
    effect_kind: str
    target_ref: str
    payload_json: str
    payload_digest: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value or len(value) > 512
            for value in (self.effect_id, self.effect_kind, self.target_ref, self.payload_digest)
        ):
            raise ValueError("decision side-effect identity is invalid")
        if not isinstance(self.payload_json, str) or not self.payload_json:
            raise ValueError("decision side-effect payload is invalid")
        try:
            payload = json.loads(self.payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("decision side-effect payload must be JSON") from exc
        if not isinstance(payload, Mapping) or canonical_json_v1(dict(payload)) != self.payload_json:
            raise ValueError("decision side-effect payload must be a canonical object")
        if canonical_digest_v1(dict(payload)) != self.payload_digest:
            raise ValueError("decision side-effect payload digest changed")

    @classmethod
    def from_payload(
        cls,
        *,
        decision_id: str,
        effect_kind: str,
        target_ref: str,
        payload: Mapping[str, object],
    ) -> DecisionSideEffectPlan:
        effect_id = stable_decision_effect_id(
            decision_id=decision_id,
            effect_kind=effect_kind,
            target_ref=target_ref,
        )
        canonical = canonical_json_v1(dict(payload))
        return cls(
            effect_id=effect_id,
            effect_kind=effect_kind,
            target_ref=target_ref,
            payload_json=canonical,
            payload_digest=canonical_digest_v1(dict(payload)),
        )


@dataclass(frozen=True, slots=True)
class DecisionRecordRequest:
    context: DecisionContext
    decision_payload_digest: str
    expected_source_state: str
    target_source_state: str | None
    source_values_json: str | None = None
    side_effects: tuple[DecisionSideEffectPlan, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.context, DecisionContext):
            raise TypeError("decision record context is invalid")
        if not isinstance(self.decision_payload_digest, str) or not self.decision_payload_digest:
            raise ValueError("decision payload digest is invalid")
        if not isinstance(self.expected_source_state, str) or not self.expected_source_state:
            raise ValueError("decision source state is invalid")
        if self.target_source_state is not None and (not isinstance(self.target_source_state, str) or not self.target_source_state):
            raise ValueError("decision target source state is invalid")
        if self.source_values_json is not None:
            if not isinstance(self.source_values_json, str) or not self.source_values_json:
                raise ValueError("decision source values are invalid")
            try:
                decoded = json.loads(self.source_values_json)
            except json.JSONDecodeError as exc:
                raise ValueError("decision source values must be JSON") from exc
            if not isinstance(decoded, Mapping):
                raise ValueError("decision source values must be an object")
        if any(not isinstance(effect, DecisionSideEffectPlan) for effect in self.side_effects):
            raise TypeError("decision side-effect plan is invalid")
        if len({effect.effect_id for effect in self.side_effects}) != len(self.side_effects):
            raise ValueError("decision side-effect identity is duplicated")
        if any(
            effect.effect_id
            != stable_decision_effect_id(
                decision_id=self.context.decision_id,
                effect_kind=effect.effect_kind,
                target_ref=effect.target_ref,
            )
            for effect in self.side_effects
        ):
            raise ValueError("decision side-effect identity changed")
        validate_decision_source_transition(self)
        expected_payload_digest = canonical_decision_payload_digest(self)
        if self.decision_payload_digest != expected_payload_digest:
            raise ValueError("decision payload digest does not bind the canonical source projection")


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    context: DecisionContext
    state: DecisionState
    decision_digest: str
    target_generation: int | None
    replayed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.context, DecisionContext) or not isinstance(self.state, DecisionState):
            raise TypeError("recorded decision is invalid")
        if not isinstance(self.decision_digest, str) or not self.decision_digest:
            raise ValueError("recorded decision digest is invalid")
        if self.target_generation is not None and (not _is_int(self.target_generation) or self.target_generation < 1):
            raise ValueError("recorded decision target generation is invalid")
        if not isinstance(self.replayed, bool):
            raise TypeError("recorded decision replay flag is invalid")


@dataclass(frozen=True, slots=True)
class PendingDecisionRecovery:
    decision: RecordedDecision
    generation_transition_epoch: int
    source_projection_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RecordedDecision):
            raise TypeError("pending decision recovery is invalid")
        if self.decision.state is not DecisionState.DECISION_RECORDED:
            raise ValueError("pending decision must be unconsumed")
        if not _is_int(self.generation_transition_epoch) or self.generation_transition_epoch < 1:
            raise ValueError("pending decision transition epoch is invalid")
        if not isinstance(self.source_projection_digest, str) or not self.source_projection_digest:
            raise ValueError("pending decision source digest is invalid")


@dataclass(frozen=True, slots=True)
class ClaimedDecisionSideEffect:
    plan: DecisionSideEffectPlan
    decision_id: str
    claim_owner: str
    claim_epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.plan, DecisionSideEffectPlan):
            raise TypeError("claimed decision side-effect plan is invalid")
        if any(not isinstance(value, str) or not value for value in (self.decision_id, self.claim_owner)):
            raise ValueError("claimed decision side-effect identity is invalid")
        if not _is_int(self.claim_epoch) or self.claim_epoch < 1:
            raise ValueError("claimed decision side-effect epoch is invalid")


@dataclass(frozen=True, slots=True)
class ResumeGenerationRequest:
    decision: RecordedDecision
    execution_intent_id: str
    execution_intent_digest: str
    execution_intent: Mapping[str, object]
    expected_generation_transition_epoch: int
    expected_session_claim_epoch: int
    writer_instance_id: str
    writer_lease_epoch: int
    current_authz_snapshot_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RecordedDecision):
            raise TypeError("resume decision is invalid")
        if self.decision.context.action not in {DecisionAction.RESUME, DecisionAction.APPROVE}:
            raise ValueError("decision action cannot open an execution generation")
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.execution_intent_id,
                self.execution_intent_digest,
                self.writer_instance_id,
                self.current_authz_snapshot_digest,
            )
        ):
            raise ValueError("resume execution intent is invalid")
        if not isinstance(self.execution_intent, Mapping):
            raise TypeError("resume execution intent envelope is invalid")
        if canonical_digest_v1(dict(self.execution_intent)) != self.execution_intent_digest:
            raise ValueError("resume execution intent digest mismatch")
        if (
            not _is_int(self.expected_generation_transition_epoch)
            or self.expected_generation_transition_epoch < 0
            or not _is_int(self.expected_session_claim_epoch)
            or self.expected_session_claim_epoch < 0
            or not _is_int(self.writer_lease_epoch)
            or self.writer_lease_epoch < 1
        ):
            raise ValueError("resume generation epoch is invalid")


@dataclass(frozen=True, slots=True)
class ResumeGenerationResult:
    disposition: DecisionOpenDisposition
    decision: RecordedDecision
    scope: TerminalScope | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, DecisionOpenDisposition) or not isinstance(
            self.decision,
            RecordedDecision,
        ):
            raise TypeError("resume generation result is invalid")
        if self.scope is not None and not isinstance(self.scope, TerminalScope):
            raise TypeError("resume generation scope is invalid")
        if self.disposition in {DecisionOpenDisposition.OPENED, DecisionOpenDisposition.REPLAY} and self.scope is None:
            raise ValueError("opened resume result requires a scope")


@dataclass(frozen=True, slots=True)
class ControlGenerationRequest:
    decision: RecordedDecision
    status: TerminalStatus
    reason_code: str
    message_key: str
    writer_instance_id: str
    writer_lease_epoch: int
    writer_admission_epoch: int
    current_authz_snapshot_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RecordedDecision):
            raise TypeError("control decision is invalid")
        if self.decision.context.action in {DecisionAction.RESUME, DecisionAction.APPROVE}:
            raise ValueError("execution decision cannot commit a control generation")
        if not isinstance(self.status, TerminalStatus) or self.status not in {
            TerminalStatus.CANCELLED,
            TerminalStatus.ABORTED,
            TerminalStatus.FAILED,
        }:
            raise ValueError("control terminal status is invalid")
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.reason_code,
                self.message_key,
                self.writer_instance_id,
                self.current_authz_snapshot_digest,
            )
        ):
            raise ValueError("control terminal metadata is invalid")
        if (
            not _is_int(self.writer_lease_epoch)
            or self.writer_lease_epoch < 1
            or not _is_int(self.writer_admission_epoch)
            or self.writer_admission_epoch < 1
        ):
            raise ValueError("control writer fence is invalid")
        expected = {
            DecisionAction.RESOLVE_WITHOUT_RESUME: (
                TerminalStatus.CANCELLED,
                "clarification_resolved",
                "turn.clarification_resolved_for_next_turn",
            ),
            DecisionAction.DISMISS: (
                TerminalStatus.CANCELLED,
                "clarification_dismissed",
                "turn.clarification_dismissed",
            ),
            DecisionAction.REJECT: (
                TerminalStatus.CANCELLED,
                "approval_rejected",
                "turn.approval_rejected",
            ),
            DecisionAction.CANCEL: (
                TerminalStatus.CANCELLED,
                "user_cancelled",
                "turn.cancelled",
            ),
            DecisionAction.EXPIRE: (
                TerminalStatus.ABORTED,
                "decision_expired",
                "turn.decision_expired",
            ),
            DecisionAction.REVOKE: (
                TerminalStatus.ABORTED,
                "access_revoked",
                "turn.access_revoked",
            ),
        }.get(self.decision.context.action)
        if (
            self.decision.context.action is DecisionAction.REVOKE
            and self.decision.context.source_kind is DecisionSourceKind.SYSTEM_CONTROL
            and (
                self.status,
                self.reason_code,
                self.message_key,
            )
            == (
                TerminalStatus.FAILED,
                "terminal_dependency_missing",
                "turn.failed",
            )
        ):
            expected = (
                TerminalStatus.FAILED,
                "terminal_dependency_missing",
                "turn.failed",
            )
        if expected != (self.status, self.reason_code, self.message_key):
            raise ValueError("control terminal mapping is invalid")


@dataclass(frozen=True, slots=True)
class TerminalFollowupCapability:
    certified: bool
    adapter_generation: str

    def __post_init__(self) -> None:
        if not isinstance(self.certified, bool):
            raise TypeError("follow-up certification is invalid")
        if not isinstance(self.adapter_generation, str) or not self.adapter_generation:
            raise ValueError("follow-up adapter generation is invalid")


@dataclass(frozen=True, slots=True)
class FrozenSteerItem:
    steer_id: str
    request_digest: str
    user_text: str
    revision: int
    attachments: tuple[str, ...] = ()
    planning_mode: str | None = None
    workspace_id: str = ""

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (self.steer_id, self.request_digest)):
            raise ValueError("frozen steer identity is invalid")
        if not isinstance(self.user_text, str):
            raise TypeError("frozen steer text is invalid")
        if any(not isinstance(value, str) or not value for value in self.attachments):
            raise ValueError("frozen steer attachments are invalid")
        if self.planning_mode is not None and not isinstance(
            self.planning_mode,
            str,
        ):
            raise TypeError("frozen steer planning mode is invalid")
        if not isinstance(self.workspace_id, str) or not self.workspace_id:
            raise ValueError("frozen steer workspace is invalid")
        if not _is_int(self.revision) or self.revision < 1:
            raise ValueError("frozen steer revision is invalid")


@dataclass(frozen=True, slots=True)
class FrozenSteerSnapshot:
    scope: TerminalScope
    claim_epoch: int
    frozen_revision: int
    items: tuple[FrozenSteerItem, ...]
    buffer_identity_digest: str
    merged_text: str
    merged_attachments: tuple[str, ...]
    planning_mode: str | None
    workspace_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("frozen steer scope is invalid")
        if not _is_int(self.claim_epoch) or self.claim_epoch < 0:
            raise ValueError("frozen steer claim epoch is invalid")
        if not _is_int(self.frozen_revision) or self.frozen_revision < 0:
            raise ValueError("frozen steer revision is invalid")
        if any(not isinstance(item, FrozenSteerItem) for item in self.items):
            raise TypeError("frozen steer items are invalid")
        if tuple(item.revision for item in self.items) != tuple(sorted(item.revision for item in self.items)):
            raise ValueError("frozen steer items are not ordered")
        if not isinstance(self.buffer_identity_digest, str) or not self.buffer_identity_digest:
            raise ValueError("frozen steer digest is invalid")
        if not isinstance(self.merged_text, str):
            raise TypeError("frozen steer merged text is invalid")
        if any(not isinstance(value, str) or not value for value in self.merged_attachments):
            raise ValueError("frozen steer merged attachments are invalid")
        if self.planning_mode is not None and not isinstance(
            self.planning_mode,
            str,
        ):
            raise TypeError("frozen steer planning mode is invalid")
        if not isinstance(self.workspace_id, str) or (self.items and not self.workspace_id):
            raise ValueError("frozen steer workspace is invalid")


@dataclass(frozen=True, slots=True)
class RecoveryWriterFence:
    owner_instance_id: str
    writer_lease_epoch: int
    writer_admission_epoch: int
    rollout_config_generation: str
    lease_expires_at: str
    recovery_only: bool = True

    def __post_init__(self) -> None:
        text_fields = (
            self.owner_instance_id,
            self.rollout_config_generation,
            self.lease_expires_at,
        )
        if any(not isinstance(value, str) or not value for value in text_fields):
            raise ValueError("recovery writer fence identity is invalid")
        if (
            not _is_int(self.writer_lease_epoch)
            or self.writer_lease_epoch < 1
            or not _is_int(self.writer_admission_epoch)
            or self.writer_admission_epoch < 1
        ):
            raise ValueError("recovery writer fence epoch is invalid")
        if self.recovery_only is not True:
            raise ValueError("recovery writer fence cannot admit fresh turns")
        _parse_aware_utc(self.lease_expires_at, "recovery writer fence expiry")


@dataclass(frozen=True, slots=True)
class TerminalWriterLease:
    """Single-active production writer capability held across live workers."""

    owner_instance_id: str
    writer_lease_epoch: int
    writer_admission_epoch: int
    rollout_config_generation: str
    lease_expires_at: str
    admission_state: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.owner_instance_id,
                self.rollout_config_generation,
                self.lease_expires_at,
            )
        ):
            raise ValueError("terminal writer lease identity is invalid")
        if self.admission_state not in {"open", "draining"}:
            raise ValueError("terminal writer lease state is invalid")
        if (
            not _is_int(self.writer_lease_epoch)
            or self.writer_lease_epoch < 1
            or not _is_int(self.writer_admission_epoch)
            or self.writer_admission_epoch < 1
        ):
            raise ValueError("terminal writer lease epoch is invalid")
        _parse_aware_utc(self.lease_expires_at, "terminal writer lease expiry")


class ConsumerProfile(StrEnum):
    """冻结 legacy consumer 差异，禁止用一个近似集合替代。"""

    CORE_BUS = "core_bus"
    WEB = "web"
    FEISHU = "feishu"
    IM_RENDERER = "im_renderer"
    TURN_HISTORY = "turn_history"


class TerminalClassification(StrEnum):
    NON_TERMINAL = "non_terminal"
    ROOT_COMMITTED = "root_committed"
    ROOT_NON_DURABLE = "root_non_durable"
    ROOT_UNKNOWN = "root_unknown"
    MALFORMED = "malformed"


class TerminalStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    CLARIFICATION_PENDING = "clarification_pending"
    APPROVAL_PENDING = "approval_pending"


EXPLICIT_STOP_CANCELLED_REASON = "explicit_stop_cancelled/v1"
LEGACY_EXPLICIT_STOP_CLOSING_REASON = f"root_done:{TerminalStatus.CANCELLED.value}"


def is_explicit_user_cancel_receipt(
    *,
    status: TerminalStatus,
    reason_code: str | None,
    generation_kind: str | None,
    closing_reason: str | None,
) -> bool:
    """Distinguish Stop from other control receipts that also use CANCELLED."""

    if status is not TerminalStatus.CANCELLED:
        return False
    if generation_kind == "control":
        return reason_code == "user_cancelled" and closing_reason == "user_cancelled"
    # Active Stop receipts written before the reason-code fix used NULL, but
    # their generation still carries the immutable explicit-Stop marker.
    return closing_reason in {
        EXPLICIT_STOP_CANCELLED_REASON,
        LEGACY_EXPLICIT_STOP_CLOSING_REASON,
    } and reason_code in {None, "user_cancelled"}


class GenerationState(StrEnum):
    OPENED = "opened"
    RUNNING = "running"
    CLOSING = "closing"
    RECOVERY_PENDING = "recovery_pending"
    CLOSED = "closed"


class ActorScope(StrEnum):
    ROOT = "root"
    PEER = "peer"
    STEP = "step"
    TRANSPORT = "transport"


class PhaseScope(StrEnum):
    IN_GENERATION = "in_generation"
    CONTROL_TRANSITION = "control_transition"
    POST_TERMINAL_AUXILIARY = "post_terminal_auxiliary"


class RootDurability(StrEnum):
    COMMITTED = "committed"
    NON_DURABLE = "non_durable"
    UNKNOWN = "unknown"


class EventPublishMode(StrEnum):
    """Durable proposal materialize 的延迟策略，不是 root 可信性结论。"""

    NORMAL = "normal"
    BEFORE_LIVE = "before_live"


@dataclass(frozen=True, slots=True)
class AcceptedEventProposal:
    """durable accept commit 后的 immutable proposal identity。"""

    scope: TerminalScope
    proposal_id: str
    event_id: str
    accept_ordinal: int
    accepted_claim_epoch: int
    event_type: str
    actor_scope: ActorScope
    phase_scope: PhaseScope
    payload_json: str
    payload_digest: str
    root_durability: RootDurability
    publish_mode: EventPublishMode
    accepted_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("accepted event scope is invalid")
        text_fields = (
            self.proposal_id,
            self.event_id,
            self.event_type,
            self.payload_json,
            self.payload_digest,
            self.accepted_at,
        )
        if any(not isinstance(value, str) or not value for value in text_fields):
            raise ValueError("accepted event identity fields must be non-empty")
        if not _is_int(self.accept_ordinal) or self.accept_ordinal < 1:
            raise ValueError("accepted event ordinal must be positive")
        if not _is_int(self.accepted_claim_epoch) or self.accepted_claim_epoch < 1:
            raise ValueError("accepted event claim epoch must be positive")
        if not isinstance(self.actor_scope, ActorScope) or not isinstance(self.phase_scope, PhaseScope):
            raise TypeError("accepted event scopes are invalid")
        if self.phase_scope is not PhaseScope.IN_GENERATION:
            raise ValueError("execution proposal must be in_generation")
        if not isinstance(self.root_durability, RootDurability) or not isinstance(self.publish_mode, EventPublishMode):
            raise TypeError("accepted event durability is invalid")
        _validate_canonical_event_payload(self.payload_json, self.payload_digest)


@dataclass(frozen=True, slots=True)
class EventTicket:
    """business event 与 proposal 同事务持久化后的 stable delivery identity。"""

    scope: TerminalScope
    proposal_id: str
    event_id: str
    accept_ordinal: int
    event_seq: int
    legacy_seq: int
    event_type: str
    actor_scope: ActorScope
    phase_scope: PhaseScope
    payload_json: str
    payload_digest: str
    root_durability: RootDurability
    occurred_at: str
    # Terminal-batch rows bind every required event (not only ``done``) to
    # the committed receipt. Keep that storage identity on the live ticket so
    # replay and live expose the same envelope.
    terminal_receipt_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("event ticket scope is invalid")
        text_fields = (
            self.proposal_id,
            self.event_id,
            self.event_type,
            self.payload_json,
            self.payload_digest,
            self.occurred_at,
        )
        if any(not isinstance(value, str) or not value for value in text_fields):
            raise ValueError("event ticket identity fields must be non-empty")
        if any(not _is_int(value) or value < 1 for value in (self.accept_ordinal, self.event_seq, self.legacy_seq)):
            raise ValueError("event ticket sequences must be positive")
        if not isinstance(self.actor_scope, ActorScope) or not isinstance(self.phase_scope, PhaseScope):
            raise TypeError("event ticket scopes are invalid")
        if self.phase_scope is not PhaseScope.IN_GENERATION:
            raise ValueError("execution event ticket must be in_generation")
        if not isinstance(self.root_durability, RootDurability):
            raise TypeError("event ticket root durability is invalid")
        if self.terminal_receipt_id is not None and (not isinstance(self.terminal_receipt_id, str) or not self.terminal_receipt_id):
            raise ValueError("event ticket terminal receipt identity is invalid")
        _validate_canonical_event_payload(self.payload_json, self.payload_digest)

    def payload(self) -> dict[str, object]:
        """每次返回新对象，调用方不能改写持久 proposal identity。"""

        parsed = json.loads(self.payload_json)
        if not isinstance(parsed, dict):
            raise TerminalProtocolMalformedError("event ticket payload is malformed")
        return parsed


@dataclass(frozen=True, slots=True)
class PostTerminalAuxiliaryContext:
    """receipt-fenced 后置事件的幂等写入权限。"""

    scope: TerminalScope
    receipt_id: str
    event_id: str
    expected_delivery_fence_epoch: int
    auth_fence_vector_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("post-terminal auxiliary scope is invalid")
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.receipt_id,
                self.event_id,
                self.auth_fence_vector_digest,
            )
        ):
            raise ValueError("post-terminal auxiliary identity is invalid")
        if not _is_int(self.expected_delivery_fence_epoch) or self.expected_delivery_fence_epoch < 0:
            raise ValueError("post-terminal auxiliary delivery fence is invalid")


class AuthFenceScopeKind(StrEnum):
    TEAM_MEMBERSHIP = "team_membership"
    RESOURCE_GRANT = "resource_grant"
    SESSION_VISIBILITY = "session_visibility"
    USER_ACCOUNT = "user_account"


_AUTH_FENCE_SCOPE_ORDER = {
    kind: ordinal
    for ordinal, kind in enumerate(
        (
            AuthFenceScopeKind.TEAM_MEMBERSHIP,
            AuthFenceScopeKind.RESOURCE_GRANT,
            AuthFenceScopeKind.SESSION_VISIBILITY,
            AuthFenceScopeKind.USER_ACCOUNT,
        )
    )
}


@dataclass(frozen=True, slots=True)
class AuthFenceScope:
    scope_kind: AuthFenceScopeKind
    team_id: str
    scope_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope_kind, AuthFenceScopeKind):
            raise TypeError("auth fence scope kind is invalid")
        if not isinstance(self.team_id, str) or not self.team_id or not isinstance(self.scope_ref, str) or not self.scope_ref:
            raise ValueError("auth fence scope identity must be non-empty")
        if (self.scope_kind is AuthFenceScopeKind.USER_ACCOUNT) != (self.team_id == "__global__"):
            raise ValueError("user account auth fence must use the global team sentinel")

    def sort_key(self) -> tuple[int, bytes, bytes]:
        return (
            _AUTH_FENCE_SCOPE_ORDER[self.scope_kind],
            self.scope_ref.encode("utf-8"),
            self.team_id.encode("utf-8"),
        )


@dataclass(frozen=True, slots=True)
class AuthFenceEpoch:
    scope: AuthFenceScope
    epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AuthFenceScope):
            raise TypeError("auth fence epoch scope is invalid")
        if not _is_int(self.epoch) or self.epoch < 1:
            raise ValueError("auth fence epoch must be positive")

    def canonical_projection(self) -> dict[str, object]:
        return {
            "epoch": self.epoch,
            "scope_kind": self.scope.scope_kind.value,
            "scope_ref": self.scope.scope_ref,
            "team_id": self.scope.team_id,
        }


@dataclass(frozen=True, slots=True)
class AuthFenceVector:
    entries: tuple[AuthFenceEpoch, ...]
    auth_fence_vector_digest: str

    def __post_init__(self) -> None:
        if not self.entries or any(not isinstance(entry, AuthFenceEpoch) for entry in self.entries):
            raise ValueError("auth fence vector entries are invalid")
        if tuple(sorted(self.entries, key=lambda entry: entry.scope.sort_key())) != self.entries:
            raise ValueError("auth fence vector must use canonical scope order")
        scopes = tuple(entry.scope for entry in self.entries)
        if len(scopes) != len(set(scopes)):
            raise ValueError("auth fence vector contains duplicate scopes")
        expected = canonical_digest_v1([entry.canonical_projection() for entry in self.entries])
        if self.auth_fence_vector_digest != expected:
            raise ValueError("auth fence vector digest mismatch")

    @classmethod
    def build(cls, entries: tuple[AuthFenceEpoch, ...]) -> AuthFenceVector:
        ordered = tuple(sorted(entries, key=lambda entry: entry.scope.sort_key()))
        digest = canonical_digest_v1([entry.canonical_projection() for entry in ordered])
        return cls(entries=ordered, auth_fence_vector_digest=digest)


@dataclass(frozen=True, slots=True)
class AuthFenceResolveContext:
    team_id: str
    viewer: str
    resource_ref: str
    session_id: str
    required_scopes: tuple[AuthFenceScope, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (self.team_id, self.viewer, self.resource_ref, self.session_id)):
            raise ValueError("auth fence resolve context fields must be non-empty")
        if not self.required_scopes or any(not isinstance(scope, AuthFenceScope) for scope in self.required_scopes):
            raise ValueError("auth fence resolve context requires typed scopes")
        if len(self.required_scopes) != len(set(self.required_scopes)):
            raise ValueError("auth fence resolve context contains duplicate scopes")
        if {scope.scope_kind for scope in self.required_scopes} != set(AuthFenceScopeKind):
            raise ValueError("auth fence resolve context requires every fixed kind of scope")
        if any(scope.scope_kind is not AuthFenceScopeKind.USER_ACCOUNT and scope.team_id != self.team_id for scope in self.required_scopes):
            raise ValueError("auth fence scope team does not match resolve context")


@dataclass(frozen=True, slots=True)
class GenerationPointer:
    generation: int
    generation_transition_epoch: int
    delivery_fence_epoch: int

    def __post_init__(self) -> None:
        if (
            not _is_int(self.generation)
            or self.generation < 1
            or not _is_int(self.generation_transition_epoch)
            or self.generation_transition_epoch < 1
            or not _is_int(self.delivery_fence_epoch)
            or self.delivery_fence_epoch < 0
        ):
            raise ValueError("generation pointer fields are invalid")


@dataclass(frozen=True, slots=True)
class SubscriberFenceToken:
    generation: int
    generation_transition_epoch: int
    delivery_fence_epoch: int
    auth_fence_vector_digest: str

    def __post_init__(self) -> None:
        GenerationPointer(
            generation=self.generation,
            generation_transition_epoch=self.generation_transition_epoch,
            delivery_fence_epoch=self.delivery_fence_epoch,
        )
        if not isinstance(self.auth_fence_vector_digest, str) or not self.auth_fence_vector_digest:
            raise ValueError("subscriber fence token must be viewer-bound")

    @property
    def pointer(self) -> GenerationPointer:
        return GenerationPointer(
            generation=self.generation,
            generation_transition_epoch=self.generation_transition_epoch,
            delivery_fence_epoch=self.delivery_fence_epoch,
        )


@dataclass(frozen=True, slots=True)
class PromoteCertificate:
    subscriber_fence_token: SubscriberFenceToken
    bus_epoch: int
    checkpoint_token: str
    persistent_event_highwater: int

    def __post_init__(self) -> None:
        if not isinstance(self.subscriber_fence_token, SubscriberFenceToken):
            raise TypeError("promote certificate subscriber token is invalid")
        if not _is_int(self.bus_epoch) or self.bus_epoch < 1:
            raise ValueError("promote certificate bus epoch must be positive")
        if not isinstance(self.checkpoint_token, str) or not self.checkpoint_token:
            raise ValueError("promote certificate checkpoint token must be non-empty")
        if not _is_int(self.persistent_event_highwater) or self.persistent_event_highwater < 0:
            raise ValueError("promote certificate highwater must be non-negative")


@dataclass(frozen=True, slots=True)
class AuthFenceFrameContext:
    scope: TerminalScope
    subscriber_id: str
    frame_digest: str
    expected_auth_fence_vector: AuthFenceVector
    expected_delivery_fence_epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("auth frame scope is invalid")
        if not isinstance(self.subscriber_id, str) or not self.subscriber_id:
            raise ValueError("auth frame subscriber identity must be non-empty")
        if not isinstance(self.frame_digest, str) or not self.frame_digest:
            raise ValueError("auth frame digest must be non-empty")
        if not isinstance(self.expected_auth_fence_vector, AuthFenceVector):
            raise TypeError("auth frame vector is invalid")
        if not _is_int(self.expected_delivery_fence_epoch) or self.expected_delivery_fence_epoch < 0:
            raise ValueError("auth frame delivery fence must be non-negative")


@dataclass(frozen=True, slots=True)
class FrameSendCertificate:
    linearized_at: str
    auth_fence_vector_digest: str
    delivery_fence_epoch: int
    frame_digest: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (self.linearized_at, self.auth_fence_vector_digest, self.frame_digest)):
            raise ValueError("frame send certificate identity is invalid")
        if not _is_int(self.delivery_fence_epoch) or self.delivery_fence_epoch < 0:
            raise ValueError("frame send certificate delivery fence is invalid")


class FinalAnswerMode(StrEnum):
    NONE = "none"
    PRECOMMITTED = "precommitted"
    IN_TERMINAL_BATCH = "in_terminal_batch"


class CandidateOrigin(StrEnum):
    NORMAL = "normal"
    RECOVERY = "recovery"
    CONTROL = "control"


class CandidateOutcome(StrEnum):
    PRIMARY = "primary"
    RECOVERY_INTERRUPT = "recovery_interrupt"


class SessionVariant(StrEnum):
    FOLLOWUP = "followup"
    DEFERRED = "deferred"
    DISCARD = "discard"
    NONE = "none"


class CommitConfirmationDisposition(StrEnum):
    COMMITTED = "committed"
    ABSENT = "absent"


class EffectClass(StrEnum):
    NONE = "none"
    READ_ONLY = "read_only"
    SIDE_EFFECT_POSSIBLE = "side_effect_possible"


class EffectState(StrEnum):
    INTENT = "intent"
    DISPATCHED = "dispatched"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class EffectSettlementSource(StrEnum):
    PROVIDER = "provider"
    MANUAL = "manual"


class EffectSettlementOutcome(StrEnum):
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"
    COMPENSATED = "compensated"


class RecoveryKind(StrEnum):
    OPENED_OWNER_LOST = "opened_owner_lost"
    EXECUTING_OWNER_LOST = "executing_owner_lost"
    FINALIZATION_UNKNOWN = "finalization_unknown"
    NON_DURABLE = "non_durable"
    DELIVERY_PENDING = "delivery_pending"
    EFFECT_UNKNOWN = "effect_unknown"
    DEPENDENCY_MISSING = "dependency_missing"


@dataclass(frozen=True, slots=True)
class FinalizationContext:
    scope: TerminalScope
    execution_owner_instance_id: str
    execution_claim_epoch: int
    finalization_owner_instance_id: str
    finalization_epoch: int
    generation_row_version: int
    closing_accepted_through: int
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("finalization scope is invalid")
        identities = (
            self.execution_owner_instance_id,
            self.finalization_owner_instance_id,
            self.lease_expires_at,
        )
        if any(not isinstance(value, str) or not value for value in identities):
            raise ValueError("finalization identity is invalid")
        if not _is_int(self.execution_claim_epoch) or self.execution_claim_epoch < 0:
            raise ValueError("finalization execution claim epoch is invalid")
        if not _is_int(self.finalization_epoch) or self.finalization_epoch < 1:
            raise ValueError("finalization epoch is invalid")
        if not _is_int(self.generation_row_version) or self.generation_row_version < 1:
            raise ValueError("finalization generation row version is invalid")
        if not _is_int(self.closing_accepted_through) or self.closing_accepted_through < 0:
            raise ValueError("finalization accepted fence is invalid")
        _parse_aware_utc(self.lease_expires_at, "finalization lease expiry")


@dataclass(frozen=True, slots=True)
class TerminalFlushResult:
    closing_accepted_through: int
    persisted_event_highwater: int
    tickets: tuple[EventTicket, ...]

    def __post_init__(self) -> None:
        if not _is_int(self.closing_accepted_through) or self.closing_accepted_through < 0:
            raise ValueError("terminal flush accepted fence is invalid")
        if not _is_int(self.persisted_event_highwater) or self.persisted_event_highwater < 0:
            raise ValueError("terminal flush persisted highwater is invalid")
        if any(not isinstance(ticket, EventTicket) for ticket in self.tickets):
            raise TypeError("terminal flush tickets are invalid")
        if len(self.tickets) != self.closing_accepted_through:
            raise ValueError("terminal flush ticket count does not match accepted fence")
        if any(ticket.event_seq > self.persisted_event_highwater for ticket in self.tickets):
            raise ValueError("terminal flush ticket exceeds persisted highwater")


@dataclass(frozen=True, slots=True)
class PreparedFinalizationContext:
    finalization: FinalizationContext
    candidate_id: str
    candidate_projection_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.finalization, FinalizationContext):
            raise TypeError("prepared finalization context is invalid")
        if any(not isinstance(value, str) or not value for value in (self.candidate_id, self.candidate_projection_digest)):
            raise ValueError("prepared finalization identity is invalid")


@dataclass(frozen=True, slots=True)
class StagedTerminalEvent:
    event_id: str
    event_type: str
    actor_scope: ActorScope
    phase_scope: PhaseScope
    payload_json: str
    payload_digest: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (self.event_id, self.event_type)):
            raise ValueError("staged terminal event identity is invalid")
        if not isinstance(self.actor_scope, ActorScope) or not isinstance(self.phase_scope, PhaseScope):
            raise TypeError("staged terminal event scope is invalid")
        _validate_canonical_event_payload(self.payload_json, self.payload_digest)

    @classmethod
    def from_payload(
        cls,
        *,
        event_id: str,
        event_type: str,
        actor_scope: ActorScope,
        phase_scope: PhaseScope,
        payload: Mapping[str, object],
    ) -> StagedTerminalEvent:
        payload_json = canonical_json_v1(dict(payload))
        return cls(
            event_id=event_id,
            event_type=event_type,
            actor_scope=actor_scope,
            phase_scope=phase_scope,
            payload_json=payload_json,
            payload_digest=canonical_digest_v1(dict(payload)),
        )

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self.payload_json)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True, slots=True)
class TerminalCandidateVariant:
    variant_key: str
    outcome_variant: CandidateOutcome
    session_variant: SessionVariant
    status: TerminalStatus
    reason_code: str | None
    message_key: str
    final_answer_mode: FinalAnswerMode
    final_answer_event_id: str | None
    final_answer_digest: str | None
    session_mutation_template: Mapping[str, object]
    events: tuple[StagedTerminalEvent, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (self.variant_key, self.message_key)):
            raise ValueError("terminal candidate variant identity is invalid")
        if not isinstance(self.outcome_variant, CandidateOutcome):
            raise TypeError("terminal candidate outcome is invalid")
        if not isinstance(self.session_variant, SessionVariant):
            raise TypeError("terminal candidate session variant is invalid")
        if not isinstance(self.status, TerminalStatus) or not isinstance(self.final_answer_mode, FinalAnswerMode):
            raise TypeError("terminal candidate status is invalid")
        if self.reason_code is not None and (not isinstance(self.reason_code, str) or self.reason_code not in _EXPECTED_REASON_CODES):
            raise ValueError("terminal candidate reason code is invalid")
        if not validate_final_answer_mode(
            status=self.status,
            mode=self.final_answer_mode,
            final_answer_event_id=self.final_answer_event_id,
        ):
            raise ValueError("terminal candidate final answer mode is invalid")
        if self.final_answer_mode is FinalAnswerMode.NONE:
            if self.final_answer_digest is not None:
                raise ValueError("terminal candidate without answer cannot carry answer digest")
        elif not isinstance(self.final_answer_digest, str) or not self.final_answer_digest:
            raise ValueError("terminal candidate answer digest is required")
        if not isinstance(self.session_mutation_template, Mapping):
            raise TypeError("terminal candidate session mutation template is invalid")
        canonical_json_v1(dict(self.session_mutation_template))
        if not self.events:
            raise ValueError("terminal candidate variant must stage events")
        if any(not isinstance(event, StagedTerminalEvent) for event in self.events):
            raise TypeError("terminal candidate staged event is invalid")
        terminal_event = self.events[-1]
        if terminal_event.event_type != "done" or terminal_event.actor_scope is not ActorScope.ROOT:
            raise ValueError("terminal candidate variant must end with root done")
        staged_answers = [event for event in self.events[:-1] if event.event_type == "final_answer"]
        if self.final_answer_mode is FinalAnswerMode.IN_TERMINAL_BATCH:
            if (
                len(staged_answers) != 1
                or staged_answers[0].event_id != self.final_answer_event_id
                or staged_answers[0].payload_digest != self.final_answer_digest
                or staged_answers[0].actor_scope is not ActorScope.ROOT
                or staged_answers[0].phase_scope is not PhaseScope.IN_GENERATION
            ):
                raise ValueError("terminal batch answer identity is invalid")
        elif staged_answers:
            raise ValueError("terminal candidate without an in-batch answer cannot stage final_answer")
        if self.outcome_variant is CandidateOutcome.RECOVERY_INTERRUPT:
            valid_interrupt = (self.reason_code, self.message_key) in {
                ("terminal_projection_not_committed", "turn.result_not_committed"),
                ("worker_start_failed", "turn.aborted"),
            }
            if (
                self.status is not TerminalStatus.ABORTED
                or not valid_interrupt
                or self.final_answer_mode is not FinalAnswerMode.NONE
                or self.session_variant not in {SessionVariant.DEFERRED, SessionVariant.DISCARD, SessionVariant.NONE}
            ):
                raise ValueError("recovery interrupt variant is invalid")

    @property
    def required_events(self) -> tuple[StagedTerminalEvent, ...]:
        return self.events[:-1]

    @property
    def required_manifest_template(self) -> tuple[dict[str, object], ...]:
        values: list[dict[str, object]] = []
        if self.final_answer_mode is FinalAnswerMode.PRECOMMITTED:
            assert self.final_answer_event_id is not None
            assert self.final_answer_digest is not None
            values.append(
                {
                    "event_id": self.final_answer_event_id,
                    "event_type": "final_answer",
                    "ordinal": 1,
                    "payload_digest": self.final_answer_digest,
                }
            )
        ordinal_offset = len(values)
        values.extend(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "ordinal": ordinal + ordinal_offset,
                "payload_digest": event.payload_digest,
            }
            for ordinal, event in enumerate(self.required_events, start=1)
        )
        return tuple(values)

    @property
    def required_manifest_template_digest(self) -> str:
        return canonical_digest_v1(self.required_manifest_template)

    @property
    def session_mutation_template_digest(self) -> str:
        return canonical_digest_v1(dict(self.session_mutation_template))

    @property
    def variant_projection(self) -> dict[str, object]:
        return {
            "variant_key": self.variant_key,
            "outcome_variant": self.outcome_variant.value,
            "session_variant": self.session_variant.value,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "message_key": self.message_key,
            "final_answer_mode": self.final_answer_mode.value,
            "final_answer_event_id": self.final_answer_event_id,
            "final_answer_digest": self.final_answer_digest,
            "required_manifest_template_digest": self.required_manifest_template_digest,
            "session_mutation_template_digest": self.session_mutation_template_digest,
            "events": [
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "ordinal": ordinal,
                    "actor_scope": event.actor_scope.value,
                    "phase_scope": event.phase_scope.value,
                    "payload_digest": event.payload_digest,
                }
                for ordinal, event in enumerate(self.events, start=1)
            ],
        }

    @property
    def variant_digest(self) -> str:
        return canonical_digest_v1(self.variant_projection)


@dataclass(frozen=True, slots=True)
class TerminalCandidatePlan:
    scope: TerminalScope
    candidate_id: str
    reserved_receipt_id: str
    reserved_terminal_event_id: str
    origin: CandidateOrigin
    closing_accepted_through: int
    persisted_event_highwater: int
    variants: tuple[TerminalCandidateVariant, ...]
    session_disposition_plan: Mapping[str, object]
    projection_extensions: tuple[Mapping[str, object], ...] = ()
    canonicalization_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("terminal candidate scope is invalid")
        identities = (self.candidate_id, self.reserved_receipt_id, self.reserved_terminal_event_id)
        if any(not isinstance(value, str) or not value for value in identities):
            raise ValueError("terminal candidate identity is invalid")
        if not isinstance(self.origin, CandidateOrigin):
            raise TypeError("terminal candidate origin is invalid")
        if not _is_int(self.canonicalization_version) or self.canonicalization_version != 1:
            raise ValueError("terminal candidate canonicalization version must be 1")
        if not _is_int(self.closing_accepted_through) or self.closing_accepted_through < 0:
            raise ValueError("terminal candidate accepted fence is invalid")
        if not _is_int(self.persisted_event_highwater) or self.persisted_event_highwater < 0:
            raise ValueError("terminal candidate persisted event highwater is invalid")
        if not self.variants or any(not isinstance(variant, TerminalCandidateVariant) for variant in self.variants):
            raise ValueError("terminal candidate variants are invalid")
        keys = [variant.variant_key for variant in self.variants]
        pairs = [(variant.outcome_variant, variant.session_variant) for variant in self.variants]
        if len(keys) != len(set(keys)) or len(pairs) != len(set(pairs)):
            raise ValueError("terminal candidate variants must be unique")
        if keys != sorted(keys):
            raise ValueError("terminal candidate variants must use canonical key order")
        if any(variant.events[-1].event_id != self.reserved_terminal_event_id for variant in self.variants):
            raise ValueError("terminal candidate variants must share reserved terminal event identity")
        nonterminal_ids = [event.event_id for variant in self.variants for event in variant.events[:-1]]
        if len(nonterminal_ids) != len(set(nonterminal_ids)):
            raise ValueError("non-terminal staged event identity cannot be shared across variants")
        if self.origin is CandidateOrigin.NORMAL:
            primary = [variant for variant in self.variants if variant.outcome_variant is CandidateOutcome.PRIMARY]
            recovery = [variant for variant in self.variants if variant.outcome_variant is CandidateOutcome.RECOVERY_INTERRUPT]
            if not primary or not recovery:
                raise ValueError("normal candidate requires primary and recovery variants")
            primary_shape = {(item.status, item.reason_code, item.message_key) for item in primary}
            if len(primary_shape) != 1:
                raise ValueError("normal candidate primary status projection must be stable")
        elif self.origin is CandidateOrigin.RECOVERY:
            outcomes = {variant.outcome_variant for variant in self.variants}
            if len(self.variants) != 1 or not outcomes <= {
                CandidateOutcome.RECOVERY_INTERRUPT,
                CandidateOutcome.PRIMARY,
            }:
                raise ValueError("recovery candidate outcome is invalid")
        elif len(self.variants) != 1 or self.variants[0].outcome_variant is not CandidateOutcome.PRIMARY:
            raise ValueError("control candidate requires one primary variant")
        if not isinstance(self.session_disposition_plan, Mapping):
            raise TypeError("terminal candidate Session plan is invalid")
        canonical_json_v1(dict(self.session_disposition_plan))
        variant_templates = self.session_disposition_plan.get("variant_templates")
        if not isinstance(variant_templates, Mapping) or set(variant_templates) != set(keys):
            raise ValueError("terminal candidate session variant templates are incomplete")
        for variant in self.variants:
            stored_template = variant_templates.get(variant.variant_key)
            if not isinstance(stored_template, Mapping) or canonical_json_v1(dict(stored_template)) != canonical_json_v1(
                dict(variant.session_mutation_template)
            ):
                raise ValueError("terminal candidate session variant template mismatch")
        followups = [variant for variant in self.variants if variant.session_variant is SessionVariant.FOLLOWUP]
        if followups:
            writer_admission_epoch = self.session_disposition_plan.get("writer_admission_epoch")
            if not _is_int(writer_admission_epoch) or writer_admission_epoch < 1:
                raise ValueError("followup candidate must freeze a writer admission epoch")
            for variant in followups:
                next_turn = variant.session_mutation_template.get("next_turn")
                if not isinstance(next_turn, Mapping):
                    raise ValueError("followup candidate must freeze a next Turn")
                if (
                    any(not isinstance(next_turn.get(field), str) or not next_turn.get(field) for field in ("turn_id", "request_id"))
                    or not isinstance(
                        next_turn.get("user_text"),
                        str,
                    )
                    or not isinstance(next_turn.get("execution_intent"), Mapping)
                ):
                    raise ValueError("followup candidate next Turn identity is invalid")
        if any(not isinstance(extension, Mapping) for extension in self.projection_extensions):
            raise TypeError("terminal candidate projection extension is invalid")
        for extension in self.projection_extensions:
            _validate_projection_extension(extension)
        extension_kinds = [str(extension["kind"]) for extension in self.projection_extensions]
        if extension_kinds != sorted(set(extension_kinds)):
            raise ValueError("terminal candidate projection extensions must use unique canonical order")

    @property
    def primary_variant(self) -> TerminalCandidateVariant | None:
        return next(
            (variant for variant in self.variants if variant.outcome_variant is CandidateOutcome.PRIMARY),
            None,
        )

    @property
    def candidate_projection(self) -> dict[str, object]:
        return {
            "canonicalization_version": self.canonicalization_version,
            "candidate_id": self.candidate_id,
            "reserved_receipt_id": self.reserved_receipt_id,
            "reserved_terminal_event_id": self.reserved_terminal_event_id,
            "turn_id": self.scope.turn_id,
            "generation": self.scope.generation,
            "protocol_version": self.scope.protocol_version,
            "candidate_origin": self.origin.value,
            "closing_accepted_through": self.closing_accepted_through,
            "persisted_event_highwater": self.persisted_event_highwater,
            "variants": [variant.variant_projection for variant in sorted(self.variants, key=lambda item: item.variant_key)],
            "session_disposition_plan_digest": self.session_disposition_plan_digest,
            "projection_extensions": [dict(value) for value in self.projection_extensions],
        }

    @property
    def session_disposition_plan_digest(self) -> str:
        return canonical_digest_v1(dict(self.session_disposition_plan))

    @property
    def candidate_projection_digest(self) -> str:
        return canonical_digest_v1(self.candidate_projection)


@dataclass(frozen=True, slots=True)
class CommittedReceipt:
    receipt_id: str
    candidate_id: str
    candidate_projection_digest: str
    selected_projection_digest: str
    selected_variant_key: str
    selected_outcome_variant: CandidateOutcome
    selected_session_variant: SessionVariant
    terminal_event_id: str
    first_event_seq: int
    last_event_seq: int
    status: TerminalStatus

    def __post_init__(self) -> None:
        strings = (
            self.receipt_id,
            self.candidate_id,
            self.candidate_projection_digest,
            self.selected_projection_digest,
            self.selected_variant_key,
            self.terminal_event_id,
        )
        if any(not isinstance(value, str) or not value for value in strings):
            raise ValueError("committed receipt identity is invalid")
        if not isinstance(self.selected_outcome_variant, CandidateOutcome):
            raise TypeError("committed receipt outcome is invalid")
        if not isinstance(self.selected_session_variant, SessionVariant):
            raise TypeError("committed receipt Session variant is invalid")
        if not isinstance(self.status, TerminalStatus):
            raise TypeError("committed receipt status is invalid")
        if not _is_int(self.first_event_seq) or self.first_event_seq < 1:
            raise ValueError("committed receipt first event sequence is invalid")
        if not _is_int(self.last_event_seq) or self.last_event_seq < self.first_event_seq:
            raise ValueError("committed receipt last event sequence is invalid")


@dataclass(frozen=True, slots=True)
class CommitConfirmation:
    disposition: CommitConfirmationDisposition
    candidate_id: str
    candidate_projection_digest: str
    receipt: CommittedReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, CommitConfirmationDisposition):
            raise TypeError("commit confirmation disposition is invalid")
        if any(not isinstance(value, str) or not value for value in (self.candidate_id, self.candidate_projection_digest)):
            raise ValueError("commit confirmation identity is invalid")
        if self.disposition is CommitConfirmationDisposition.COMMITTED:
            if self.receipt is None:
                raise ValueError("committed confirmation requires receipt")
        elif self.receipt is not None:
            raise ValueError("absent confirmation cannot carry receipt")


@dataclass(frozen=True, slots=True)
class EffectAttemptContext:
    scope: TerminalScope
    attempt_id: str
    action_ref: str
    provider: str
    effect_class: EffectClass
    state: EffectState
    claim_epoch: int
    row_version: int
    idempotency_or_fence_token: str | None
    non_retriable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("effect attempt scope is invalid")
        if any(not isinstance(value, str) or not value for value in (self.attempt_id, self.action_ref, self.provider)):
            raise ValueError("effect attempt identity is invalid")
        if not isinstance(self.effect_class, EffectClass) or not isinstance(self.state, EffectState):
            raise TypeError("effect attempt state is invalid")
        if not _is_int(self.claim_epoch) or self.claim_epoch < 1:
            raise ValueError("effect attempt claim epoch is invalid")
        if not _is_int(self.row_version) or self.row_version < 1:
            raise ValueError("effect attempt row version is invalid")
        if self.idempotency_or_fence_token is not None and (
            not isinstance(self.idempotency_or_fence_token, str) or not self.idempotency_or_fence_token
        ):
            raise ValueError("effect attempt provider token is invalid")
        if self.idempotency_or_fence_token is None and not self.non_retriable:
            raise ValueError("effect attempt without provider token must be non-retriable")


@dataclass(frozen=True, slots=True)
class EffectDispatchClaim:
    """CAS result proving whether this caller owns the provider dispatch."""

    attempt: EffectAttemptContext
    dispatched_here: bool

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, EffectAttemptContext):
            raise TypeError("effect dispatch claim attempt is invalid")
        if not isinstance(self.dispatched_here, bool):
            raise TypeError("effect dispatch claim disposition is invalid")
        if self.dispatched_here and self.attempt.state is not EffectState.DISPATCHED:
            raise ValueError("effect dispatch winner must be dispatched")


@dataclass(frozen=True, slots=True)
class RecoveryEffectContext:
    scope: TerminalScope
    decision_id: str
    source_kind: DecisionSourceKind
    source_id: str
    action: DecisionAction
    attempt: EffectAttemptContext
    execution_authz_snapshot_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope) or self.attempt.scope != self.scope:
            raise TypeError("recovery effect scope is invalid")
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.decision_id,
                self.source_id,
                self.execution_authz_snapshot_digest,
            )
        ):
            raise ValueError("recovery effect identity is invalid")
        if not isinstance(self.source_kind, DecisionSourceKind) or not isinstance(self.action, DecisionAction):
            raise TypeError("recovery effect decision enum is invalid")


def stable_approval_effect_action_ref(
    *,
    decision_id: str,
    approval_id: str,
    arguments_digest: str,
) -> str:
    """Bind an approval effect to its decision, source row, and frozen args."""

    if any(not isinstance(value, str) or not value for value in (decision_id, approval_id, arguments_digest)):
        raise ValueError("approval effect action identity is invalid")
    return "approval:" + canonical_digest_v1(
        {
            "approval_id": approval_id,
            "arguments_digest": arguments_digest,
            "decision_id": decision_id,
        }
    )


@dataclass(frozen=True, slots=True)
class EffectDisclosureAttempt:
    attempt_id: str
    state: EffectState
    outcome: EffectSettlementOutcome | None
    provider_contract_key: str
    disclosure_kind: str
    registry_contract_version: int
    settlement_id: str | None
    result_digest: str | None
    evidence_digest: str | None
    provider_external_ref_digest: str | None

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.attempt_id,
                self.provider_contract_key,
                self.disclosure_kind,
            )
        ):
            raise ValueError("effect disclosure attempt identity is invalid")
        if self.disclosure_kind not in {
            "accounting",
            "inference_internal",
            "user_action",
        }:
            raise ValueError("effect disclosure kind is invalid")
        if not isinstance(self.state, EffectState):
            raise TypeError("effect disclosure attempt state is invalid")
        if not _is_int(self.registry_contract_version) or self.registry_contract_version < 1:
            raise ValueError("effect disclosure registry version is invalid")
        settled_fields = (
            self.settlement_id,
            self.result_digest,
            self.evidence_digest,
        )
        if self.state is EffectState.SETTLED:
            if not isinstance(self.outcome, EffectSettlementOutcome) or any(not isinstance(value, str) or not value for value in settled_fields):
                raise ValueError("settled effect disclosure proof is incomplete")
        elif self.outcome is not None or any(value is not None for value in (*settled_fields, self.provider_external_ref_digest)):
            raise ValueError("unsettled effect disclosure carries settlement proof")
        if self.provider_external_ref_digest is not None and (
            not isinstance(self.provider_external_ref_digest, str) or not self.provider_external_ref_digest
        ):
            raise ValueError("effect disclosure provider reference is invalid")

    @property
    def projection(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "disclosure_kind": self.disclosure_kind,
            "evidence_digest": self.evidence_digest,
            "outcome": self.outcome.value if self.outcome is not None else None,
            "provider_contract_key": self.provider_contract_key,
            "provider_external_ref_digest": self.provider_external_ref_digest,
            "registry_contract_version": self.registry_contract_version,
            "result_digest": self.result_digest,
            "settlement_id": self.settlement_id,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class EffectDisclosureSnapshot:
    scope: TerminalScope
    attempt_count: int
    confirmed_applied_count: int
    intent_count: int
    unsettled_count: int
    state: str
    snapshot_digest: str
    protocol: int = 1
    attempts: tuple[EffectDisclosureAttempt, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("effect disclosure scope is invalid")
        for value in (
            self.attempt_count,
            self.confirmed_applied_count,
            self.intent_count,
            self.unsettled_count,
        ):
            if not _is_int(value) or value < 0:
                raise ValueError("effect disclosure count is invalid")
        if not _is_int(self.protocol) or self.protocol not in {1, 2}:
            raise ValueError("effect disclosure protocol is invalid")
        if self.protocol == 1 and self.attempt_count < 1:
            raise ValueError("effect disclosure requires a side-effect attempt")
        if self.confirmed_applied_count > self.attempt_count or self.intent_count > self.attempt_count or self.unsettled_count > self.attempt_count:
            raise ValueError("effect disclosure count exceeds attempt count")
        if self.state not in {"confirmed_applied", "possibly_applied"}:
            raise ValueError("effect disclosure state is invalid")
        if not isinstance(self.snapshot_digest, str) or not self.snapshot_digest:
            raise ValueError("effect disclosure digest is invalid")
        if self.protocol == 1:
            if self.attempts:
                raise ValueError("v1 effect disclosure cannot carry a v2 manifest")
        elif len(self.attempts) != self.attempt_count:
            raise ValueError("v2 effect disclosure attempt count changed")

    @classmethod
    def v2(
        cls,
        *,
        scope: TerminalScope,
        attempts: tuple[EffectDisclosureAttempt, ...],
    ) -> EffectDisclosureSnapshot:
        if any(not isinstance(item, EffectDisclosureAttempt) for item in attempts):
            raise TypeError("v2 effect disclosure attempts are invalid")
        ordered = tuple(sorted(attempts, key=lambda item: item.attempt_id))
        if len({item.attempt_id for item in ordered}) != len(ordered):
            raise ValueError("v2 effect disclosure attempt identity is duplicated")
        applied = sum(item.outcome is EffectSettlementOutcome.CONFIRMED_APPLIED for item in ordered)
        intent_count = sum(item.state is EffectState.INTENT for item in ordered)
        unsettled = sum(item.state is not EffectState.SETTLED for item in ordered)
        return cls(
            scope=scope,
            attempt_count=len(ordered),
            confirmed_applied_count=applied,
            intent_count=intent_count,
            unsettled_count=unsettled,
            state="confirmed_applied" if applied else "possibly_applied",
            snapshot_digest=canonical_digest_v1([item.projection for item in ordered]),
            protocol=2,
            attempts=ordered,
        )

    @property
    def message_key(self) -> str:
        if self.state == "confirmed_applied":
            return "turn.effect_confirmed_applied"
        return "turn.effect_may_have_occurred"

    @property
    def has_only_uncommitted_intents(self) -> bool:
        """Return whether every unsettled attempt is still a discardable intent."""

        if self.protocol == 2:
            return bool(self.intent_count > 0 and self.intent_count == self.unsettled_count)
        return bool(self.intent_count > 0 and self.intent_count == self.unsettled_count and self.confirmed_applied_count == 0)

    @property
    def event_payload(self) -> dict[str, object]:
        if self.protocol == 2:
            counts = self.kind_outcome_counts
            warnings: list[str] = []
            if counts["inference_internal"]["unsettled"]:
                warnings.append("turn.effect_inference_unsettled")
            if counts["user_action"]["unsettled"]:
                warnings.append("turn.effect_user_action_unsettled")
            return {
                "attempt_count": self.attempt_count,
                "counts": counts,
                "protocol": 2,
                "snapshot_digest": self.snapshot_digest,
                "unsettled_count": self.unsettled_count,
                "warning_message_keys": warnings,
            }
        return {
            "attempt_count": self.attempt_count,
            "confirmed_applied_count": self.confirmed_applied_count,
            "intent_count": self.intent_count,
            "message_key": self.message_key,
            "protocol": 1,
            "snapshot_digest": self.snapshot_digest,
            "state": self.state,
            "unsettled_count": self.unsettled_count,
        }

    @property
    def kind_outcome_counts(self) -> dict[str, dict[str, int]]:
        counts = {
            kind: {
                "applied": 0,
                "compensated": 0,
                "not_applied": 0,
                "unsettled": 0,
            }
            for kind in ("accounting", "inference_internal", "user_action")
        }
        for attempt in self.attempts:
            if attempt.outcome is EffectSettlementOutcome.CONFIRMED_APPLIED:
                bucket = "applied"
            elif attempt.outcome is EffectSettlementOutcome.CONFIRMED_NOT_APPLIED:
                bucket = "not_applied"
            elif attempt.outcome is EffectSettlementOutcome.COMPENSATED:
                bucket = "compensated"
            else:
                bucket = "unsettled"
            counts[attempt.disclosure_kind][bucket] += 1
        return counts

    @property
    def projection_extensions(self) -> tuple[Mapping[str, object], ...]:
        if self.protocol == 1:
            return ()
        header: Mapping[str, object] = {
            "kind": "effect_disclosure_v2",
            "attempt_count": self.attempt_count,
            "protocol_version": "2",
            "snapshot_digest": self.snapshot_digest,
        }
        members = tuple(
            {
                "kind": f"effect_disclosure_v2.member.{canonical_digest_v1({'attempt_id': item.attempt_id})[:24]}",
                "attempt_id": item.attempt_id,
                "attempt_state": item.state.value,
                "disclosure_kind_code": item.disclosure_kind,
                "evidence_digest": item.evidence_digest,
                "outcome_code": item.outcome.value if item.outcome is not None else None,
                "provider_contract_id": item.provider_contract_key,
                "provider_external_ref_digest": item.provider_external_ref_digest,
                "registry_contract_version": str(item.registry_contract_version),
                "result_digest": item.result_digest,
                "settlement_id": item.settlement_id,
            }
            for item in self.attempts
        )
        return tuple(sorted((header, *members), key=lambda item: str(item["kind"])))


@dataclass(frozen=True, slots=True)
class EffectDisclosureOverlay:
    scope: TerminalScope
    receipt_id: str | None
    protocol: int
    attempt_count: int
    counts: Mapping[str, Mapping[str, int]]
    proof_digest: str
    unknown_count: int
    oldest_unknown_attempt_started_at: str | None
    oldest_unknown_age_upper_bound_seconds: int | None
    warning_message_keys: tuple[str, ...]
    finalized: bool

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("effect disclosure overlay scope is invalid")
        if self.receipt_id is not None and (not isinstance(self.receipt_id, str) or not self.receipt_id):
            raise ValueError("effect disclosure overlay receipt is invalid")
        if self.protocol != 2:
            raise ValueError("effect disclosure overlay protocol is invalid")
        if not _is_int(self.attempt_count) or self.attempt_count < 0:
            raise ValueError("effect disclosure overlay attempt count is invalid")
        expected_kinds = {
            "accounting",
            "inference_internal",
            "user_action",
        }
        expected_outcomes = {
            "applied",
            "compensated",
            "not_applied",
            "unsettled",
        }
        if not isinstance(self.counts, Mapping) or set(self.counts) != expected_kinds:
            raise ValueError("effect disclosure overlay counts are invalid")
        count_total = 0
        unsettled_total = 0
        for outcomes in self.counts.values():
            if not isinstance(outcomes, Mapping) or set(outcomes) != expected_outcomes:
                raise ValueError("effect disclosure overlay outcomes are invalid")
            if any(not _is_int(value) or value < 0 for value in outcomes.values()):
                raise ValueError("effect disclosure overlay outcome count is invalid")
            count_total += sum(outcomes.values())
            unsettled_total += outcomes["unsettled"]
        if count_total != self.attempt_count:
            raise ValueError("effect disclosure overlay count total changed")
        if not _is_int(self.unknown_count) or not 0 <= self.unknown_count <= self.attempt_count:
            raise ValueError("effect disclosure overlay unknown count is invalid")
        if self.unknown_count > unsettled_total:
            raise ValueError("effect disclosure overlay unknown count exceeds unsettled count")
        if not isinstance(self.proof_digest, str) or not self.proof_digest:
            raise ValueError("effect disclosure overlay proof is invalid")
        if self.oldest_unknown_attempt_started_at is not None:
            _parse_aware_utc(
                self.oldest_unknown_attempt_started_at,
                "effect disclosure overlay oldest unknown attempt",
            )
        if (self.oldest_unknown_attempt_started_at is None) != (self.oldest_unknown_age_upper_bound_seconds is None) or (
            self.oldest_unknown_age_upper_bound_seconds is not None
            and (not _is_int(self.oldest_unknown_age_upper_bound_seconds) or self.oldest_unknown_age_upper_bound_seconds < 0)
        ):
            raise ValueError("effect disclosure overlay unknown age is invalid")
        if not isinstance(self.finalized, bool) or any(not isinstance(value, str) or not value for value in self.warning_message_keys):
            raise ValueError("effect disclosure overlay state is invalid")

    @property
    def payload(self) -> dict[str, object]:
        return {
            "attempt_count": self.attempt_count,
            "counts": {kind: dict(outcomes) for kind, outcomes in self.counts.items()},
            "finalized": self.finalized,
            "oldest_unknown_attempt_started_at": (self.oldest_unknown_attempt_started_at),
            "oldest_unknown_age_upper_bound_seconds": (self.oldest_unknown_age_upper_bound_seconds),
            "proof_digest": self.proof_digest,
            "protocol": self.protocol,
            "receipt_id": self.receipt_id,
            "unknown_count": self.unknown_count,
            "warning_message_keys": list(self.warning_message_keys),
        }


@dataclass(frozen=True, slots=True)
class EffectAttemptDetail:
    attempt: EffectAttemptContext
    disclosure_kind: str
    outcome: EffectSettlementOutcome | None
    result_digest: str | None
    settlement_id: str | None
    settlement_source: EffectSettlementSource | None
    settlement_code: str | None
    actor_audit_ref: str | None
    authz_snapshot_digest: str | None
    evidence_link_id: str | None
    evidence_digest: str | None
    object_version_digest: str | None
    audit_object_ref: str | None
    evidence_retention_state: str | None
    evidence_purge_after: str | None
    provider_external_ref_digest: str | None
    provider_external_ref_key_version: str | None
    attempt_started_at: str
    manual_settlement_allowed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, EffectAttemptContext):
            raise TypeError("effect attempt detail context is invalid")
        if not isinstance(self.disclosure_kind, str) or not self.disclosure_kind:
            raise ValueError("effect attempt detail identity is invalid")
        if self.settlement_source is not None and not isinstance(
            self.settlement_source,
            EffectSettlementSource,
        ):
            raise TypeError("effect attempt detail settlement source is invalid")
        exact_fields = (
            self.result_digest,
            self.settlement_id,
            self.settlement_code,
            self.actor_audit_ref,
            self.authz_snapshot_digest,
            self.evidence_link_id,
            self.evidence_digest,
            self.object_version_digest,
            self.audit_object_ref,
            self.evidence_retention_state,
            self.provider_external_ref_digest,
            self.provider_external_ref_key_version,
        )
        if any(value is not None and (not isinstance(value, str) or not value) for value in exact_fields):
            raise ValueError("effect attempt detail exact proof is invalid")
        _parse_aware_utc(self.attempt_started_at, "effect attempt start time")
        if self.evidence_purge_after is not None:
            _parse_aware_utc(
                self.evidence_purge_after,
                "effect attempt evidence purge time",
            )
        settlement_proof = (
            self.outcome,
            self.result_digest,
            self.settlement_id,
            self.settlement_source,
            self.settlement_code,
            self.actor_audit_ref,
            self.authz_snapshot_digest,
            self.evidence_link_id,
            self.evidence_digest,
            self.object_version_digest,
            self.audit_object_ref,
            self.evidence_retention_state,
            self.evidence_purge_after,
            self.provider_external_ref_digest,
            self.provider_external_ref_key_version,
        )
        if self.attempt.state is EffectState.SETTLED:
            required = settlement_proof[:10] + settlement_proof[11:13]
            if any(value is None for value in required):
                raise ValueError("settled effect attempt detail proof is incomplete")
            if self.evidence_retention_state not in {
                "active",
                "purge_pending",
                "purged",
            }:
                raise ValueError("settled effect attempt retention state is invalid")
            if self.evidence_retention_state == "purged":
                if self.audit_object_ref is not None:
                    raise ValueError("purged effect attempt retains an audit reference")
            if (self.provider_external_ref_digest is None) != (self.provider_external_ref_key_version is None):
                raise ValueError("effect attempt provider reference shape is invalid")
        elif any(value is not None for value in settlement_proof):
            raise ValueError("unsettled effect attempt carries settlement proof")
        if not isinstance(self.manual_settlement_allowed, bool):
            raise TypeError("effect attempt manual settlement flag is invalid")

    @property
    def payload(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt.attempt_id,
            "attempt_started_at": self.attempt_started_at,
            "actor_audit_ref": self.actor_audit_ref,
            "audit_object_ref": self.audit_object_ref,
            "disclosure_kind": self.disclosure_kind,
            "evidence_digest": self.evidence_digest,
            "evidence_link_id": self.evidence_link_id,
            "evidence_purge_after": self.evidence_purge_after,
            "evidence_retention_state": self.evidence_retention_state,
            "manual_settlement_allowed": self.manual_settlement_allowed,
            "object_version_digest": self.object_version_digest,
            "outcome": self.outcome.value if self.outcome is not None else None,
            "provider_contract_key": self.attempt.provider,
            "provider_external_ref_digest": self.provider_external_ref_digest,
            "provider_external_ref_key_version": self.provider_external_ref_key_version,
            "result_digest": self.result_digest,
            "row_version": self.attempt.row_version,
            "settlement_code": self.settlement_code,
            "settlement_id": self.settlement_id,
            "settlement_source": (self.settlement_source.value if self.settlement_source is not None else None),
            "state": self.attempt.state.value,
        }


@dataclass(frozen=True, slots=True)
class EffectSettlementContext:
    scope: TerminalScope
    attempt_id: str
    expected_attempt_row_version: int
    settlement_id: str
    source: EffectSettlementSource
    outcome: EffectSettlementOutcome
    settlement_code: str
    principal_ref: str
    principal_role: str
    authz_snapshot_digest: str
    evidence_link_id: str
    evidence_digest: str
    object_version_digest: str
    purge_after: str | None
    audit_object_ref: str | None = None
    provider_external_ref_digest: str | None = None
    provider_external_ref_key_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("effect settlement scope is invalid")
        strings = (
            self.attempt_id,
            self.settlement_id,
            self.settlement_code,
            self.principal_ref,
            self.principal_role,
            self.authz_snapshot_digest,
            self.evidence_link_id,
            self.evidence_digest,
            self.object_version_digest,
        )
        if any(not isinstance(value, str) or not value for value in strings):
            raise ValueError("effect settlement identity is invalid")
        if not _is_int(self.expected_attempt_row_version) or self.expected_attempt_row_version < 1:
            raise ValueError("effect settlement attempt version is invalid")
        if not isinstance(self.source, EffectSettlementSource) or not isinstance(
            self.outcome,
            EffectSettlementOutcome,
        ):
            raise TypeError("effect settlement enum is invalid")
        if self.source is EffectSettlementSource.PROVIDER:
            if self.principal_role != "provider_adapter":
                raise ValueError("provider settlement requires provider adapter principal")
        elif self.principal_role not in {"owner", "admin"}:
            raise ValueError("manual settlement requires owner or admin principal")
        if (self.provider_external_ref_digest is None) != (self.provider_external_ref_key_version is None):
            raise ValueError("provider external reference digest shape is invalid")
        for value in (
            self.audit_object_ref,
            self.provider_external_ref_digest,
            self.provider_external_ref_key_version,
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("effect settlement optional identity is invalid")
        if self.purge_after is not None:
            _parse_aware_utc(self.purge_after, "effect settlement purge_after")


@dataclass(frozen=True, slots=True)
class RecoveryDiscoveryContext:
    scope: TerminalScope
    observed_state: GenerationState
    observed_generation_row_version: int
    observed_execution_claim_epoch: int
    observed_finalization_epoch: int
    scanner_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope) or not isinstance(self.observed_state, GenerationState):
            raise TypeError("recovery discovery scope is invalid")
        if not _is_int(self.observed_generation_row_version) or self.observed_generation_row_version < 1:
            raise ValueError("recovery discovery row version is invalid")
        for value in (self.observed_execution_claim_epoch, self.observed_finalization_epoch):
            if not _is_int(value) or value < 0:
                raise ValueError("recovery discovery epoch is invalid")
        if not isinstance(self.scanner_id, str) or not self.scanner_id:
            raise ValueError("recovery discovery scanner identity is invalid")


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    scope: TerminalScope
    recovery_id: str
    kind: RecoveryKind
    discovery_identity: str
    generation_row_version: int
    recovery_claim_epoch: int
    owner_instance_id: str | None
    recovery_lease_expires_at: str | None
    finalization_epoch: int
    created: bool

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope) or not isinstance(self.kind, RecoveryKind):
            raise TypeError("recovery record scope is invalid")
        if any(not isinstance(value, str) or not value for value in (self.recovery_id, self.discovery_identity)):
            raise ValueError("recovery record identity is invalid")
        if not _is_int(self.generation_row_version) or self.generation_row_version < 1:
            raise ValueError("recovery record generation version is invalid")
        for value in (self.recovery_claim_epoch, self.finalization_epoch):
            if not _is_int(value) or value < 0:
                raise ValueError("recovery record epoch is invalid")
        if (self.owner_instance_id is None) != (self.recovery_lease_expires_at is None):
            raise ValueError("recovery record owner/lease shape is invalid")
        if self.recovery_lease_expires_at is not None:
            _parse_aware_utc(self.recovery_lease_expires_at, "recovery record lease expiry")


@dataclass(frozen=True, slots=True)
class EffectUnknownRecoveryTransition:
    """One transaction's UNKNOWN attempt and generation recovery fence."""

    attempt: EffectAttemptContext
    recovery: RecoveryRecord

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, EffectAttemptContext) or self.attempt.state not in {
            EffectState.UNKNOWN,
            EffectState.SETTLED,
        }:
            raise TypeError("effect UNKNOWN recovery attempt is invalid")
        if not isinstance(self.recovery, RecoveryRecord) or self.recovery.kind is not RecoveryKind.EFFECT_UNKNOWN:
            raise TypeError("effect UNKNOWN recovery record is invalid")
        if self.attempt.scope != self.recovery.scope:
            raise ValueError("effect UNKNOWN recovery scope mismatch")


@dataclass(frozen=True, slots=True)
class RecoveryProcessingContext:
    scope: TerminalScope
    recovery_id: str
    recovery_owner_instance_id: str
    recovery_claim_epoch: int
    recovery_lease_expires_at: str
    finalization_epoch: int
    finalization_lease_expires_at: str
    generation_row_version: int
    closing_accepted_through: int
    cancelled_finalization: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("recovery processing scope is invalid")
        strings = (
            self.recovery_id,
            self.recovery_owner_instance_id,
            self.recovery_lease_expires_at,
            self.finalization_lease_expires_at,
        )
        if any(not isinstance(value, str) or not value for value in strings):
            raise ValueError("recovery processing identity is invalid")
        for value in (self.recovery_claim_epoch, self.finalization_epoch, self.generation_row_version):
            if not _is_int(value) or value < 1:
                raise ValueError("recovery processing epoch is invalid")
        if not _is_int(self.closing_accepted_through) or self.closing_accepted_through < 0:
            raise ValueError("recovery processing accepted fence is invalid")
        if not isinstance(self.cancelled_finalization, bool):
            raise TypeError("recovery processing cancellation marker is invalid")
        _parse_aware_utc(self.recovery_lease_expires_at, "recovery processing lease expiry")
        _parse_aware_utc(self.finalization_lease_expires_at, "recovery finalization lease expiry")


@dataclass(frozen=True, slots=True)
class PreparedRecoveryContext:
    processing: RecoveryProcessingContext
    candidate_id: str
    candidate_projection_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.processing, RecoveryProcessingContext):
            raise TypeError("prepared recovery context is invalid")
        if any(not isinstance(value, str) or not value for value in (self.candidate_id, self.candidate_projection_digest)):
            raise ValueError("prepared recovery identity is invalid")


@dataclass(frozen=True, slots=True)
class TerminalDeleteContext:
    scope: TerminalScope
    deletion_id: str
    expected_generation_row_version: int
    expected_candidate_id: str | None
    expected_delivery_fence_epoch: int
    actor_audit_ref: str
    authz_snapshot_digest: str
    policy_code: str
    effect_identity_digest: str
    effect_bundle_digest: str
    effect_audit_key_version: str
    effect_key_required_until: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("terminal delete scope is invalid")
        strings = (
            self.deletion_id,
            self.actor_audit_ref,
            self.authz_snapshot_digest,
            self.policy_code,
            self.effect_identity_digest,
            self.effect_bundle_digest,
            self.effect_audit_key_version,
            self.effect_key_required_until,
        )
        if any(not isinstance(value, str) or not value for value in strings):
            raise ValueError("terminal delete identity is invalid")
        if not _is_int(self.expected_generation_row_version) or self.expected_generation_row_version < 1:
            raise ValueError("terminal delete generation version is invalid")
        if not _is_int(self.expected_delivery_fence_epoch) or self.expected_delivery_fence_epoch < 0:
            raise ValueError("terminal delete delivery fence is invalid")
        if self.expected_candidate_id is not None and (not isinstance(self.expected_candidate_id, str) or not self.expected_candidate_id):
            raise ValueError("terminal delete candidate identity is invalid")
        _parse_aware_utc(self.effect_key_required_until, "terminal delete effect key horizon")


@dataclass(frozen=True, slots=True)
class ProtocolDeleteResult:
    scope: TerminalScope
    deletion_id: str
    deletion_digest: str
    new_delivery_fence_epoch: int
    unsettled_effect_count: int
    created: bool

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("protocol delete result scope is invalid")
        if any(not isinstance(value, str) or not value for value in (self.deletion_id, self.deletion_digest)):
            raise ValueError("protocol delete result identity is invalid")
        for value in (self.new_delivery_fence_epoch, self.unsettled_effect_count):
            if not _is_int(value) or value < 0:
                raise ValueError("protocol delete result counter is invalid")


@dataclass(frozen=True, slots=True)
class EvidencePurgeContext:
    evidence_link_id: str
    team_id: str
    object_version_digest: str
    purge_request_id: str
    purge_owner: str
    purge_lease_epoch: int
    purge_lease_expires_at: str
    audit_object_ref: str

    def __post_init__(self) -> None:
        strings = (
            self.evidence_link_id,
            self.team_id,
            self.object_version_digest,
            self.purge_request_id,
            self.purge_owner,
            self.purge_lease_expires_at,
            self.audit_object_ref,
        )
        if any(not isinstance(value, str) or not value for value in strings):
            raise ValueError("evidence purge identity is invalid")
        if not _is_int(self.purge_lease_epoch) or self.purge_lease_epoch < 1:
            raise ValueError("evidence purge lease epoch is invalid")
        _parse_aware_utc(self.purge_lease_expires_at, "evidence purge lease expiry")


@dataclass(frozen=True, slots=True)
class EvidencePurgeReceipt:
    context: EvidencePurgeContext
    outcome: str
    delete_outcome_digest: str
    signed_receipt_digest: str
    signature_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.context, EvidencePurgeContext):
            raise TypeError("evidence purge receipt context is invalid")
        if self.outcome not in {"deleted", "already_absent"}:
            raise ValueError("evidence purge outcome is invalid")
        if any(not isinstance(value, str) or not value for value in (self.delete_outcome_digest, self.signed_receipt_digest)):
            raise ValueError("evidence purge receipt identity is invalid")
        if self.signature_verified is not True:
            raise ValueError("evidence purge receipt signature must be verified")


@dataclass(frozen=True, slots=True)
class EffectRetentionMemberKey:
    member_key_id: str
    attempt_identity_digest: str
    provider_token_digest: str | None

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (self.member_key_id, self.attempt_identity_digest)):
            raise ValueError("effect retention member identity is invalid")
        if self.provider_token_digest is not None and (not isinstance(self.provider_token_digest, str) or not self.provider_token_digest):
            raise ValueError("effect retention provider digest is invalid")


@dataclass(frozen=True, slots=True)
class EffectRetentionContext:
    scope: TerminalScope
    tombstone_id: str
    effect_bundle_digest: str
    audit_key_version: str
    provider_token_digests_digest: str
    settlement_outcomes_digest: str
    eligible_at: str
    provider_dedup_horizon_at: str
    expires_at: str
    members: tuple[EffectRetentionMemberKey, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("effect retention scope is invalid")
        strings = (
            self.tombstone_id,
            self.effect_bundle_digest,
            self.audit_key_version,
            self.provider_token_digests_digest,
            self.settlement_outcomes_digest,
            self.eligible_at,
            self.provider_dedup_horizon_at,
            self.expires_at,
        )
        if any(not isinstance(value, str) or not value for value in strings):
            raise ValueError("effect retention identity is invalid")
        if not self.members or any(not isinstance(value, EffectRetentionMemberKey) for value in self.members):
            raise ValueError("effect retention members are invalid")
        if len({value.member_key_id for value in self.members}) != len(self.members):
            raise ValueError("effect retention member identities must be unique")
        if len({value.attempt_identity_digest for value in self.members}) != len(self.members):
            raise ValueError("effect retention attempt identities must be unique")
        eligible = _parse_aware_utc(self.eligible_at, "effect retention eligible_at")
        horizon = _parse_aware_utc(
            self.provider_dedup_horizon_at,
            "effect retention provider horizon",
        )
        expires = _parse_aware_utc(self.expires_at, "effect retention expiry")
        if not eligible <= horizon <= expires:
            raise ValueError("effect retention timestamps are out of order")


@dataclass(frozen=True, slots=True)
class EffectRetentionResult:
    scope: TerminalScope
    tombstone_id: str
    effect_bundle_digest: str
    attempt_count: int
    created: bool

    def __post_init__(self) -> None:
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("effect retention result scope is invalid")
        if any(not isinstance(value, str) or not value for value in (self.tombstone_id, self.effect_bundle_digest)):
            raise ValueError("effect retention result identity is invalid")
        if not _is_int(self.attempt_count) or self.attempt_count < 1:
            raise ValueError("effect retention attempt count is invalid")


def recovery_effects_safe(states: tuple[EffectState, ...]) -> bool:
    """An intent cannot have crossed dispatch; dispatched/unknown require settlement."""

    if any(not isinstance(state, EffectState) for state in states):
        raise TypeError("recovery effect state is invalid")
    return all(state in {EffectState.INTENT, EffectState.SETTLED} for state in states)


def _validate_projection_extension(extension: Mapping[str, object]) -> None:
    """Keep the S3 extension slot content-free: identities, digests, enums and counts only."""

    value = dict(extension)
    kind = value.get("kind")
    if (
        not value
        or not isinstance(kind, str)
        or not kind
        or len(kind) > 64
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for character in kind)
    ):
        raise ValueError("terminal candidate projection extension kind is invalid")
    suffixes = ("_code", "_count", "_digest", "_id", "_state", "_version")
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 96:
            raise ValueError("terminal candidate projection extension key is invalid")
        if key == "kind":
            continue
        if not key.endswith(suffixes):
            raise ValueError("terminal candidate projection extension may not carry content")
        if key.endswith("_count"):
            if not _is_int(item) or item < 0:
                raise ValueError("terminal candidate projection extension count is invalid")
        elif item is not None and (not isinstance(item, str) or not item or len(item) > 256):
            raise ValueError("terminal candidate projection extension scalar is invalid")
    canonical_json_v1(value)


def _parse_aware_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


_LEGACY_TERMINAL_EVENTS: dict[ConsumerProfile, frozenset[str]] = {
    ConsumerProfile.CORE_BUS: frozenset({"done", "error"}),
    ConsumerProfile.WEB: frozenset({"done", "error", "turn_failed", "turn_aborted"}),
    ConsumerProfile.FEISHU: frozenset({"done", "error", "turn_failed", "turn_aborted"}),
    ConsumerProfile.IM_RENDERER: frozenset({"done", "error", "turn_failed", "turn_aborted", "turn_cancelled"}),
    ConsumerProfile.TURN_HISTORY: frozenset({"done", "turn_failed", "turn_cancelled"}),
}

_WIRE_TO_LEGACY_STATUS: dict[TerminalStatus, str] = {
    TerminalStatus.COMPLETED: "completed",
    TerminalStatus.FAILED: "failed",
    TerminalStatus.CANCELLED: "cancelled",
    TerminalStatus.ABORTED: "aborted",
    TerminalStatus.CLARIFICATION_PENDING: "clarification_pending",
    TerminalStatus.APPROVAL_PENDING: "awaiting_approval",
}


@dataclass(frozen=True, slots=True)
class TerminalEvent:
    """Predicate 所需的最小 versioned event projection。"""

    event_type: str
    event_id: str | None = None
    generation: int | None = None
    actor_scope: str | None = None
    phase_scope: str | None = None
    terminal_receipt_id: str | None = None
    payload: Mapping[str, object] | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TerminalEvent:
        payload = value.get("payload")
        return cls(
            event_type=value.get("event_type") if isinstance(value.get("event_type"), str) else "",
            event_id=value.get("event_id") if isinstance(value.get("event_id"), str) else None,
            generation=value.get("generation") if _is_int(value.get("generation")) else None,
            actor_scope=value.get("actor_scope") if isinstance(value.get("actor_scope"), str) else None,
            phase_scope=value.get("phase_scope") if isinstance(value.get("phase_scope"), str) else None,
            terminal_receipt_id=(value.get("terminal_receipt_id") if isinstance(value.get("terminal_receipt_id"), str) else None),
            payload=payload if isinstance(payload, Mapping) else None,
        )


def load_terminal_protocol_enum_registry() -> dict[str, tuple[str, ...]]:
    """读取随代码冻结的 v1 enum registry，并拒绝不完整或未来版本。"""

    raw = _load_terminal_protocol_registry_document()
    enums = raw.get("enums")
    if not isinstance(enums, dict):
        raise TerminalProtocolMalformedError("terminal protocol enum registry is malformed")
    parsed: dict[str, tuple[str, ...]] = {}
    for name, values in enums.items():
        if not isinstance(name, str) or not isinstance(values, list) or not values:
            raise TerminalProtocolMalformedError("terminal protocol enum registry is malformed")
        if any(not isinstance(value, str) or not value for value in values):
            raise TerminalProtocolMalformedError("terminal protocol enum registry is malformed")
        if len(values) != len(set(values)):
            raise TerminalProtocolMalformedError("terminal protocol enum registry contains duplicates")
        parsed[name] = tuple(values)
    if frozenset(parsed) != _EXPECTED_ENUM_NAMES:
        raise TerminalProtocolMalformedError("terminal protocol enum registry names mismatch")
    return parsed


def load_terminal_protocol_reason_codes() -> tuple[str, ...]:
    """返回顺序冻结的 B1b v1 reason codes。"""

    raw = _load_terminal_protocol_registry_document()
    values = raw.get("reason_codes")
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise TerminalProtocolMalformedError("terminal protocol reason code registry is malformed")
    parsed = tuple(values)
    if parsed != _EXPECTED_REASON_CODES or len(parsed) != len(set(parsed)):
        raise TerminalProtocolMalformedError("terminal protocol reason code registry mismatch")
    return parsed


def terminal_protocol_enum_values(name: str) -> frozenset[str]:
    """为 schema/reader adapter 提供唯一 canonical enum 值来源。"""

    return _enum_values(name)


def canonical_json_v1(value: object) -> str:
    """产生 CJ1：UTF-8、key 排序、紧凑 JSON、UTC 时间且拒绝 NaN/Infinity。"""

    normalized = _normalize_canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest_v1(value: object) -> str:
    return hashlib.sha256(canonical_json_v1(value).encode("utf-8")).hexdigest()


APPROVAL_EXECUTION_FINAL_TEXT = "审批操作已执行。"


def approval_execution_event_payload(
    *,
    approval_id: str,
    ok: bool,
    result_digest: str | None,
) -> dict[str, object]:
    """Canonical live/recovery projection for one settled approval effect."""

    if not isinstance(approval_id, str) or not approval_id:
        raise ValueError("approval execution identity is invalid")
    if not isinstance(ok, bool):
        raise TypeError("approval execution outcome is invalid")
    if ok and (not isinstance(result_digest, str) or not result_digest):
        raise ValueError("successful approval execution requires result digest")
    if not ok and result_digest is not None:
        raise ValueError("failed approval execution cannot claim a result digest")
    return {
        "approval_id": approval_id,
        "ok": ok,
        "result_digest": result_digest,
        "error_code": None if ok else "approval_execution_failed",
    }


def stable_decision_id(
    *,
    scope: TerminalScope,
    source_kind: DecisionSourceKind,
    source_id: str,
    action: DecisionAction,
) -> str:
    if not isinstance(scope, TerminalScope):
        raise TypeError("decision scope is invalid")
    if not isinstance(source_kind, DecisionSourceKind) or not isinstance(action, DecisionAction):
        raise TypeError("decision enum is invalid")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("decision source identity is invalid")
    digest = canonical_digest_v1(
        {
            "protocol_version": TERMINAL_PROTOCOL_VERSION,
            "team_id": scope.team_id,
            "turn_id": scope.turn_id,
            "prior_generation": scope.generation,
            "source_kind": source_kind.value,
            "source_id": source_id,
            "action": action.value,
        }
    )
    return f"td_{digest[:32]}"


def stable_decision_action_id(decision_id: str, terminal_or_resume_kind: str) -> str:
    if any(not isinstance(value, str) or not value for value in (decision_id, terminal_or_resume_kind)):
        raise ValueError("decision action identity is invalid")
    digest = canonical_digest_v1(
        {
            "decision_id": decision_id,
            "kind": terminal_or_resume_kind,
            "protocol_version": TERMINAL_PROTOCOL_VERSION,
        }
    )
    return f"ta_{digest[:32]}"


def stable_decision_effect_id(*, decision_id: str, effect_kind: str, target_ref: str) -> str:
    if any(not isinstance(value, str) or not value for value in (decision_id, effect_kind, target_ref)):
        raise ValueError("decision effect identity is invalid")
    digest = canonical_digest_v1(
        {
            "decision_id": decision_id,
            "effect_kind": effect_kind,
            "protocol_version": TERMINAL_PROTOCOL_VERSION,
            "target_ref": target_ref,
        }
    )
    return f"tef_{digest[:32]}"


def validate_decision_source_transition(request: DecisionRecordRequest) -> None:
    """Reject every source/action/state/value combination outside the frozen S5 graph."""

    context = request.context
    transition = _DECISION_SOURCE_TRANSITIONS.get((context.source_kind, context.action))
    if transition is None:
        raise ValueError("decision source transition is not allowed")
    if (request.expected_source_state, request.target_source_state) != transition:
        raise ValueError("decision source transition does not match its action")
    values: Mapping[str, object]
    if request.source_values_json is None:
        values = {}
    else:
        parsed = json.loads(request.source_values_json)
        if not isinstance(parsed, Mapping):
            raise ValueError("decision source values must be an object")
        values = parsed
    keys = set(values)
    if context.source_kind is DecisionSourceKind.SYSTEM_CONTROL:
        expected_keys = {"denial_reason_code"} if context.action is DecisionAction.REVOKE else set()
        if keys != expected_keys:
            raise ValueError("system control audit fields are invalid")
        if expected_keys and values.get("denial_reason_code") not in (_SYSTEM_CONTROL_DENIAL_REASON_CODES):
            raise ValueError("system control denial reason is invalid")
        if not context.actor_ref.startswith("system:"):
            raise ValueError("system control requires a namespaced system actor")
        return
    if context.source_kind is DecisionSourceKind.APPROVAL:
        expected_keys = {"arguments_digest"} if context.action is DecisionAction.APPROVE else set()
        if keys != expected_keys:
            raise ValueError("approval source audit fields are invalid")
        if expected_keys and (not isinstance(values.get("arguments_digest"), str) or not values["arguments_digest"]):
            raise ValueError("approval arguments digest is invalid")
        return
    allowed_by_action = {
        DecisionAction.RESUME: {
            "answers_json",
            "chosen_value",
            "chosen_values_json",
            "edited_subs_json",
            "user_directive",
        },
        DecisionAction.RESOLVE_WITHOUT_RESUME: {
            "answers_json",
            "chosen_value",
            "chosen_values_json",
            "edited_subs_json",
            "user_directive",
        },
        DecisionAction.DISMISS: {"dismissed_at"},
        DecisionAction.EXPIRE: set(),
    }
    if keys - allowed_by_action[context.action]:
        raise ValueError("clarification source values do not match the decision action")


def canonical_decision_source_projection(request: DecisionRecordRequest) -> dict[str, object]:
    """Canonical caller-owned source mutation; store-generated timestamps stay outside it."""

    validate_decision_source_transition(request)
    return canonical_decision_source_projection_for(
        source_kind=request.context.source_kind,
        action=request.context.action,
        expected_source_state=request.expected_source_state,
        target_source_state=request.target_source_state,
        source_values_json=request.source_values_json,
    )


def canonical_decision_source_projection_for(
    *,
    source_kind: DecisionSourceKind,
    action: DecisionAction,
    expected_source_state: str,
    target_source_state: str | None,
    source_values_json: str | None,
) -> dict[str, object]:
    values = json.loads(source_values_json) if source_values_json is not None else {}
    return {
        "action": action.value,
        "expected_source_state": expected_source_state,
        "source_kind": source_kind.value,
        "source_values": values,
        "target_source_state": target_source_state,
    }


def canonical_decision_source_audit_projection(request: DecisionRecordRequest) -> dict[str, object]:
    """Redacted immutable audit projection; user-entered values stay in the source row."""

    projection = canonical_decision_source_projection(request)
    values = projection.pop("source_values")
    assert isinstance(values, dict)
    projection["source_value_keys"] = sorted(values)
    projection["source_values_digest"] = canonical_digest_v1(values)
    projection["source_projection_digest"] = canonical_decision_payload_digest(request)
    return projection


def canonical_decision_payload_digest(request: DecisionRecordRequest) -> str:
    return canonical_digest_v1(canonical_decision_source_projection(request))


def canonical_decision_payload_digest_for(
    *,
    source_kind: DecisionSourceKind,
    action: DecisionAction,
    expected_source_state: str,
    target_source_state: str | None,
    source_values_json: str | None,
) -> str:
    return canonical_digest_v1(
        canonical_decision_source_projection_for(
            source_kind=source_kind,
            action=action,
            expected_source_state=expected_source_state,
            target_source_state=target_source_state,
            source_values_json=source_values_json,
        )
    )


def decision_identity_digest(request: DecisionRecordRequest) -> str:
    """Decision idempotency excludes mutable authorization epochs by design."""

    if not isinstance(request, DecisionRecordRequest):
        raise TypeError("decision record request is invalid")
    context = request.context
    return canonical_digest_v1(
        {
            "team_id": context.scope.team_id,
            "session_id": context.scope.session_id,
            "turn_id": context.scope.turn_id,
            "prior_generation": context.scope.generation,
            "prior_receipt_id": context.prior_receipt_id,
            "source_kind": context.source_kind.value,
            "source_id": context.source_id,
            "action": context.action.value,
            "decision_payload_digest": request.decision_payload_digest,
            "requester_ref": context.principals.requester_ref,
            "actor_ref": context.actor_ref,
            "effect_principal_ref": context.principals.effect_principal_ref,
            "side_effects": [
                {
                    "effect_id": effect.effect_id,
                    "effect_kind": effect.effect_kind,
                    "payload_digest": effect.payload_digest,
                    "target_ref": effect.target_ref,
                }
                for effect in request.side_effects
            ],
        }
    )


def _validate_canonical_event_payload(payload_json: str, payload_digest: str) -> None:
    try:
        parsed = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError("terminal event payload JSON is malformed") from exc
    if not isinstance(parsed, dict) or canonical_json_v1(parsed) != payload_json:
        raise ValueError("terminal event payload JSON is not canonical")
    if canonical_digest_v1(parsed) != payload_digest:
        raise ValueError("terminal event payload digest mismatch")


def classify_terminal(
    protocol_version: int | None,
    consumer_profile: ConsumerProfile,
    event: TerminalEvent | Mapping[str, object],
) -> TerminalClassification:
    """按 protocol version 和显式 consumer profile 判断 event 的终止类别。"""

    terminal_event = event if isinstance(event, TerminalEvent) else TerminalEvent.from_mapping(event)
    if protocol_version is None:
        return (
            TerminalClassification.ROOT_COMMITTED
            if terminal_event.event_type in _LEGACY_TERMINAL_EVENTS[consumer_profile]
            else TerminalClassification.NON_TERMINAL
        )
    if not _is_int(protocol_version) or protocol_version != TERMINAL_PROTOCOL_VERSION:
        return TerminalClassification.MALFORMED
    if not _valid_v1_event_envelope(terminal_event):
        return TerminalClassification.MALFORMED

    if terminal_event.event_type == "done":
        return _classify_v1_done(terminal_event)
    if terminal_event.event_type == "error":
        return _classify_v1_root_error(terminal_event)
    return TerminalClassification.NON_TERMINAL


def legacy_terminal_events(consumer_profile: ConsumerProfile) -> frozenset[str]:
    return _LEGACY_TERMINAL_EVENTS[consumer_profile]


def legacy_status_for(status: TerminalStatus | str) -> str:
    try:
        parsed = status if isinstance(status, TerminalStatus) else TerminalStatus(status)
    except ValueError as exc:
        raise TerminalProtocolMalformedError("unknown terminal status") from exc
    return _WIRE_TO_LEGACY_STATUS[parsed]


def validate_final_answer_mode(
    *,
    status: TerminalStatus | str,
    mode: FinalAnswerMode | str,
    final_answer_event_id: str | None,
) -> bool:
    """验证 §4.3 的正文模式，不生成或猜测正文。"""

    try:
        parsed_status = status if isinstance(status, TerminalStatus) else TerminalStatus(status)
        parsed_mode = mode if isinstance(mode, FinalAnswerMode) else FinalAnswerMode(mode)
    except ValueError:
        return False
    has_identity = isinstance(final_answer_event_id, str) and bool(final_answer_event_id)
    if parsed_status is TerminalStatus.COMPLETED:
        return parsed_mode is not FinalAnswerMode.NONE and has_identity
    if parsed_status in {
        TerminalStatus.CANCELLED,
        TerminalStatus.ABORTED,
        TerminalStatus.CLARIFICATION_PENDING,
        TerminalStatus.APPROVAL_PENDING,
    }:
        return parsed_mode is FinalAnswerMode.NONE and not has_identity
    if parsed_mode is FinalAnswerMode.NONE:
        return not has_identity
    return has_identity


def _classify_v1_done(event: TerminalEvent) -> TerminalClassification:
    if event.actor_scope != ActorScope.ROOT:
        return TerminalClassification.MALFORMED
    if event.phase_scope not in {PhaseScope.IN_GENERATION, PhaseScope.CONTROL_TRANSITION}:
        return TerminalClassification.MALFORMED
    if not event.terminal_receipt_id or event.payload is None:
        return TerminalClassification.MALFORMED
    receipt_id = event.payload.get("receipt_id")
    if receipt_id != event.terminal_receipt_id:
        return TerminalClassification.MALFORMED
    status = event.payload.get("status")
    if not isinstance(status, str) or status not in _enum_values("terminal_status"):
        return TerminalClassification.MALFORMED
    durability = event.payload.get("durability")
    if durability is not None and durability != RootDurability.COMMITTED:
        return TerminalClassification.MALFORMED
    return TerminalClassification.ROOT_COMMITTED


def _classify_v1_root_error(event: TerminalEvent) -> TerminalClassification:
    if event.actor_scope in {ActorScope.PEER, ActorScope.STEP}:
        return TerminalClassification.NON_TERMINAL
    if event.actor_scope not in {ActorScope.ROOT, ActorScope.TRANSPORT}:
        return TerminalClassification.MALFORMED
    if event.phase_scope == PhaseScope.POST_TERMINAL_AUXILIARY or event.terminal_receipt_id is not None:
        return TerminalClassification.MALFORMED
    if event.payload is None:
        return TerminalClassification.MALFORMED
    durability = event.payload.get("durability")
    if durability == RootDurability.NON_DURABLE:
        return TerminalClassification.ROOT_NON_DURABLE
    if durability == RootDurability.UNKNOWN:
        return TerminalClassification.ROOT_UNKNOWN
    return TerminalClassification.MALFORMED


def _valid_v1_event_envelope(event: TerminalEvent) -> bool:
    return (
        bool(event.event_type)
        and bool(event.event_id)
        and _is_int(event.generation)
        and int(event.generation) >= 1
        and event.actor_scope in _enum_values("actor_scope")
        and event.phase_scope in _enum_values("phase_scope")
        and event.payload is not None
    )


def _enum_values(name: str) -> frozenset[str]:
    values = _ENUM_REGISTRY.get(name)
    if values is None:
        raise TerminalProtocolMalformedError(f"terminal protocol enum missing: {name}")
    return frozenset(values)


def _normalize_canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical JSON requires timezone-aware datetimes")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _normalize_canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_canonical_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON rejects NaN and Infinity")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _load_terminal_protocol_registry_document() -> dict[str, object]:
    path = Path(__file__).with_name("terminal_protocol_v1_enums.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TerminalProtocolMalformedError("terminal protocol enum registry is malformed")
    if raw.get("schema_version") != TERMINAL_PROTOCOL_ENUM_REGISTRY_VERSION:
        raise TerminalProtocolMalformedError("terminal protocol enum registry version mismatch")
    return raw


_ENUM_REGISTRY = load_terminal_protocol_enum_registry()
_REASON_CODES = load_terminal_protocol_reason_codes()

if set(TerminalStatus) != set(_ENUM_REGISTRY["terminal_status"]):
    raise TerminalProtocolMalformedError("TerminalStatus does not match enum registry")
if set(GenerationState) != set(_ENUM_REGISTRY["generation_state"]):
    raise TerminalProtocolMalformedError("GenerationState does not match enum registry")
if set(ActorScope) != set(_ENUM_REGISTRY["actor_scope"]):
    raise TerminalProtocolMalformedError("ActorScope does not match enum registry")
if set(PhaseScope) != set(_ENUM_REGISTRY["phase_scope"]):
    raise TerminalProtocolMalformedError("PhaseScope does not match enum registry")
if set(RootDurability) != set(_ENUM_REGISTRY["root_durability"]):
    raise TerminalProtocolMalformedError("RootDurability does not match enum registry")
if set(FinalAnswerMode) != set(_ENUM_REGISTRY["final_answer_mode"]):
    raise TerminalProtocolMalformedError("FinalAnswerMode does not match enum registry")
