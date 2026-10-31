"""Terminal Protocol v1 的 Core Store。

S2 在 S1 admission/claim fence 上增加 durable event/checkpoint seam；S3 增加
candidate、receipt、finalizer、recovery、effect 与 retention seam。这些 writer API 仍未接入
Gateway/runtime；所有 v1 写事务继续使用本模块的 FULL/foreign-key connection helper。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import aiosqlite

from bowhead_core.agent_draft_send_target import AgentDraftSendTarget
from bowhead_core.audit_actions import AuditAction
from bowhead_core.chat_turn_source import (
    chat_turn_user_source_projection,
    snapshot_chat_turn_user_source,
)
from bowhead_core.iana_time_zone import load_iana_time_zone
from bowhead_core.language_context import LanguageContext
from bowhead_core.rate_card import ModelUsageLine, credits_for_tokens_at_version
from bowhead_core.session_store import (
    _cancel_running_plan_for_turn_in_tx,
    _ensure_chat_session_in_tx,
    _insert_chat_turn_user_source_in_tx,
    _read_chat_turn_user_source_in_tx,
)
from bowhead_core.terminal_effect_contract import (
    EffectDisclosureKind,
    EffectExecutionContract,
    EffectIntentDisposition,
    EffectUsageEvidence,
    effect_control_identity_digest,
    get_effect_provider_contract,
    resolve_effect_execution_contract,
)
from bowhead_core.terminal_protocol import (
    EXPLICIT_STOP_CANCELLED_REASON,
    TERMINAL_PROTOCOL_TABLES,
    WEB_ACTION_MAX_EXECUTION_GENERATION,
    AcceptedEventProposal,
    ActorScope,
    AdmissionContext,
    AuthFenceEpoch,
    AuthFenceResolveContext,
    AuthFenceScope,
    AuthFenceScopeKind,
    AuthFenceVector,
    CandidateOrigin,
    CandidateOutcome,
    ClaimedDecisionSideEffect,
    CommitConfirmation,
    CommitConfirmationDisposition,
    CommittedReceipt,
    ConsumerProfile,
    ControlGenerationRequest,
    DecisionAction,
    DecisionContext,
    DecisionOpenDisposition,
    DecisionPreflightSnapshot,
    DecisionPrincipalSet,
    DecisionRecordRequest,
    DecisionSideEffectPlan,
    DecisionSourceKind,
    DecisionState,
    EffectAttemptContext,
    EffectAttemptDetail,
    EffectClass,
    EffectDisclosureAttempt,
    EffectDisclosureOverlay,
    EffectDisclosureSnapshot,
    EffectDispatchClaim,
    EffectRetentionContext,
    EffectRetentionResult,
    EffectSettlementContext,
    EffectSettlementOutcome,
    EffectSettlementSource,
    EffectState,
    EffectUnknownRecoveryTransition,
    EventPublishMode,
    EventTicket,
    EvidencePurgeContext,
    EvidencePurgeReceipt,
    ExecutionGenerationContext,
    FinalAnswerMode,
    FinalizationContext,
    FrozenSteerItem,
    FrozenSteerSnapshot,
    GenerationClaimContext,
    GenerationPointer,
    GenerationState,
    PendingDecisionRecovery,
    PhaseScope,
    PostTerminalAuxiliaryContext,
    PreparedFinalizationContext,
    PreparedRecoveryContext,
    ProtocolDeleteResult,
    RecordedDecision,
    RecoveryDiscoveryContext,
    RecoveryEffectContext,
    RecoveryKind,
    RecoveryProcessingContext,
    RecoveryRecord,
    RecoveryWriterFence,
    ResumeGenerationRequest,
    ResumeGenerationResult,
    RootDurability,
    SessionVariant,
    StagedTerminalEvent,
    SubscriberFenceToken,
    TerminalAdmissionRequest,
    TerminalCandidatePlan,
    TerminalCandidateVariant,
    TerminalClassification,
    TerminalDeleteContext,
    TerminalEvent,
    TerminalFlushResult,
    TerminalProtocolFutureVersionError,
    TerminalProtocolMalformedError,
    TerminalScope,
    TerminalStatus,
    TerminalWriterLease,
    canonical_decision_payload_digest,
    canonical_decision_payload_digest_for,
    canonical_decision_source_audit_projection,
    canonical_decision_source_projection_for,
    canonical_digest_v1,
    canonical_json_v1,
    classify_terminal,
    decision_identity_digest,
    is_explicit_user_cancel_receipt,
    is_narrow_system_decision,
    is_web_cancel_decision,
    recovery_effects_safe,
    stable_approval_effect_action_ref,
    stable_decision_id,
    terminal_protocol_enum_values,
    validate_final_answer_mode,
)
from bowhead_core.user_store import (
    UserStore,
    _deduct_credit_with_stable_id_in_tx,
    _read_team_role_in_tx,
    _stable_credit_ledger_row_exists_in_tx,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bowhead_core.context_activity import ContextActivity
    from bowhead_core.context_publication import StagedTerminalContext

_REQUIRED_TURN_COLUMNS = frozenset(
    {
        "terminal_protocol_version",
        "terminal_ingress_surface",
        "terminal_admission_cohort",
        "protocol_admitted_at",
        "current_generation",
        "generation_transition_epoch",
        "last_terminal_generation",
        "current_terminal_receipt_id",
        "event_sequence_next",
        "delivery_fenced_at",
        "delivery_fence_epoch",
        "presentation_locale",
        "message_catalog_version",
    }
)

_REQUIRED_SESSION_COLUMNS = frozenset(
    {
        "active_turn_protocol_version",
        "active_turn_generation",
        "active_turn_claim_epoch",
        "recovery_lock_turn_id",
        "recovery_lock_generation",
        "recovery_lock_reason",
        "steer_buffer_revision",
        "steer_freeze_turn_id",
        "steer_freeze_generation",
        "steer_freeze_claim_epoch",
        "steer_frozen_revision",
        "steer_buffer_state",
    }
)

_REQUIRED_EVENT_COLUMNS = frozenset(
    {
        "protocol_version",
        "generation",
        "event_seq",
        "actor_scope",
        "phase_scope",
        "payload_digest",
        "terminal_receipt_id",
        "source_proposal_id",
        "accept_ordinal",
    }
)

_REQUIRED_DECISION_COLUMNS = frozenset(
    {
        "requester_ref",
        "effect_principal_ref",
        "source_projection_json",
        "source_projection_digest",
        "prior_receipt_id",
        "prior_generation_transition_epoch",
        "recovery_attempt_count",
        "recovery_next_attempt_at",
        "recovery_last_code",
        "control_outcome_code",
    }
)

_REQUIRED_GENERATION_COLUMNS = frozenset(
    {
        "admission_request_digest",
        "claim_authz_snapshot_digest",
        "execution_intent_json",
        "execution_authz_snapshot_digest",
    }
)

_WRITER_LEASE_NAME = "v1_writer"
_EXECUTION_LEASE_SECONDS = 30
_FINALIZATION_LEASE_SECONDS = 30
_RECOVERY_LEASE_SECONDS = 30
TERMINAL_TASK_START_GRACE_SECONDS = 30
_CANCELLED_FINALIZATION_REASON = EXPLICIT_STOP_CANCELLED_REASON


class TerminalProtocolFaultPoint(StrEnum):
    """仅供进程内测试注入；没有环境变量、HTTP 或生产配置入口。"""

    ADMISSION_BEFORE_COMMIT = "ADMISSION_BEFORE_COMMIT"
    ADMISSION_AFTER_COMMIT = "ADMISSION_AFTER_COMMIT"
    CLAIM_AFTER_GENERATION_CAS = "CLAIM_AFTER_GENERATION_CAS"
    CLAIM_AFTER_SESSION_EPOCH_CAS = "CLAIM_AFTER_SESSION_EPOCH_CAS"
    CLAIM_AFTER_COMMIT = "CLAIM_AFTER_COMMIT"
    EXECUTION_BEFORE_STARTED = "EXECUTION_BEFORE_STARTED"
    EVENT_AFTER_DURABLE_ACCEPT = "EVENT_AFTER_DURABLE_ACCEPT"
    EVENT_BEFORE_PERSIST = "EVENT_BEFORE_PERSIST"
    EVENT_AFTER_PERSIST = "EVENT_AFTER_PERSIST"
    FINALIZE_AFTER_CLOSING = "FINALIZE_AFTER_CLOSING"
    FINALIZE_AFTER_BARRIER = "FINALIZE_AFTER_BARRIER"
    FINALIZE_AFTER_FLUSH = "FINALIZE_AFTER_FLUSH"
    CANDIDATE_BEFORE_PREPARE_CAS = "CANDIDATE_BEFORE_PREPARE_CAS"
    CANDIDATE_AFTER_PREPARE = "CANDIDATE_AFTER_PREPARE"
    TERMINAL_BEFORE_SEQ_ALLOC = "TERMINAL_BEFORE_SEQ_ALLOC"
    TERMINAL_AFTER_SEQ_ALLOC = "TERMINAL_AFTER_SEQ_ALLOC"
    TERMINAL_BEFORE_COMMIT = "TERMINAL_BEFORE_COMMIT"
    TERMINAL_AFTER_COMMIT_BEFORE_ACK = "TERMINAL_AFTER_COMMIT_BEFORE_ACK"
    EFFECT_AFTER_INTENT = "EFFECT_AFTER_INTENT"
    EFFECT_AFTER_DISPATCH = "EFFECT_AFTER_DISPATCH"
    EFFECT_BEFORE_SETTLE = "EFFECT_BEFORE_SETTLE"
    EFFECT_SETTLEMENT_AFTER_EVIDENCE_LINK = "EFFECT_SETTLEMENT_AFTER_EVIDENCE_LINK"
    EFFECT_SETTLEMENT_AFTER_INSERT = "EFFECT_SETTLEMENT_AFTER_INSERT"
    EFFECT_SETTLEMENT_BEFORE_ATTEMPT_CAS = "EFFECT_SETTLEMENT_BEFORE_ATTEMPT_CAS"
    LLM_COMPLETION_AFTER_USAGE = "LLM_COMPLETION_AFTER_USAGE"
    EFFECT_UNKNOWN_AFTER_ATTEMPT_CAS = "EFFECT_UNKNOWN_AFTER_ATTEMPT_CAS"
    LOCAL_BILLING_AFTER_USAGE_FREEZE = "LOCAL_BILLING_AFTER_USAGE_FREEZE"
    LOCAL_BILLING_AFTER_LEDGER = "LOCAL_BILLING_AFTER_LEDGER"
    LOCAL_BILLING_AFTER_COMMIT_BEFORE_ACK = "LOCAL_BILLING_AFTER_COMMIT_BEFORE_ACK"
    CANCEL_FINALIZATION_BEFORE_COMMIT = "CANCEL_FINALIZATION_BEFORE_COMMIT"
    EFFECT_FINAL_OVERLAY_BEFORE_EVENT = "EFFECT_FINAL_OVERLAY_BEFORE_EVENT"
    RECOVERY_DISCOVERY_AFTER_SCAN = "RECOVERY_DISCOVERY_AFTER_SCAN"
    RECOVERY_DISCOVERY_AFTER_ROW_CREATE = "RECOVERY_DISCOVERY_AFTER_ROW_CREATE"
    RECOVERY_AFTER_STATE_CAS = "RECOVERY_AFTER_STATE_CAS"
    FINALIZATION_AFTER_OWNER_TRANSFER = "FINALIZATION_AFTER_OWNER_TRANSFER"
    RECOVERY_AFTER_CANDIDATE_PREPARE = "RECOVERY_AFTER_CANDIDATE_PREPARE"
    DELETE_AFTER_LEDGER = "DELETE_AFTER_LEDGER"
    DELETE_AFTER_GENERATION_CAS = "DELETE_AFTER_GENERATION_CAS"
    DELETE_BEFORE_COMMIT = "DELETE_BEFORE_COMMIT"
    EVIDENCE_PURGE_AFTER_CLAIM = "EVIDENCE_PURGE_AFTER_CLAIM"
    EVIDENCE_PURGE_BEFORE_COMMIT = "EVIDENCE_PURGE_BEFORE_COMMIT"
    RETENTION_AFTER_TOMBSTONE = "RETENTION_AFTER_TOMBSTONE"
    RETENTION_AFTER_BUNDLE_DELETE = "RETENTION_AFTER_BUNDLE_DELETE"
    AUTH_SCOPE_AFTER_ENSURE = "AUTH_SCOPE_AFTER_ENSURE"
    TRANSITION_WATCH_AFTER_POINTER_READ = "TRANSITION_WATCH_AFTER_POINTER_READ"
    DECISION_BEFORE_SOURCE_CAS = "DECISION_BEFORE_SOURCE_CAS"
    DECISION_AFTER_SOURCE_CAS_BEFORE_INSERT = "DECISION_AFTER_SOURCE_CAS_BEFORE_INSERT"
    DECISION_AFTER_COMMIT_BEFORE_ACK = "DECISION_AFTER_COMMIT_BEFORE_ACK"
    RESUME_BEFORE_EVENT_ALLOC = "RESUME_BEFORE_EVENT_ALLOC"
    RESUME_AFTER_EVENT_BEFORE_POINTER = "RESUME_AFTER_EVENT_BEFORE_POINTER"
    RESUME_AFTER_OPEN_BEFORE_CLAIM = "RESUME_AFTER_OPEN_BEFORE_CLAIM"
    CONTROL_BEFORE_CANDIDATE = "CONTROL_BEFORE_CANDIDATE"
    CONTROL_AFTER_RECEIPT_BEFORE_ACK = "CONTROL_AFTER_RECEIPT_BEFORE_ACK"
    RECOVERY_FENCE_AFTER_CAS = "RECOVERY_FENCE_AFTER_CAS"
    RESUME_BEFORE_TASK_CREATE = "RESUME_BEFORE_TASK_CREATE"
    FOLLOWUP_AFTER_PARENT_RECEIPT_BEFORE_CLAIM = "FOLLOWUP_AFTER_PARENT_RECEIPT_BEFORE_CLAIM"
    FOLLOWUP_AFTER_CLAIM_BEFORE_TASK = "FOLLOWUP_AFTER_CLAIM_BEFORE_TASK"
    RECOVERY_STARTUP_BEFORE_OLD_LEASE_EXPIRY = "RECOVERY_STARTUP_BEFORE_OLD_LEASE_EXPIRY"
    RECOVERY_AFTER_EFFECT_SETTLEMENT_BEFORE_PROJECTION = "RECOVERY_AFTER_EFFECT_SETTLEMENT_BEFORE_PROJECTION"
    RECOVERY_AFTER_PROJECTION_BEFORE_FINALIZER = "RECOVERY_AFTER_PROJECTION_BEFORE_FINALIZER"


@dataclass(frozen=True, slots=True)
class LLMEffectSettlementResult:
    attempt: EffectAttemptContext
    continue_execution: bool
    usage_recorded: bool


class AdmissionDisposition(StrEnum):
    ADMITTED = "admitted"
    ADMITTED_REPLAY = "admitted_replay"
    STEERED = "steered"
    STEER_REPLAY = "steer_replay"
    REJECTED = "rejected"


class AdmissionCode(StrEnum):
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_ACCESS_DENIED = "session_access_denied"
    SESSION_DELETED = "session_deleted"
    SESSION_RECOVERY_LOCKED = "session_recovery_locked"
    SESSION_PRIOR_EFFECT_UNKNOWN = "session_prior_effect_unknown"
    SESSION_TRANSITIONING = "session_transitioning"
    SESSION_TURN_ACTIVE = "session_turn_active"
    STEER_TARGET_CHANGED = "terminal_steer_target_changed"
    SESSION_DEFERRED_PENDING = "session_deferred_pending"
    WRITER_NOT_READY = "terminal_writer_not_ready"
    AUTH_FENCE_CHANGED = "terminal_auth_fence_changed"
    REQUEST_CONFLICT = "terminal_admission_request_conflict"


class GenerationClaimDisposition(StrEnum):
    CLAIMED = "claimed"
    CLAIM_REPLAY = "claim_replay"
    REJECTED = "rejected"


class GenerationClaimCode(StrEnum):
    SCOPE_NOT_FOUND = "terminal_generation_not_found"
    WRITER_LEASE_UNAVAILABLE = "terminal_writer_lease_unavailable"
    GENERATION_NOT_OPENED = "terminal_generation_not_opened"
    GENERATION_ALREADY_CLAIMED = "terminal_generation_already_claimed"
    GENERATION_VERSION_CHANGED = "terminal_generation_version_changed"
    SESSION_CLAIM_CHANGED = "terminal_session_claim_changed"
    AUTH_FENCE_CHANGED = "terminal_auth_fence_changed"


class DecisionConflictError(TerminalProtocolMalformedError):
    """A durable decision source was consumed by a different identity."""


class DecisionAccessRevokedError(TerminalProtocolMalformedError):
    """The current authorization vector no longer permits the decision."""


class DecisionAuthorizationDeniedError(DecisionAccessRevokedError):
    """A current policy resolver produced a finite permanent denial."""

    def __init__(self, reason_code: str) -> None:
        if reason_code not in {
            "access_revoked",
            "dependency_missing",
            "entitlement_denied",
        }:
            raise ValueError("decision denial reason is invalid")
        super().__init__(reason_code)
        self.reason_code = reason_code


class DecisionSourceNotReadyError(TerminalProtocolMalformedError):
    """The shared decision source schema or persisted protocol is unavailable."""


@dataclass(frozen=True, slots=True)
class TerminalAdmissionResult:
    disposition: AdmissionDisposition
    code: AdmissionCode | None
    request_id: str
    request_digest: str
    scope: TerminalScope | None = None
    active_turn_id: str | None = None
    active_protocol_version: int | None = None
    active_generation: int | None = None
    steer_revision: int | None = None
    effective_user_text: str | None = None
    effective_execution_intent: Mapping[str, object] | None = None
    effective_presentation_locale: str | None = None
    steer_ack_proposal: AcceptedEventProposal | None = None
    execution_started: bool | None = None

    def __post_init__(self) -> None:
        if self.disposition is AdmissionDisposition.REJECTED:
            if self.code is None or self.scope is not None:
                raise ValueError("rejected admission result shape is invalid")
        elif self.code is not None:
            raise ValueError("accepted admission result cannot carry an error code")
        if self.disposition in {AdmissionDisposition.ADMITTED, AdmissionDisposition.ADMITTED_REPLAY} and self.scope is None:
            raise ValueError("admitted result must carry a terminal scope")
        envelope = (
            self.effective_user_text,
            self.effective_execution_intent,
            self.effective_presentation_locale,
        )
        if self.disposition is AdmissionDisposition.ADMITTED and any(value is None for value in envelope):
            raise ValueError("admitted result must carry its durable execution envelope")
        if (
            self.disposition is AdmissionDisposition.ADMITTED_REPLAY
            and any(value is not None for value in envelope)
            and any(value is None for value in envelope)
        ):
            raise ValueError("replayed admission execution envelope is incomplete")
        if self.steer_ack_proposal is not None:
            if self.disposition not in {
                AdmissionDisposition.STEERED,
                AdmissionDisposition.STEER_REPLAY,
            }:
                raise ValueError("steer acknowledgement requires a steer result")
            if self.scope != self.steer_ack_proposal.scope:
                raise ValueError("steer acknowledgement scope changed")
            if not isinstance(self.execution_started, bool):
                raise ValueError("steer acknowledgement execution state is missing")
        elif self.execution_started is not None:
            raise ValueError("execution state requires a steer acknowledgement")

    @property
    def accepted(self) -> bool:
        return self.disposition is not AdmissionDisposition.REJECTED


# Execution-intent keys whose persisted values are resolved by the server at
# first admission from team state (the team model catalog that validates the
# requested model, the Datarc connection and membership) rather than copied
# verbatim from the client payload.
WEB_START_ADMITTED_RESOLUTION_KEYS = frozenset(
    {
        "active_datarc_project",
        "member_uid",
        "datarc_selection",
        "preferred_model",
        "preferred_provider",
    }
)
# Historical admissions predate the explicit Datarc source snapshot. Lookup
# preserves their exact projection; it must not invent current source evidence.
_WEB_START_LEGACY_RESOLUTION_KEYS = WEB_START_ADMITTED_RESOLUTION_KEYS - {"datarc_selection"}


@dataclass(frozen=True, slots=True)
class WebChatActionLookup:
    """A read-only projection of an existing Web Terminal admission fact."""

    action_id: str
    action_kind: Literal["start", "steer"]
    scope: TerminalScope
    workspace_id: str
    request_digest: str | None = None
    paradigm_v2: bool | None = None
    planning_mode: str | bool | None = None
    plan_execution_id: str | None = None
    expected_plan_revision: int | None = None
    # Language snapshot resolved at first admission. Replay compares against it
    # instead of re-resolving, because resolution depends on server state and
    # feature flags that may change between the first POST and its retry.
    language_context: Mapping[str, object] | None = None
    # Viewer zone admitted beside a missing language snapshot (i18n disabled); "" = none.
    viewer_time_zone: str = ""
    # Server-resolved model selection and Datarc identity snapshot from first
    # admission (keys: WEB_START_ADMITTED_RESOLUTION_KEYS). Replay compares
    # against it instead of re-resolving team model catalogs or Datarc
    # membership, which may change between the first POST and its retry.
    # None means the envelope cannot prove the snapshot; replay fails closed.
    admitted_resolution: Mapping[str, object] | None = None
    agent_draft_target: Mapping[str, object] | None = None
    context_maintenance: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.agent_draft_target is not None:
            AgentDraftSendTarget.model_validate(dict(self.agent_draft_target))
            if self.action_kind != "start":
                raise ValueError("draft targets require a start action")
        if self.context_maintenance is not None:
            from bowhead_core.context_action import ContextMaintenanceAction

            ContextMaintenanceAction.model_validate(dict(self.context_maintenance))
            if self.action_kind != "start" or self.agent_draft_target is not None:
                raise ValueError("maintenance requires its own start action")
        if not isinstance(self.action_id, str) or not self.action_id:
            raise ValueError("chat action identity is invalid")
        if self.action_kind not in {"start", "steer"}:
            raise ValueError("chat action kind is invalid")
        if not isinstance(self.scope, TerminalScope):
            raise TypeError("chat action scope is invalid")
        if not isinstance(self.workspace_id, str) or not self.workspace_id:
            raise ValueError("chat action workspace is invalid")
        if self.action_kind == "start" and not self.request_digest:
            raise ValueError("chat start action request digest is missing")
        if self.request_digest is not None and (not isinstance(self.request_digest, str) or not self.request_digest):
            raise ValueError("chat action request digest is invalid")
        if self.planning_mode is not None and not isinstance(
            self.planning_mode,
            (str, bool),
        ):
            raise ValueError("chat action planning mode is invalid")
        if self.plan_execution_id is not None and (
            not isinstance(self.plan_execution_id, str)
            or not self.plan_execution_id
            or self.plan_execution_id.strip() != self.plan_execution_id
            or len(self.plan_execution_id) > 128
        ):
            raise ValueError("chat action Plan execution identity is invalid")
        if self.expected_plan_revision is not None and (
            type(self.expected_plan_revision) is not int or self.expected_plan_revision < 1 or self.plan_execution_id is None
        ):
            raise ValueError("chat action Plan revision is invalid")
        if self.paradigm_v2 is not None and not isinstance(self.paradigm_v2, bool):
            raise TypeError("chat action paradigm snapshot is invalid")
        if self.language_context is not None and not isinstance(self.language_context, Mapping):
            raise TypeError("chat action language snapshot is invalid")
        if self.admitted_resolution is not None and (
            not isinstance(self.admitted_resolution, Mapping)
            or set(self.admitted_resolution) not in (WEB_START_ADMITTED_RESOLUTION_KEYS, _WEB_START_LEGACY_RESOLUTION_KEYS)
        ):
            raise TypeError("chat action resolution snapshot is invalid")


@dataclass(frozen=True, slots=True)
class WebChatActionAccessScope:
    """Persisted tenant scope used only to authorize an action error."""

    team_id: str
    session_id: str
    turn_id: str
    generation: int
    workspace_id: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.team_id,
                self.session_id,
                self.turn_id,
                self.workspace_id,
            )
        ):
            raise ValueError("chat action access scope is invalid")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 1:
            raise ValueError("chat action access generation is invalid")


class WebChatActionFutureVersionError(TerminalProtocolFutureVersionError):
    """Future action state carrying only the persisted scope needed for ACL."""

    def __init__(
        self,
        message: str,
        *,
        access_scope: WebChatActionAccessScope | None,
    ) -> None:
        super().__init__(message)
        self.access_scope = access_scope


class WebChatActionMalformedError(TerminalProtocolMalformedError):
    """Malformed action state carrying only the persisted scope needed for ACL."""

    def __init__(
        self,
        message: str,
        *,
        access_scope: WebChatActionAccessScope | None,
    ) -> None:
        super().__init__(message)
        self.access_scope = access_scope


@dataclass(frozen=True, slots=True)
class ActiveV1ControlAuthorization:
    """Read-only CAS result for an in-process control of the active v1 Turn."""

    scope: TerminalScope | None
    code: AdmissionCode | None

    def __post_init__(self) -> None:
        if (self.scope is None) == (self.code is None):
            raise ValueError("active v1 control authorization shape is invalid")


@dataclass(frozen=True, slots=True)
class GenerationClaimResult:
    disposition: GenerationClaimDisposition
    code: GenerationClaimCode | None
    context: ExecutionGenerationContext | None = None
    observed_state: GenerationState | None = None
    observed_owner_instance_id: str | None = None
    observed_claim_epoch: int | None = None

    def __post_init__(self) -> None:
        if self.disposition is GenerationClaimDisposition.CLAIMED:
            if self.context is None or self.code is not None:
                raise ValueError("claimed generation result shape is invalid")
        elif self.context is not None:
            raise ValueError("non-winning generation claim cannot carry execution authority")
        if self.disposition is GenerationClaimDisposition.REJECTED and self.code is None:
            raise ValueError("rejected generation claim must carry a code")
        if self.disposition is GenerationClaimDisposition.CLAIM_REPLAY and self.code is not None:
            raise ValueError("generation claim replay cannot carry an error code")

    @property
    def claimed(self) -> bool:
        """只有本调用赢得 CAS 才能据此创建 task；replay 只能 attach。"""

        return self.disposition is GenerationClaimDisposition.CLAIMED


@asynccontextmanager
async def open_terminal_protocol_connection(db_path: str) -> AsyncIterator[aiosqlite.Connection]:
    """打开 v1 correctness connection；FULL 与 FK 失败时不泄漏连接。"""

    connection = await aiosqlite.connect(db_path)
    try:
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("PRAGMA synchronous=FULL")
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA secure_delete=ON")
    except BaseException:
        await connection.close()
        raise
    try:
        yield connection
    finally:
        await connection.close()


@dataclass(frozen=True, slots=True)
class TerminalReadContext:
    team_id: str
    session_id: str
    turn_id: str
    viewer: str
    auth_fence_vector_digest: str

    def __post_init__(self) -> None:
        if not all((self.team_id, self.session_id, self.turn_id, self.viewer, self.auth_fence_vector_digest)):
            raise ValueError("terminal read context fields must be non-empty")


@dataclass(frozen=True, slots=True)
class TerminalReceiptSummary:
    receipt_id: str
    candidate_id: str
    generation: int
    status: TerminalStatus
    reason_code: str | None
    message_key: str
    final_answer_mode: FinalAnswerMode
    final_answer_event_id: str | None
    selected_variant_key: str
    required_manifest_template_digest: str
    terminal_event_id: str
    first_event_seq: int
    last_event_seq: int
    persisted_event_highwater: int
    committed_at: str
    operation_kind: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalRecoverySummary:
    recovery_id: str
    kind: str
    phase: str
    state: str
    last_code: str | None
    next_attempt_at: str | None


@dataclass(frozen=True, slots=True)
class _LocalBillingFact:
    scope: TerminalScope
    user_id: str
    workspace_id: str
    rate_card_version: str
    input_tokens: int
    output_tokens: int
    cost_amount: str
    cost_credits: int
    slot_digest: str
    fact_digest: str
    usage_digest: str
    ledger_id: str
    action_ref: str
    attempt_id: str
    provider: str
    model_usage: tuple[ModelUsageLine, ...] = ()


@dataclass(frozen=True, slots=True)
class TerminalProtocolSummary:
    version: int | None
    current_generation: int | None = None
    current_generation_kind: str | None = None
    current_generation_closing_reason: str | None = None
    generation_transition_epoch: int | None = None
    current_generation_state: GenerationState | None = None
    current_generation_resolution: str | None = None
    last_terminal_generation: int | None = None
    event_sequence_next: int | None = None
    delivery_fence_epoch: int | None = None
    current_receipt: TerminalReceiptSummary | None = None
    recovery: TerminalRecoverySummary | None = None
    ingress_surface: str | None = None
    admission_cohort: str | None = None
    presentation_locale: str | None = None
    message_catalog_version: int | None = None


@dataclass(frozen=True, slots=True)
class SessionActiveClaimSummary:
    turn_id: str
    protocol_version: int
    generation: int
    claim_epoch: int


@dataclass(frozen=True, slots=True)
class SessionProtocolFacts:
    """Persisted protocol phase and active identity for one authorized Session."""

    has_v1_history: bool
    legacy_active_turn_id: str | None = None
    v1_active_claim: SessionActiveClaimSummary | None = None

    def __post_init__(self) -> None:
        if self.legacy_active_turn_id is not None and self.v1_active_claim is not None:
            raise ValueError("Session protocol facts contain two active Turns")
        if self.v1_active_claim is not None and not self.has_v1_history:
            raise ValueError("active v1 claim requires v1 history")
        if self.legacy_active_turn_id is not None and self.has_v1_history:
            raise ValueError("v1 Session cannot have a legacy active Turn")


@dataclass(frozen=True, slots=True)
class RequiredEventSummary:
    event_id: str
    event_seq: int
    event_type: str
    actor_scope: str
    phase_scope: str
    canonical_row_digest: str


@dataclass(frozen=True, slots=True)
class TerminalCheckpoint:
    protocol_version: int
    turn_id: str
    current_generation: int
    generation_transition_epoch: int
    current_generation_state: GenerationState
    current_generation_resolution: str | None
    last_terminal_generation: int | None
    persistent_event_highwater: int
    current_receipt_summary: TerminalReceiptSummary | None
    required_event_manifest: tuple[RequiredEventSummary, ...]
    recovery_summary: TerminalRecoverySummary | None
    session_active_claim: SessionActiveClaimSummary | None
    delivery_fence_epoch: int
    auth_fence_vector_digest: str
    checkpoint_token: str

    @property
    def generation_pointer(self) -> GenerationPointer:
        return GenerationPointer(
            generation=self.current_generation,
            generation_transition_epoch=self.generation_transition_epoch,
            delivery_fence_epoch=self.delivery_fence_epoch,
        )

    @property
    def subscriber_fence_token(self) -> SubscriberFenceToken:
        return SubscriberFenceToken(
            generation=self.current_generation,
            generation_transition_epoch=self.generation_transition_epoch,
            delivery_fence_epoch=self.delivery_fence_epoch,
            auth_fence_vector_digest=self.auth_fence_vector_digest,
        )


@dataclass(frozen=True, slots=True)
class TerminalProtocolEventRow:
    event_id: str
    turn_id: str
    generation: int
    event_seq: int
    event_type: str
    actor_scope: str
    phase_scope: str
    terminal_receipt_id: str | None
    payload: dict[str, object]
    payload_digest: str
    occurred_at: str

    def as_terminal_event(self) -> TerminalEvent:
        return TerminalEvent(
            event_type=self.event_type,
            event_id=self.event_id,
            generation=self.generation,
            actor_scope=self.actor_scope,
            phase_scope=self.phase_scope,
            terminal_receipt_id=self.terminal_receipt_id,
            payload=self.payload,
        )


class TerminalProtocolStore:
    """v1 transaction owner；schema 缺失时只报迁移错误，绝不运行 DDL。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        _clock: Callable[[], datetime] | None = None,
        _fault_injector: Callable[[TerminalProtocolFaultPoint], None] | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._clock = _clock or (lambda: datetime.now(UTC))
        self._fault_injector = _fault_injector

    @property
    def db_path(self) -> str:
        return self._db_path

    async def initialize(self) -> None:
        async with open_terminal_protocol_connection(self._db_path) as connection:
            tables = await _schema_objects(connection, "table")
            turn_columns = await _table_columns(connection, "turns")
            session_columns = await _table_columns(connection, "sessions")
            event_columns = await _table_columns(connection, "turn_events")
            decision_columns = await _table_columns(connection, "turn_control_decisions")
            generation_columns = await _table_columns(connection, "turn_generations")
        missing = sorted(
            (TERMINAL_PROTOCOL_TABLES - tables)
            | {f"turns.{name}" for name in _REQUIRED_TURN_COLUMNS - turn_columns}
            | {f"sessions.{name}" for name in _REQUIRED_SESSION_COLUMNS - session_columns}
            | {f"turn_events.{name}" for name in _REQUIRED_EVENT_COLUMNS - event_columns}
            | {f"turn_control_decisions.{name}" for name in _REQUIRED_DECISION_COLUMNS - decision_columns}
            | {f"turn_generations.{name}" for name in _REQUIRED_GENERATION_COLUMNS - generation_columns}
        )
        if missing:
            raise RuntimeError(f"terminal protocol schema missing; run bowhead migrate upgrade head: {missing[0]}")

    async def read_session_protocol_facts(
        self,
        *,
        team_id: str,
        actor: str,
        workspace_id: str,
        session_id: str,
    ) -> SessionProtocolFacts | None:
        """Read one authorized Session's protocol phase from a single snapshot.

        Scope misses are opaque. Future or malformed persisted protocol facts
        remain typed failures so callers can fail closed without inventing a
        second protocol classifier.
        """

        if not all(isinstance(value, str) and value for value in (team_id, actor, workspace_id, session_id)):
            raise ValueError("Session protocol fact scope is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            try:
                session = await _read_session_for_admission(connection, session_id)
                if session is None or session["bound_team_id"] != team_id or session["user_id"] != actor or session["workspace_id"] != workspace_id:
                    return None
                return await read_session_protocol_facts_in_tx(
                    connection,
                    session,
                )
            finally:
                if connection.in_transaction:
                    await connection.rollback()

    async def ensure_auth_scope_epochs(self, context: AuthFenceResolveContext) -> AuthFenceVector:
        """惰性建立完整 fixed-scope vector；fault 时与 ensure 一起 rollback。"""

        if not isinstance(context, AuthFenceResolveContext):
            raise TypeError("auth fence resolve context is invalid")
        result: AuthFenceVector
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now_text = _iso_utc(self._now())
                for scope in sorted(context.required_scopes, key=AuthFenceScope.sort_key):
                    await connection.execute(
                        """
                        INSERT OR IGNORE INTO terminal_auth_scope_epochs(
                          scope_kind,team_id,scope_ref,epoch,row_version,updated_at
                        ) VALUES (?,?,?,1,1,?)
                        """,
                        (scope.scope_kind.value, scope.team_id, scope.scope_ref, now_text),
                    )
                self._inject(TerminalProtocolFaultPoint.AUTH_SCOPE_AFTER_ENSURE)
                result = await _read_auth_fence_vector(connection, context.required_scopes)
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def read_auth_fence_vector(self, context: AuthFenceResolveContext) -> AuthFenceVector:
        """frame/checkpoint read path只读既有scope；缺行绝不自行补建。"""

        if not isinstance(context, AuthFenceResolveContext):
            raise TypeError("auth fence resolve context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            return await _read_auth_fence_vector(connection, context.required_scopes)

    async def read_persisted_turn_protocol_version(
        self,
        *,
        team_id: str,
        session_id: str,
        turn_id: str,
    ) -> int | None:
        """Read the persisted discriminator after the caller's ACL preflight."""

        if any(not isinstance(value, str) or not value for value in (team_id, session_id, turn_id)):
            raise ValueError("turn protocol identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT t.terminal_protocol_version,w.team_id,t.session_id
                FROM turns t
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                WHERE t.id=?
                """,
                (turn_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None or row["team_id"] != team_id or row["session_id"] != session_id:
            raise DecisionAccessRevokedError("turn protocol scope access changed")
        version = row["terminal_protocol_version"]
        if version is None:
            return None
        if version != 1:
            raise TerminalProtocolFutureVersionError("Turn uses a future terminal protocol")
        return 1

    async def read_persisted_turn_protocol_binding(
        self,
        *,
        team_id: str,
        turn_id: str,
    ) -> tuple[str, int | None]:
        """Resolve authoritative Session/version after the caller's source ACL."""

        if any(not isinstance(value, str) or not value for value in (team_id, turn_id)):
            raise ValueError("turn protocol binding identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT t.terminal_protocol_version,t.session_id,w.team_id
                FROM turns t
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                WHERE t.id=?
                """,
                (turn_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None or row["team_id"] != team_id:
            raise DecisionAccessRevokedError("turn protocol binding access changed")
        version = row["terminal_protocol_version"]
        if version is not None and version != 1:
            raise TerminalProtocolFutureVersionError("Turn uses a future terminal protocol")
        return (
            _required_text(row["session_id"], "turn protocol binding Session"),
            version,
        )

    async def read_persisted_turn_presentation_locale(
        self,
        *,
        team_id: str,
        turn_id: str,
    ) -> str:
        """Read the immutable parent locale from the authoritative Turn."""

        if any(not isinstance(value, str) or not value for value in (team_id, turn_id)):
            raise ValueError("turn locale identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT t.presentation_locale,w.team_id
                FROM turns t
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                WHERE t.id=? AND t.terminal_protocol_version=1
                """,
                (turn_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None or row["team_id"] != team_id:
            raise DecisionAccessRevokedError("turn locale scope access changed")
        return _required_text(
            row["presentation_locale"],
            "turn presentation locale",
        )

    async def read_persisted_turn_scope(
        self,
        *,
        turn_id: str,
    ) -> tuple[str, str, int | None]:
        """Internal system-control lookup; callers must already own system authority."""

        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("turn protocol scope identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT t.terminal_protocol_version,t.session_id,w.team_id
                FROM turns t
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                WHERE t.id=?
                """,
                (turn_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise DecisionSourceNotReadyError("turn protocol scope is missing")
        version = row["terminal_protocol_version"]
        if version is not None and version != 1:
            raise TerminalProtocolFutureVersionError("Turn uses a future terminal protocol")
        return (
            _required_text(row["team_id"], "turn protocol scope team"),
            _required_text(row["session_id"], "turn protocol scope Session"),
            version,
        )

    async def read_current_scope(
        self,
        *,
        team_id: str,
        turn_id: str,
    ) -> TerminalScope:
        """Resolve the current persisted v1 generation after an ACL preflight."""

        session_id, version = await self.read_persisted_turn_protocol_binding(
            team_id=team_id,
            turn_id=turn_id,
        )
        if version != 1:
            raise DecisionSourceNotReadyError("turn is not a v1 Turn")
        async with (
            open_terminal_protocol_connection(self._db_path) as connection,
            connection.execute(
                "SELECT current_generation FROM turns WHERE id=?",
                (turn_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise DecisionSourceNotReadyError("turn current generation is missing")
        return TerminalScope(
            team_id=team_id,
            session_id=session_id,
            turn_id=turn_id,
            generation=_positive_int(
                row[0],
                "turn current generation",
            ),
        )

    async def read_web_chat_action(
        self,
        *,
        action_id: str,
        action_kind: Literal["start", "steer"],
        actor: str,
        team_id: str,
        turn_id: str,
        user_message_id: str | None = None,
        session_id: str | None = None,
        execution_generation: int | None = None,
    ) -> WebChatActionLookup | None:
        """Point-read an admitted Web action without creating protocol state."""

        if any(not isinstance(value, str) or not value or len(value) > 128 for value in (action_id, actor, team_id, turn_id)):
            raise ValueError("chat action lookup identity is invalid")
        if action_kind == "start":
            if session_id is not None or execution_generation is not None or user_message_id is not None:
                raise ValueError("start action lookup contains steer-only scope")
            return await self._read_web_start_action(
                action_id=action_id,
                actor=actor,
                team_id=team_id,
                turn_id=turn_id,
            )
        if action_kind != "steer":
            raise ValueError("chat action lookup kind is invalid")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("steer action Session identity is invalid")
        if len(session_id) > 128:
            raise ValueError("steer action Session identity is invalid")
        if not isinstance(user_message_id, str) or not user_message_id or len(user_message_id) > 128:
            raise ValueError("steer action user message identity is invalid")
        if (
            not isinstance(execution_generation, int)
            or isinstance(execution_generation, bool)
            or execution_generation < 1
            or execution_generation > WEB_ACTION_MAX_EXECUTION_GENERATION
        ):
            raise ValueError("steer action generation is invalid")
        return await self._read_web_steer_action(
            action_id=action_id,
            actor=actor,
            team_id=team_id,
            session_id=session_id,
            turn_id=turn_id,
            execution_generation=execution_generation,
            user_message_id=user_message_id,
        )

    async def _read_web_start_action(
        self,
        *,
        action_id: str,
        actor: str,
        team_id: str,
        turn_id: str,
    ) -> WebChatActionLookup | None:
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA query_only=ON")
            async with connection.execute(
                """
                SELECT t.id AS turn_id,t.session_id,t.terminal_protocol_version,
                       t.terminal_ingress_surface,t.current_generation,t.user_text,
                       t.presentation_locale,t.message_catalog_version,
                       s.workspace_id,s.user_id,w.team_id,
                       g.generation AS action_generation,g.kind AS generation_kind,
                       g.protocol_version AS generation_protocol_version,
                       g.execution_intent_id,g.execution_intent_json,
                       g.execution_intent_digest,g.admission_request_digest
                FROM turns t
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                LEFT JOIN turn_generations g
                  ON g.turn_id=t.id AND g.generation=1
                WHERE t.id=? AND s.user_id=? AND w.team_id=?
                """,
                (turn_id, actor, team_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            access_scope: WebChatActionAccessScope | None = None
            try:
                access_scope = WebChatActionAccessScope(
                    team_id=_required_text(row["team_id"], "chat action Team"),
                    session_id=_required_text(row["session_id"], "chat action Session"),
                    turn_id=_required_text(row["turn_id"], "chat action Turn"),
                    generation=1,
                    workspace_id=_required_text(
                        row["workspace_id"],
                        "chat action workspace",
                    ),
                )
                _require_v1_protocol_version(
                    row["terminal_protocol_version"],
                    "chat start action Turn",
                )
                presentation_locale = _required_text(
                    row["presentation_locale"],
                    "chat start action presentation locale",
                )
                _positive_int(
                    row["message_catalog_version"],
                    "chat start action message catalog version",
                )
                if row["terminal_ingress_surface"] != "web" or row["generation_kind"] != "execution":
                    raise TerminalProtocolMalformedError(
                        "chat start action identity is malformed",
                    )
                _require_v1_protocol_version(
                    row["generation_protocol_version"],
                    "chat start action generation",
                )
                generation = _positive_int(
                    row["action_generation"],
                    "chat action generation",
                )
                current_generation = _positive_int(
                    row["current_generation"],
                    "chat action current generation",
                )
                if generation != 1 or current_generation < generation:
                    raise TerminalProtocolMalformedError(
                        "chat action generation is malformed",
                    )
                if row["execution_intent_id"] != action_id:
                    return None
                source = await _read_chat_turn_user_source_in_tx(
                    connection,
                    turn_id=access_scope.turn_id,
                )
                if source is None or (
                    source.turn_id != access_scope.turn_id
                    or source.session_id != access_scope.session_id
                    or source.user_id != actor
                    or source.workspace_id != access_scope.workspace_id
                    or source.user_text != _required_string(row["user_text"], "chat action user text")
                ):
                    raise TerminalProtocolMalformedError(
                        "chat start action user source is malformed",
                    )
                execution_intent = _parse_payload(row["execution_intent_json"])
                if not _typed_execution_context_matches(
                    execution_intent,
                    presentation_locale,
                ):
                    raise TerminalProtocolMalformedError(
                        "chat start action execution context is malformed",
                    )
                execution_source = {
                    "attachment_ids": execution_intent.get("attachments"),
                    "attachment_refs": execution_intent.get("attachment_refs"),
                    "parts": execution_intent.get("parts"),
                    "session_id": access_scope.session_id,
                    "turn_id": access_scope.turn_id,
                    "user_id": actor,
                    "user_text": execution_intent.get("text"),
                    "workspace_id": execution_intent.get("workspace_id"),
                }
                maintenance = await _read_context_maintenance_source(connection, turn_id=access_scope.turn_id)
                if maintenance is not None:
                    execution_source["context_maintenance"] = execution_intent.get("context_maintenance")
                admission_digest = canonical_admission_request_digest(
                    execution_intent=execution_intent,
                    request_id=action_id,
                    session_id=access_scope.session_id,
                    team_id=access_scope.team_id,
                    turn_id=access_scope.turn_id,
                    user_text=_required_string(
                        row["user_text"],
                        "chat action user text",
                    ),
                )
                if (
                    canonical_json_v1(execution_intent) != row["execution_intent_json"]
                    or canonical_json_v1(execution_source)
                    != canonical_json_v1(
                        chat_turn_user_source_projection(source),
                    )
                    or row["execution_intent_digest"] != admission_digest
                    or row["admission_request_digest"] != admission_digest
                ):
                    raise TerminalProtocolMalformedError(
                        "chat start action execution envelope is malformed",
                    )
                lookup = WebChatActionLookup(
                    action_id=action_id,
                    action_kind="start",
                    scope=TerminalScope(
                        team_id=access_scope.team_id,
                        session_id=access_scope.session_id,
                        turn_id=access_scope.turn_id,
                        generation=access_scope.generation,
                    ),
                    workspace_id=access_scope.workspace_id,
                    request_digest=admission_digest,
                    context_maintenance=source.context_maintenance.model_dump() if source.context_maintenance is not None else None,
                    agent_draft_target=execution_intent.get("agent_draft_target"),
                    paradigm_v2=execution_intent.get("paradigm_v2"),
                    expected_plan_revision=execution_intent.get("expected_plan_revision"),
                    language_context=execution_intent.get("language_context"),
                    viewer_time_zone=(execution_intent["viewer_time_zone"] if isinstance(execution_intent.get("viewer_time_zone"), str) else ""),
                    admitted_resolution=(
                        {key: execution_intent[key] for key in WEB_START_ADMITTED_RESOLUTION_KEYS if key in execution_intent}
                        if set(execution_intent) >= _WEB_START_LEGACY_RESOLUTION_KEYS
                        else None
                    ),
                    planning_mode=(
                        execution_intent.get("planning_mode")
                        if isinstance(
                            execution_intent.get("planning_mode"),
                            (str, bool),
                        )
                        else None
                    ),
                    plan_execution_id=(
                        execution_intent.get("plan_execution_id")
                        if isinstance(
                            execution_intent.get("plan_execution_id"),
                            str,
                        )
                        else None
                    ),
                )
            except TerminalProtocolFutureVersionError as exc:
                raise WebChatActionFutureVersionError(
                    str(exc),
                    access_scope=access_scope,
                ) from exc
            except (TerminalProtocolMalformedError, TypeError, ValueError) as exc:
                raise WebChatActionMalformedError(
                    str(exc),
                    access_scope=access_scope,
                ) from exc
        return lookup

    async def _read_web_steer_action(
        self,
        *,
        action_id: str,
        actor: str,
        team_id: str,
        session_id: str,
        turn_id: str,
        execution_generation: int,
        user_message_id: str,
    ) -> WebChatActionLookup | None:
        proposal_id, event_id = _idempotent_event_identity(
            turn_id=turn_id,
            generation=execution_generation,
            idempotency_key=f"steer_ack:{action_id}",
        )
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA query_only=ON")
            async with connection.execute(
                """
                SELECT p.*,t.session_id,t.terminal_protocol_version,
                       t.current_generation,
                       t.event_sequence_next AS turn_event_sequence_next,
                       s.workspace_id,s.user_id,w.team_id,
                       g.kind AS generation_kind,
                       g.protocol_version AS generation_protocol_version,
                       g.claim_epoch AS generation_claim_epoch,
                       g.accepted_proposal_next AS generation_accepted_proposal_next,
                       e.id AS persisted_id,e.turn_id AS persisted_turn_id,
                       e.generation AS persisted_generation,e.seq AS persisted_legacy_seq,
                       e.event_seq AS persisted_event_seq_row,
                       e.event_type AS persisted_event_type,
                       e.actor_scope AS persisted_actor_scope,
                       e.phase_scope AS persisted_phase_scope,
                       e.payload_json AS persisted_payload_json,
                       e.payload_digest AS persisted_payload_digest,
                       e.source_proposal_id AS persisted_source_proposal_id,
                       e.accept_ordinal AS persisted_accept_ordinal,
                       e.occurred_at AS persisted_occurred_at,
                       e.protocol_version AS persisted_protocol_version,
                       e.terminal_receipt_id AS persisted_terminal_receipt_id
                FROM turn_event_proposals p
                JOIN turns t ON t.id=p.turn_id
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                JOIN turn_generations g
                  ON g.turn_id=p.turn_id AND g.generation=p.generation
                LEFT JOIN turn_events e ON e.id=p.event_id
                WHERE p.proposal_id=? AND p.turn_id=? AND p.generation=?
                  AND t.session_id=? AND s.user_id=? AND w.team_id=?
                """,
                (
                    proposal_id,
                    turn_id,
                    execution_generation,
                    session_id,
                    actor,
                    team_id,
                ),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        access_scope: WebChatActionAccessScope | None = None
        try:
            access_scope = WebChatActionAccessScope(
                team_id=_required_text(row["team_id"], "chat action Team"),
                session_id=_required_text(row["session_id"], "chat action Session"),
                turn_id=turn_id,
                generation=execution_generation,
                workspace_id=_required_text(
                    row["workspace_id"],
                    "chat action workspace",
                ),
            )
            expected_payload = {
                "emoji": "🤝",
                "steer_id": action_id,
                "user_msg_id": user_message_id,
            }
            expected_payload_json = canonical_json_v1(expected_payload)
            expected_payload_digest = canonical_digest_v1(expected_payload)
            _require_v1_protocol_version(
                row["terminal_protocol_version"],
                "chat steer action Turn",
            )
            _require_v1_protocol_version(
                row["generation_protocol_version"],
                "chat steer action generation",
            )
            if row["persisted_protocol_version"] is not None:
                _require_v1_protocol_version(
                    row["persisted_protocol_version"],
                    "chat steer acknowledgement event",
                )
            if (
                row["proposal_id"] != proposal_id
                or row["event_id"] != event_id
                or row["generation_kind"] != "execution"
                or row["event_type"] != "emoji_ack"
                or row["actor_scope"] != ActorScope.ROOT.value
                or row["phase_scope"] != PhaseScope.IN_GENERATION.value
                or row["payload_json"] != expected_payload_json
                or row["payload_digest"] != expected_payload_digest
                or row["durability"] != RootDurability.COMMITTED.value
            ):
                raise TerminalProtocolMalformedError(
                    "chat steer acknowledgement identity is malformed",
                )
            accepted_claim_epoch = _positive_int(
                row["accepted_claim_epoch"],
                "chat steer acknowledgement claim epoch",
            )
            generation_claim_epoch = _positive_int(
                row["generation_claim_epoch"],
                "chat steer generation claim epoch",
            )
            if accepted_claim_epoch > generation_claim_epoch:
                raise TerminalProtocolMalformedError(
                    "chat steer acknowledgement claim moved ahead of its generation",
                )
            accept_ordinal = _positive_int(
                row["accept_ordinal"],
                "chat steer acknowledgement ordinal",
            )
            accepted_proposal_next = _positive_int(
                row["generation_accepted_proposal_next"],
                "chat steer generation accepted proposal allocator",
            )
            if accept_ordinal >= accepted_proposal_next:
                raise TerminalProtocolMalformedError(
                    "chat steer acknowledgement proposal allocator was not committed",
                )
            _required_text(
                row["accepted_at"],
                "chat steer acknowledgement accepted_at",
            )
            state = row["state"]
            if state == "accepted":
                if row["persisted_id"] is not None:
                    raise TerminalProtocolMalformedError(
                        "accepted chat steer acknowledgement already has an event",
                    )
            elif state == "persisted":
                if (
                    row["persisted_protocol_version"] != 1
                    or row["persisted_terminal_receipt_id"] is not None
                    or row["persisted_occurred_at"] != row["accepted_at"]
                ):
                    raise TerminalProtocolMalformedError(
                        "persisted chat steer acknowledgement envelope is malformed",
                    )
                _required_text(
                    row["persisted_at"],
                    "chat steer acknowledgement persisted_at",
                )
                _validate_persisted_proposal_projection(row)
                persisted_event_seq = _positive_int(
                    row["persisted_event_seq_row"],
                    "chat steer acknowledgement event sequence",
                )
                event_sequence_next = _positive_int(
                    row["turn_event_sequence_next"],
                    "chat steer Turn event allocator",
                )
                if persisted_event_seq >= event_sequence_next:
                    raise TerminalProtocolMalformedError(
                        "chat steer acknowledgement event allocator was not committed",
                    )
            else:
                raise TerminalProtocolMalformedError(
                    "chat steer acknowledgement state is malformed",
                )
            current_generation = _positive_int(
                row["current_generation"],
                "chat action current generation",
            )
            if current_generation < execution_generation:
                raise TerminalProtocolMalformedError(
                    "chat action current generation moved backwards",
                )
            lookup = WebChatActionLookup(
                action_id=action_id,
                action_kind="steer",
                scope=TerminalScope(
                    team_id=access_scope.team_id,
                    session_id=access_scope.session_id,
                    turn_id=access_scope.turn_id,
                    generation=access_scope.generation,
                ),
                workspace_id=access_scope.workspace_id,
            )
        except TerminalProtocolFutureVersionError as exc:
            raise WebChatActionFutureVersionError(
                str(exc),
                access_scope=access_scope,
            ) from exc
        except (TerminalProtocolMalformedError, TypeError, ValueError) as exc:
            raise WebChatActionMalformedError(
                str(exc),
                access_scope=access_scope,
            ) from exc
        return lookup

    async def read_decision_preflight(
        self,
        *,
        team_id: str,
        session_id: str,
        turn_id: str,
        source_kind: DecisionSourceKind,
        source_id: str,
        action: DecisionAction,
        principals: DecisionPrincipalSet,
    ) -> DecisionPreflightSnapshot:
        """Read the authoritative pause tuple after the surface ACL preflight.

        Callers must ensure the required auth-scope rows before this read.  This
        method never creates epochs and therefore cannot turn a read into an
        authorization mutation.
        """

        if any(not isinstance(value, str) or not value for value in (team_id, session_id, turn_id, source_id)):
            raise ValueError("decision preflight identity is invalid")
        if not isinstance(source_kind, DecisionSourceKind) or not isinstance(
            action,
            DecisionAction,
        ):
            raise TypeError("decision preflight source enum is invalid")
        if not isinstance(principals, DecisionPrincipalSet):
            raise TypeError("decision preflight principals are invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT t.current_generation,t.generation_transition_epoch,
                       t.current_terminal_receipt_id,t.terminal_protocol_version,
                       g.state AS generation_state,g.terminal_receipt_id AS generation_receipt_id,
                       r.status AS receipt_status,
                  r.operation_kind AS receipt_operation_kind,
                  t.operation_kind AS turn_operation_kind,w.team_id,t.session_id
                FROM turns t
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                JOIN turn_generations g
                  ON g.turn_id=t.id AND g.generation=t.current_generation
                LEFT JOIN turn_terminal_receipts r
                  ON r.receipt_id=t.current_terminal_receipt_id
                WHERE t.id=?
                """,
                (turn_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row["team_id"] != team_id or row["session_id"] != session_id:
                raise DecisionAccessRevokedError("decision preflight scope access changed")
            version = row["terminal_protocol_version"]
            if version is None:
                raise DecisionSourceNotReadyError("decision source belongs to a legacy Turn")
            if version != 1:
                raise TerminalProtocolFutureVersionError("decision source uses a future protocol")
            expected_status = {
                DecisionSourceKind.CLARIFICATION: TerminalStatus.CLARIFICATION_PENDING.value,
                DecisionSourceKind.APPROVAL: TerminalStatus.APPROVAL_PENDING.value,
            }.get(source_kind)
            if row["generation_state"] != GenerationState.CLOSED.value:
                raise DecisionConflictError("decision preflight generation is not paused")
            prior_receipt_id = _required_text(
                row["current_terminal_receipt_id"],
                "decision.preflight.prior_receipt_id",
            )
            if row["generation_receipt_id"] != prior_receipt_id or (expected_status is not None and row["receipt_status"] != expected_status):
                raise DecisionConflictError("decision preflight pause truth changed")
            scope = TerminalScope(
                team_id=team_id,
                session_id=session_id,
                turn_id=turn_id,
                generation=_positive_int(
                    row["current_generation"],
                    "decision.preflight.generation",
                ),
            )
            effective_source_id = (
                f"{turn_id}:{scope.generation}:control"
                if source_kind is DecisionSourceKind.SYSTEM_CONTROL and source_id == "__auto_control__"
                else source_id
            )
            auth_context = DecisionContext(
                scope=scope,
                decision_id="decision_preflight",
                source_kind=source_kind,
                source_id=effective_source_id,
                action=action,
                principals=principals,
                authz_snapshot_digest="decision_preflight",
                prior_receipt_id=prior_receipt_id,
            )
            vector = await _read_auth_fence_vector(
                connection,
                _decision_auth_fence_scopes(auth_context),
            )
        return DecisionPreflightSnapshot(
            scope=scope,
            generation_transition_epoch=_positive_int(
                row["generation_transition_epoch"],
                "decision.preflight.generation_transition_epoch",
            ),
            prior_receipt_id=prior_receipt_id,
            authz_snapshot_digest=vector.auth_fence_vector_digest,
        )

    async def read_source_decision(
        self,
        *,
        team_id: str,
        session_id: str,
        turn_id: str,
        source_kind: DecisionSourceKind,
        source_id: str,
        action: DecisionAction,
        principals: DecisionPrincipalSet,
    ) -> tuple[RecordedDecision, DecisionPreflightSnapshot] | None:
        """Rehydrate a durable decision after its target generation was opened."""

        if any(not isinstance(value, str) or not value for value in (team_id, session_id, turn_id, source_id)):
            raise ValueError("source decision identity is invalid")
        if not isinstance(source_kind, DecisionSourceKind) or not isinstance(
            action,
            DecisionAction,
        ):
            raise TypeError("source decision enum is invalid")
        if not isinstance(principals, DecisionPrincipalSet):
            raise TypeError("source decision principals are invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            if source_kind is DecisionSourceKind.SYSTEM_CONTROL and source_id == "__auto_control__":
                query = """
                    SELECT d.*,t.session_id,t.generation_transition_epoch,
                           t.terminal_protocol_version,w.team_id,
                           s.workspace_id AS session_workspace_id
                    FROM turn_control_decisions d
                    JOIN turns t ON t.id=d.turn_id
                    JOIN sessions s ON s.id=t.session_id
                    JOIN workspaces w ON w.id=s.workspace_id
                    WHERE d.source_kind=? AND d.turn_id=? AND d.action=?
                    ORDER BY d.prior_generation DESC LIMIT 1
                """
                parameters = (source_kind.value, turn_id, action.value)
            else:
                query = """
                    SELECT d.*,t.session_id,t.generation_transition_epoch,
                           t.terminal_protocol_version,w.team_id,
                           s.workspace_id AS session_workspace_id
                    FROM turn_control_decisions d
                    JOIN turns t ON t.id=d.turn_id
                    JOIN sessions s ON s.id=t.session_id
                    JOIN workspaces w ON w.id=s.workspace_id
                    WHERE d.source_kind=? AND d.source_id=?
                """
                parameters = (source_kind.value, source_id)
            async with connection.execute(query, parameters) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            if row["team_id"] != team_id or row["session_id"] != session_id or row["turn_id"] != turn_id:
                raise DecisionAccessRevokedError("source decision scope changed")
            if row["terminal_protocol_version"] != 1:
                raise TerminalProtocolFutureVersionError("source decision protocol changed")
            scope = TerminalScope(
                team_id=team_id,
                session_id=session_id,
                turn_id=turn_id,
                generation=_positive_int(
                    row["prior_generation"],
                    "source decision prior generation",
                ),
            )
            persisted_source_id = _required_text(row["source_id"], "source decision source id")
            context = DecisionContext(
                scope=scope,
                decision_id=stable_decision_id(
                    scope=scope,
                    source_kind=source_kind,
                    source_id=persisted_source_id,
                    action=action,
                ),
                source_kind=source_kind,
                source_id=persisted_source_id,
                action=action,
                principals=principals,
                authz_snapshot_digest=_required_text(
                    row["authz_snapshot_digest"],
                    "source decision auth digest",
                ),
                prior_receipt_id=_required_text(
                    row["prior_receipt_id"],
                    "source decision prior receipt",
                ),
            )
            try:
                state = DecisionState(row["state"])
            except ValueError as exc:
                raise TerminalProtocolMalformedError("source decision state is malformed") from exc
            target = row["target_generation"]
            decision = RecordedDecision(
                context=context,
                state=state,
                decision_digest=_required_text(
                    row["decision_digest"],
                    "source decision digest",
                ),
                target_generation=(_positive_int(target, "source decision target generation") if target is not None else None),
                replayed=True,
            )
            _validate_stored_decision(row, decision)
            source = await _read_decision_source(connection, context)
            _validate_rehydrated_decision_source_projection(
                source=source,
                stored=row,
                context=context,
                session_workspace_id=_required_text(
                    row["session_workspace_id"],
                    "source decision workspace",
                ),
            )
            # The persisted digest is immutable decision-time audit evidence,
            # not a permanent authorization verdict. Rehydration returns the
            # current fence below; the live/recovery authorizer decides whether
            # that change is still permitted before opening N+1.
            vector = await _read_auth_fence_vector(
                connection,
                _decision_auth_fence_scopes(context),
            )
        return (
            decision,
            DecisionPreflightSnapshot(
                scope=scope,
                generation_transition_epoch=_positive_int(
                    row["prior_generation_transition_epoch"],
                    "source decision transition epoch",
                ),
                prior_receipt_id=context.prior_receipt_id,
                authz_snapshot_digest=vector.auth_fence_vector_digest,
            ),
        )

    async def list_pending_decisions(
        self,
        *,
        limit: int = 100,
    ) -> tuple[PendingDecisionRecovery, ...]:
        """Return durable source winners that have not opened a target generation."""

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("pending decision limit must be between 1 and 100")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT d.*,t.session_id,t.generation_transition_epoch,t.current_terminal_receipt_id,
                       w.team_id
                FROM turn_control_decisions d
                JOIN turns t ON t.id=d.turn_id
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                WHERE d.state='decision_recorded' AND d.target_generation IS NULL
                  AND (d.recovery_next_attempt_at IS NULL OR d.recovery_next_attempt_at<=?)
                ORDER BY d.recorded_at,d.decision_id LIMIT ?
                """,
                (_iso_utc(self._now()), limit),
            ) as cursor:
                rows = await cursor.fetchall()
        result: list[PendingDecisionRecovery] = []
        malformed: list[str] = []
        for row in rows:
            try:
                source_projection = _parse_payload(row["source_projection_json"])
                if (
                    canonical_json_v1(source_projection) != row["source_projection_json"]
                    or source_projection.get("source_projection_digest") != row["source_projection_digest"]
                ):
                    raise TerminalProtocolMalformedError("pending decision source projection is malformed")
                source_kind = DecisionSourceKind(row["source_kind"])
                action = DecisionAction(row["action"])
                scope = TerminalScope(
                    team_id=_required_text(
                        row["team_id"],
                        "decision.team_id",
                    ),
                    session_id=_required_text(
                        row["session_id"],
                        "decision.session_id",
                    ),
                    turn_id=_required_text(
                        row["turn_id"],
                        "decision.turn_id",
                    ),
                    generation=_positive_int(
                        row["prior_generation"],
                        "decision.prior_generation",
                    ),
                )
                context = DecisionContext(
                    scope=scope,
                    decision_id=_required_text(
                        row["decision_id"],
                        "decision.decision_id",
                    ),
                    source_kind=source_kind,
                    source_id=_required_text(
                        row["source_id"],
                        "decision.source_id",
                    ),
                    action=action,
                    principals=DecisionPrincipalSet(
                        requester_ref=_required_text(
                            row["requester_ref"],
                            "decision.requester_ref",
                        ),
                        actor_ref=_required_text(
                            row["actor_ref"],
                            "decision.actor_ref",
                        ),
                        effect_principal_ref=_required_text(
                            row["effect_principal_ref"],
                            "decision.effect_principal_ref",
                        ),
                    ),
                    authz_snapshot_digest=_required_text(
                        row["authz_snapshot_digest"],
                        "decision.authz_snapshot_digest",
                    ),
                    prior_receipt_id=_required_text(
                        row["prior_receipt_id"],
                        "decision.prior_receipt_id",
                    ),
                )
                result.append(
                    PendingDecisionRecovery(
                        decision=RecordedDecision(
                            context=context,
                            state=DecisionState.DECISION_RECORDED,
                            decision_digest=_required_text(
                                row["decision_digest"],
                                "decision.decision_digest",
                            ),
                            target_generation=None,
                            replayed=True,
                        ),
                        generation_transition_epoch=_positive_int(
                            row["prior_generation_transition_epoch"],
                            "turn.generation_transition_epoch",
                        ),
                        source_projection_digest=_required_text(
                            row["source_projection_digest"],
                            "decision.source_projection_digest",
                        ),
                    )
                )
            except BaseException:
                decision_id = row["decision_id"]
                if isinstance(decision_id, str) and decision_id:
                    malformed.append(decision_id)
        for decision_id in malformed:
            await self.defer_pending_decision(
                decision_id=decision_id,
                code="terminal_recovery_decision_malformed",
            )
        return tuple(result)

    async def supersede_pending_decision_with_revoke(
        self,
        decision: RecordedDecision,
        *,
        denial_reason_code: str = "access_revoked",
    ) -> tuple[RecordedDecision, DecisionPreflightSnapshot]:
        """Atomically replace a recorded-but-unopened action after auth revoke."""

        if not isinstance(decision, RecordedDecision) or decision.state is not DecisionState.DECISION_RECORDED:
            raise TypeError("revocation requires a recorded decision")
        old_context = decision.context
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                old = await _read_stored_decision(
                    connection,
                    old_context.decision_id,
                )
                _validate_stored_decision(old, decision)
                if denial_reason_code not in {
                    "access_revoked",
                    "dependency_missing",
                    "entitlement_denied",
                }:
                    raise ValueError("decision denial reason is invalid")
                source_id = f"{old_context.scope.turn_id}:{old_context.scope.generation}:control"
                principals = DecisionPrincipalSet(
                    old_context.principals.requester_ref,
                    "system:authorization-revocation",
                    old_context.principals.requester_ref,
                )
                draft_context = DecisionContext(
                    scope=old_context.scope,
                    decision_id=stable_decision_id(
                        scope=old_context.scope,
                        source_kind=DecisionSourceKind.SYSTEM_CONTROL,
                        source_id=source_id,
                        action=DecisionAction.REVOKE,
                    ),
                    source_kind=DecisionSourceKind.SYSTEM_CONTROL,
                    source_id=source_id,
                    action=DecisionAction.REVOKE,
                    principals=principals,
                    authz_snapshot_digest="decision_revoke_preflight",
                    prior_receipt_id=old_context.prior_receipt_id,
                )
                vector = await _read_auth_fence_vector(
                    connection,
                    _decision_auth_fence_scopes(draft_context),
                )
                context = replace(
                    draft_context,
                    authz_snapshot_digest=(vector.auth_fence_vector_digest),
                )
                request = DecisionRecordRequest(
                    context=context,
                    decision_payload_digest=(
                        canonical_decision_payload_digest_for(
                            source_kind=DecisionSourceKind.SYSTEM_CONTROL,
                            action=DecisionAction.REVOKE,
                            expected_source_state="pending",
                            target_source_state=None,
                            source_values_json=canonical_json_v1({"denial_reason_code": (denial_reason_code)}),
                        )
                    ),
                    expected_source_state="pending",
                    target_source_state=None,
                    source_values_json=canonical_json_v1({"denial_reason_code": denial_reason_code}),
                )
                digest = decision_identity_digest(request)
                replay = await _read_decision_replay(
                    connection,
                    request=request,
                    decision_digest=digest,
                )
                if replay is None:
                    if old is None or old["state"] != DecisionState.DECISION_RECORDED.value or old["target_generation"] is not None:
                        raise DecisionConflictError("pending decision revocation lost its race")
                    now_text = _iso_utc(self._now())
                    superseded = await connection.execute(
                        """
                        UPDATE turn_control_decisions
                        SET state='terminal_control_committed',consumed_at=?
                        WHERE decision_id=? AND state='decision_recorded'
                          AND target_generation IS NULL
                        """,
                        (now_text, old_context.decision_id),
                    )
                    if superseded.rowcount != 1:
                        raise DecisionConflictError("pending decision revocation lost its CAS")
                    await _skip_available_decision_effects(
                        connection,
                        decision_id=old_context.decision_id,
                        code=denial_reason_code,
                        now_text=now_text,
                    )
                    await connection.execute(
                        """
                        INSERT INTO turn_control_decisions(
                          decision_id,source_kind,source_id,turn_id,
                          prior_generation,action,decision_digest,state,
                          target_generation,actor_ref,requester_ref,
                          effect_principal_ref,source_projection_json,
                          source_projection_digest,prior_receipt_id,
                          prior_generation_transition_epoch,
                          authz_snapshot_digest,created_at,recorded_at,
                          consumed_at,control_outcome_code
                        ) VALUES (?,?,?,?,?,?,?,'decision_recorded',NULL,
                                  ?,?,?,?,?,?,?,?,?,?,NULL,?)
                        """,
                        (
                            context.decision_id,
                            context.source_kind.value,
                            context.source_id,
                            context.scope.turn_id,
                            context.scope.generation,
                            context.action.value,
                            digest,
                            context.actor_ref,
                            context.principals.requester_ref,
                            context.principals.effect_principal_ref,
                            canonical_json_v1(canonical_decision_source_audit_projection(request)),
                            canonical_decision_payload_digest(request),
                            context.prior_receipt_id,
                            _positive_int(
                                old["prior_generation_transition_epoch"],
                                "decision prior transition epoch",
                            ),
                            context.authz_snapshot_digest,
                            now_text,
                            now_text,
                            denial_reason_code,
                        ),
                    )
                    replay = RecordedDecision(
                        context=context,
                        state=DecisionState.DECISION_RECORDED,
                        decision_digest=digest,
                        target_generation=None,
                        replayed=False,
                    )
                    await connection.commit()
                else:
                    # A live effect claim is not stolen by revocation.  A
                    # later replay is nevertheless responsible for settling
                    # that claim once its lease expires, so superseded work
                    # cannot remain in recovery inventory forever.
                    changed = await _skip_available_decision_effects(
                        connection,
                        decision_id=old_context.decision_id,
                        code=denial_reason_code,
                        now_text=_iso_utc(self._now()),
                    )
                    if changed:
                        await connection.commit()
                    else:
                        await connection.rollback()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return (
            replay,
            DecisionPreflightSnapshot(
                scope=context.scope,
                generation_transition_epoch=_positive_int(
                    old["prior_generation_transition_epoch"],
                    "decision prior transition epoch",
                ),
                prior_receipt_id=context.prior_receipt_id,
                authz_snapshot_digest=context.authz_snapshot_digest,
            ),
        )

    async def rehydrate_decision_effect_source(
        self,
        *,
        decision_id: str,
    ) -> tuple[RecordedDecision, DecisionPreflightSnapshot]:
        """Revalidate an effect's immutable winner against its live source row.

        Decision-effect workers are internal and begin with only a decision ID.
        Resolve the persisted principals/scope, then deliberately pass through
        ``read_source_decision`` so source projection and current auth-vector
        validation cannot drift from pending-generation recovery.
        """

        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("decision effect source identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT d.decision_id,d.source_kind,d.source_id,d.turn_id,d.action,
                       d.requester_ref,d.actor_ref,d.effect_principal_ref,
                       d.state,d.target_generation,
                       t.session_id,t.terminal_protocol_version,w.team_id
                FROM turn_control_decisions d
                JOIN turns t ON t.id=d.turn_id
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                WHERE d.decision_id=?
                """,
                (decision_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise DecisionSourceNotReadyError("decision effect winner is missing")
        if row["terminal_protocol_version"] != 1:
            raise TerminalProtocolFutureVersionError("decision effect source protocol changed")
        if row["state"] == DecisionState.TERMINAL_CONTROL_COMMITTED.value and row["target_generation"] is None:
            raise DecisionAuthorizationDeniedError("access_revoked")
        try:
            source_kind = DecisionSourceKind(row["source_kind"])
            action = DecisionAction(row["action"])
        except ValueError as exc:
            raise TerminalProtocolMalformedError("decision effect source enum is malformed") from exc
        result = await self.read_source_decision(
            team_id=_required_text(row["team_id"], "decision effect team"),
            session_id=_required_text(
                row["session_id"],
                "decision effect Session",
            ),
            turn_id=_required_text(row["turn_id"], "decision effect Turn"),
            source_kind=source_kind,
            source_id=_required_text(row["source_id"], "decision effect source"),
            action=action,
            principals=DecisionPrincipalSet(
                requester_ref=_required_text(
                    row["requester_ref"],
                    "decision effect requester",
                ),
                actor_ref=_required_text(row["actor_ref"], "decision effect actor"),
                effect_principal_ref=_required_text(
                    row["effect_principal_ref"],
                    "decision effect principal",
                ),
            ),
        )
        if result is None or result[0].context.decision_id != decision_id:
            raise TerminalProtocolMalformedError("decision effect winner identity changed")
        return result

    async def defer_pending_decision(
        self,
        *,
        decision_id: str,
        code: str,
        delay_seconds: int = 30,
    ) -> bool:
        """Persist a per-decision retry point so one bad row cannot starve FIFO."""

        if any(not isinstance(value, str) or not value for value in (decision_id, code)):
            raise ValueError("pending decision defer identity is invalid")
        if not isinstance(delay_seconds, int) or isinstance(delay_seconds, bool) or delay_seconds < 1:
            raise ValueError("pending decision defer delay is invalid")
        next_attempt = _iso_utc(self._now() + timedelta(seconds=delay_seconds))
        async with open_terminal_protocol_connection(self._db_path) as connection:
            update = await connection.execute(
                """
                UPDATE turn_control_decisions
                SET recovery_attempt_count=recovery_attempt_count+1,
                    recovery_next_attempt_at=?,recovery_last_code=?
                WHERE decision_id=? AND state='decision_recorded' AND target_generation IS NULL
                """,
                (next_attempt, code[:128], decision_id),
            )
            await connection.commit()
        return update.rowcount == 1

    async def claim_decision_effects(
        self,
        *,
        decision_id: str,
        owner: str,
        expected_authz_snapshot_digest: str,
        lease_seconds: int = 30,
        limit: int = 25,
    ) -> tuple[ClaimedDecisionSideEffect, ...]:
        """Claim pending post-commit projections with a durable, expiring fence."""

        if any(
            not isinstance(value, str) or not value
            for value in (
                decision_id,
                owner,
                expected_authz_snapshot_digest,
            )
        ):
            raise ValueError("decision effect claim identity is invalid")
        if not 1 <= lease_seconds <= 300 or not 1 <= limit <= 100:
            raise ValueError("decision effect claim bounds are invalid")
        now = self._now()
        now_text = _iso_utc(now)
        expires = _iso_utc(now + timedelta(seconds=lease_seconds))
        claimed: list[ClaimedDecisionSideEffect] = []
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                decision_context = await _read_decision_auth_context(
                    connection,
                    decision_id,
                )
                current_auth = await _read_auth_fence_vector(
                    connection,
                    _decision_auth_fence_scopes(decision_context),
                )
                if current_auth.auth_fence_vector_digest != expected_authz_snapshot_digest:
                    raise DecisionAccessRevokedError("decision effect authorization changed")
                async with connection.execute(
                    """
                    SELECT * FROM turn_decision_effects
                    WHERE decision_id=? AND state NOT IN ('applied','skipped')
                      AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                      AND (state='pending' OR claim_expires_at IS NULL OR claim_expires_at<=?)
                    ORDER BY created_at,effect_id LIMIT ?
                    """,
                    (decision_id, now_text, now_text, limit),
                ) as cursor:
                    rows = await cursor.fetchall()
                for row in rows:
                    next_epoch = int(row["claim_epoch"]) + 1
                    update = await connection.execute(
                        """
                        UPDATE turn_decision_effects
                        SET state='claimed',claim_owner=?,claim_epoch=?,claim_expires_at=?,
                            attempt_count=attempt_count+1,last_code=NULL,next_attempt_at=NULL
                        WHERE effect_id=? AND claim_epoch=?
                          AND state NOT IN ('applied','skipped')
                          AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                          AND (state='pending' OR claim_expires_at IS NULL OR claim_expires_at<=?)
                        """,
                        (
                            owner,
                            next_epoch,
                            expires,
                            row["effect_id"],
                            row["claim_epoch"],
                            now_text,
                            now_text,
                        ),
                    )
                    if update.rowcount != 1:
                        continue
                    claimed.append(
                        ClaimedDecisionSideEffect(
                            plan=DecisionSideEffectPlan(
                                effect_id=_required_text(row["effect_id"], "decision_effect.effect_id"),
                                effect_kind=_required_text(row["effect_kind"], "decision_effect.effect_kind"),
                                target_ref=_required_text(row["target_ref"], "decision_effect.target_ref"),
                                payload_json=_required_text(row["payload_json"], "decision_effect.payload_json"),
                                payload_digest=_required_text(row["payload_digest"], "decision_effect.payload_digest"),
                            ),
                            decision_id=decision_id,
                            claim_owner=owner,
                            claim_epoch=next_epoch,
                        )
                    )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return tuple(claimed)

    async def settle_decision_effect(
        self,
        claimed: ClaimedDecisionSideEffect,
        *,
        applied: bool,
        code: str | None = None,
        retry_delay_seconds: int = 30,
    ) -> bool:
        """Settle or release a claimed projection without changing its identity."""

        if not isinstance(claimed, ClaimedDecisionSideEffect):
            raise TypeError("claimed decision effect is invalid")
        if not isinstance(applied, bool) or (not applied and (not code or not isinstance(code, str))):
            raise ValueError("decision effect settlement is invalid")
        if not isinstance(retry_delay_seconds, int) or isinstance(retry_delay_seconds, bool) or not 1 <= retry_delay_seconds <= 3600:
            raise ValueError("decision effect retry delay is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            if applied:
                update = await connection.execute(
                    """
                    UPDATE turn_decision_effects
                    SET state='applied',claim_owner=NULL,claim_expires_at=NULL,
                        next_attempt_at=NULL,applied_at=?,last_code=NULL,
                        resolution_code=NULL
                    WHERE effect_id=? AND decision_id=? AND state='claimed'
                      AND claim_owner=? AND claim_epoch=?
                    """,
                    (
                        _iso_utc(self._now()),
                        claimed.plan.effect_id,
                        claimed.decision_id,
                        claimed.claim_owner,
                        claimed.claim_epoch,
                    ),
                )
            else:
                update = await connection.execute(
                    """
                    UPDATE turn_decision_effects
                    SET state='pending',claim_owner=NULL,claim_expires_at=NULL,
                        next_attempt_at=?,last_code=?
                    WHERE effect_id=? AND decision_id=? AND state='claimed'
                      AND claim_owner=? AND claim_epoch=?
                    """,
                    (
                        _iso_utc(self._now() + timedelta(seconds=retry_delay_seconds)),
                        code[:128],
                        claimed.plan.effect_id,
                        claimed.decision_id,
                        claimed.claim_owner,
                        claimed.claim_epoch,
                    ),
                )
            await connection.commit()
        return update.rowcount == 1

    async def skip_decision_effects(
        self,
        *,
        decision_id: str,
        code: str,
    ) -> int:
        """Permanently settle projections that current authorization forbids."""

        if any(not isinstance(value, str) or not value for value in (decision_id, code)):
            raise ValueError("decision effect skip identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            update = await connection.execute(
                """
                UPDATE turn_decision_effects
                SET state='skipped',claim_owner=NULL,claim_expires_at=NULL,
                    next_attempt_at=NULL,last_code=NULL,resolution_code=?,
                    applied_at=?
                WHERE decision_id=? AND (
                  state='pending' OR (
                    state='claimed' AND (
                      claim_expires_at IS NULL OR claim_expires_at<=?
                    )
                  )
                )
                """,
                (
                    code[:128],
                    _iso_utc(self._now()),
                    decision_id,
                    _iso_utc(self._now()),
                ),
            )
            await connection.commit()
        return update.rowcount

    async def list_pending_decision_effect_ids(
        self,
        *,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """List due decision-effect groups without exposing their payloads."""

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("decision effect list limit is invalid")
        now_text = _iso_utc(self._now())
        async with (
            open_terminal_protocol_connection(self._db_path) as connection,
            connection.execute(
                """
                SELECT decision_id,MIN(created_at) AS first_created
                FROM turn_decision_effects
                WHERE state NOT IN ('applied','skipped')
                  AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                  AND (state='pending' OR claim_expires_at IS NULL
                       OR claim_expires_at<=?)
                GROUP BY decision_id
                ORDER BY first_created,decision_id LIMIT ?
                """,
                (now_text, now_text, limit),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return tuple(_required_text(row[0], "decision_effect.decision_id") for row in rows)

    async def defer_decision_effect_group(
        self,
        *,
        decision_id: str,
        code: str,
        delay_seconds: int = 30,
    ) -> int:
        """Back off a due effect group even when source rehydrate failed."""

        if any(not isinstance(value, str) or not value for value in (decision_id, code)):
            raise ValueError("decision effect defer identity is invalid")
        if not 1 <= delay_seconds <= 300:
            raise ValueError("decision effect defer delay is invalid")
        now_text = _iso_utc(self._now())
        next_attempt = _iso_utc(self._now() + timedelta(seconds=delay_seconds))
        async with open_terminal_protocol_connection(self._db_path) as connection:
            update = await connection.execute(
                """
                UPDATE turn_decision_effects
                SET state='pending',claim_owner=NULL,claim_expires_at=NULL,
                    next_attempt_at=?,last_code=?
                WHERE decision_id=? AND state NOT IN ('applied','skipped')
                  AND (state='pending' OR claim_expires_at IS NULL
                       OR claim_expires_at<=?)
                """,
                (
                    next_attempt,
                    code[:128],
                    decision_id,
                    now_text,
                ),
            )
            await connection.commit()
        return update.rowcount

    async def read_current_decision_auth_digest(
        self,
        decision: RecordedDecision,
    ) -> str:
        if not isinstance(decision, RecordedDecision):
            raise TypeError("decision auth recovery input is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            stored = await _read_stored_decision(connection, decision.context.decision_id)
            _validate_stored_decision(stored, decision)
            vector = await _read_auth_fence_vector(
                connection,
                _decision_auth_fence_scopes(decision.context),
            )
        return vector.auth_fence_vector_digest

    async def acquire_recovery_writer_fence(
        self,
        *,
        owner_instance_id: str,
        rollout_config_generation: str,
    ) -> RecoveryWriterFence | None:
        """Acquire an expired/empty writer row in draining, recovery-only mode."""

        if any(not isinstance(value, str) or not value for value in (owner_instance_id, rollout_config_generation)):
            raise ValueError("recovery writer identity is invalid")
        result: RecoveryWriterFence | None
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                now_text = _iso_utc(now)
                expiry = _iso_utc(now + timedelta(seconds=_RECOVERY_LEASE_SECONDS))
                async with connection.execute(
                    "SELECT * FROM terminal_protocol_writer_lease WHERE lease_name=?",
                    (_WRITER_LEASE_NAME,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await connection.execute(
                        """
                        INSERT INTO terminal_protocol_writer_lease(
                          lease_name,owner_instance_id,epoch,admission_state,admission_epoch,
                          rollout_config_generation,deployment_attestation_id,
                          lease_expires_at,heartbeat_at
                        ) VALUES (?, ?,1,'draining',1,?,NULL,?,?)
                        """,
                        (
                            _WRITER_LEASE_NAME,
                            owner_instance_id,
                            rollout_config_generation,
                            expiry,
                            now_text,
                        ),
                    )
                    result = RecoveryWriterFence(
                        owner_instance_id=owner_instance_id,
                        writer_lease_epoch=1,
                        writer_admission_epoch=1,
                        rollout_config_generation=rollout_config_generation,
                        lease_expires_at=expiry,
                    )
                elif (
                    row["owner_instance_id"] == owner_instance_id
                    and row["admission_state"] == "draining"
                    and row["rollout_config_generation"] == rollout_config_generation
                    and _timestamp_after(row["lease_expires_at"], now)
                ):
                    result = _recovery_writer_fence_from_row(row)
                    await connection.rollback()
                    return result
                elif row["owner_instance_id"] is not None and _timestamp_after(
                    row["lease_expires_at"],
                    now,
                ):
                    await connection.rollback()
                    return None
                else:
                    next_epoch = _nonnegative_int(row["epoch"], "writer.epoch") + 1
                    next_admission_epoch = _nonnegative_int(row["admission_epoch"], "writer.admission_epoch") + 1
                    update = await connection.execute(
                        """
                        UPDATE terminal_protocol_writer_lease
                        SET owner_instance_id=?,epoch=?,admission_state='draining',
                            admission_epoch=?,rollout_config_generation=?,
                            deployment_attestation_id=NULL,lease_expires_at=?,heartbeat_at=?
                        WHERE lease_name=? AND epoch=? AND admission_epoch=?
                          AND (owner_instance_id IS NULL OR lease_expires_at IS NULL OR lease_expires_at<=?)
                        """,
                        (
                            owner_instance_id,
                            next_epoch,
                            next_admission_epoch,
                            rollout_config_generation,
                            expiry,
                            now_text,
                            _WRITER_LEASE_NAME,
                            row["epoch"],
                            row["admission_epoch"],
                            now_text,
                        ),
                    )
                    if update.rowcount != 1:
                        await connection.rollback()
                        return None
                    result = RecoveryWriterFence(
                        owner_instance_id=owner_instance_id,
                        writer_lease_epoch=next_epoch,
                        writer_admission_epoch=next_admission_epoch,
                        rollout_config_generation=rollout_config_generation,
                        lease_expires_at=expiry,
                    )
                self._inject(TerminalProtocolFaultPoint.RECOVERY_FENCE_AFTER_CAS)
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def heartbeat_recovery_writer_fence(
        self,
        fence: RecoveryWriterFence,
    ) -> RecoveryWriterFence:
        if not isinstance(fence, RecoveryWriterFence):
            raise TypeError("recovery writer fence is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                expiry = _iso_utc(now + timedelta(seconds=_RECOVERY_LEASE_SECONDS))
                update = await connection.execute(
                    """
                    UPDATE terminal_protocol_writer_lease
                    SET lease_expires_at=?,heartbeat_at=?
                    WHERE lease_name=? AND owner_instance_id=? AND epoch=?
                      AND admission_epoch=? AND admission_state='draining'
                      AND rollout_config_generation=? AND lease_expires_at>?
                    """,
                    (
                        expiry,
                        _iso_utc(now),
                        _WRITER_LEASE_NAME,
                        fence.owner_instance_id,
                        fence.writer_lease_epoch,
                        fence.writer_admission_epoch,
                        fence.rollout_config_generation,
                        _iso_utc(now),
                    ),
                )
                if update.rowcount != 1:
                    raise TerminalProtocolMalformedError("recovery writer fence ownership changed")
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return replace(fence, lease_expires_at=expiry)

    async def release_recovery_writer_fence(self, fence: RecoveryWriterFence) -> bool:
        if not isinstance(fence, RecoveryWriterFence):
            raise TypeError("recovery writer fence is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                update = await connection.execute(
                    """
                    UPDATE terminal_protocol_writer_lease
                    SET owner_instance_id=NULL,admission_state='closed',
                        admission_epoch=admission_epoch+1,lease_expires_at=NULL,heartbeat_at=?
                    WHERE lease_name=? AND owner_instance_id=? AND epoch=?
                      AND admission_epoch=? AND admission_state='draining'
                    """,
                    (
                        _iso_utc(self._now()),
                        _WRITER_LEASE_NAME,
                        fence.owner_instance_id,
                        fence.writer_lease_epoch,
                        fence.writer_admission_epoch,
                    ),
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return update.rowcount == 1

    async def acquire_terminal_writer_lease(
        self,
        *,
        owner_instance_id: str,
        rollout_config_generation: str,
    ) -> TerminalWriterLease | None:
        """Acquire the single active fresh-admission writer lease."""

        if any(not isinstance(value, str) or not value for value in (owner_instance_id, rollout_config_generation)):
            raise ValueError("terminal writer identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                now_text = _iso_utc(now)
                expiry = _iso_utc(now + timedelta(seconds=_RECOVERY_LEASE_SECONDS))
                async with connection.execute(
                    "SELECT * FROM terminal_protocol_writer_lease WHERE lease_name=?",
                    (_WRITER_LEASE_NAME,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    epoch = 1
                    admission_epoch = 1
                    await connection.execute(
                        """
                        INSERT INTO terminal_protocol_writer_lease(
                          lease_name,owner_instance_id,epoch,admission_state,
                          admission_epoch,rollout_config_generation,
                          deployment_attestation_id,lease_expires_at,heartbeat_at
                        ) VALUES (?, ?,?,'open',?,?,NULL,?,?)
                        """,
                        (
                            _WRITER_LEASE_NAME,
                            owner_instance_id,
                            epoch,
                            admission_epoch,
                            rollout_config_generation,
                            expiry,
                            now_text,
                        ),
                    )
                elif (
                    row["owner_instance_id"] == owner_instance_id
                    and row["admission_state"] == "open"
                    and row["rollout_config_generation"] == rollout_config_generation
                    and _timestamp_after(row["lease_expires_at"], now)
                ):
                    await connection.rollback()
                    return _terminal_writer_lease_from_row(row)
                elif row["owner_instance_id"] is not None and _timestamp_after(
                    row["lease_expires_at"],
                    now,
                ):
                    await connection.rollback()
                    return None
                else:
                    epoch = _nonnegative_int(row["epoch"], "writer.epoch") + 1
                    admission_epoch = (
                        _nonnegative_int(
                            row["admission_epoch"],
                            "writer.admission_epoch",
                        )
                        + 1
                    )
                    update = await connection.execute(
                        """
                        UPDATE terminal_protocol_writer_lease
                        SET owner_instance_id=?,epoch=?,admission_state='open',
                            admission_epoch=?,rollout_config_generation=?,
                            deployment_attestation_id=NULL,lease_expires_at=?,heartbeat_at=?
                        WHERE lease_name=? AND epoch=? AND admission_epoch=?
                          AND (owner_instance_id IS NULL OR lease_expires_at IS NULL
                               OR lease_expires_at<=?)
                        """,
                        (
                            owner_instance_id,
                            epoch,
                            admission_epoch,
                            rollout_config_generation,
                            expiry,
                            now_text,
                            _WRITER_LEASE_NAME,
                            row["epoch"],
                            row["admission_epoch"],
                            now_text,
                        ),
                    )
                    if update.rowcount != 1:
                        await connection.rollback()
                        return None
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return TerminalWriterLease(
            owner_instance_id=owner_instance_id,
            writer_lease_epoch=epoch,
            writer_admission_epoch=admission_epoch,
            rollout_config_generation=rollout_config_generation,
            lease_expires_at=expiry,
            admission_state="open",
        )

    async def heartbeat_terminal_writer_lease(
        self,
        lease: TerminalWriterLease,
    ) -> TerminalWriterLease:
        if not isinstance(lease, TerminalWriterLease):
            raise TypeError("terminal writer lease is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            now = self._now()
            expiry = _iso_utc(now + timedelta(seconds=_RECOVERY_LEASE_SECONDS))
            update = await connection.execute(
                """
                UPDATE terminal_protocol_writer_lease
                SET lease_expires_at=?,heartbeat_at=?
                WHERE lease_name=? AND owner_instance_id=? AND epoch=?
                  AND admission_epoch=? AND admission_state=?
                  AND rollout_config_generation=? AND lease_expires_at>?
                """,
                (
                    expiry,
                    _iso_utc(now),
                    _WRITER_LEASE_NAME,
                    lease.owner_instance_id,
                    lease.writer_lease_epoch,
                    lease.writer_admission_epoch,
                    lease.admission_state,
                    lease.rollout_config_generation,
                    _iso_utc(now),
                ),
            )
            await connection.commit()
        if update.rowcount != 1:
            raise TerminalProtocolMalformedError("terminal writer lease ownership changed")
        return replace(lease, lease_expires_at=expiry)

    async def drain_terminal_writer_lease(
        self,
        lease: TerminalWriterLease,
    ) -> TerminalWriterLease:
        if not isinstance(lease, TerminalWriterLease):
            raise TypeError("terminal writer lease is invalid")
        if lease.admission_state == "draining":
            return lease
        async with open_terminal_protocol_connection(self._db_path) as connection:
            now = self._now()
            expiry = _iso_utc(now + timedelta(seconds=_RECOVERY_LEASE_SECONDS))
            next_admission_epoch = lease.writer_admission_epoch + 1
            update = await connection.execute(
                """
                UPDATE terminal_protocol_writer_lease
                SET admission_state='draining',admission_epoch=?,
                    lease_expires_at=?,heartbeat_at=?
                WHERE lease_name=? AND owner_instance_id=? AND epoch=?
                  AND admission_epoch=? AND admission_state='open'
                  AND lease_expires_at>?
                """,
                (
                    next_admission_epoch,
                    expiry,
                    _iso_utc(now),
                    _WRITER_LEASE_NAME,
                    lease.owner_instance_id,
                    lease.writer_lease_epoch,
                    lease.writer_admission_epoch,
                    _iso_utc(now),
                ),
            )
            await connection.commit()
        if update.rowcount != 1:
            raise TerminalProtocolMalformedError("terminal writer drain fence changed")
        return replace(
            lease,
            writer_admission_epoch=next_admission_epoch,
            lease_expires_at=expiry,
            admission_state="draining",
        )

    async def release_terminal_writer_lease(
        self,
        lease: TerminalWriterLease,
    ) -> bool:
        if not isinstance(lease, TerminalWriterLease):
            raise TypeError("terminal writer lease is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            update = await connection.execute(
                """
                UPDATE terminal_protocol_writer_lease
                SET owner_instance_id=NULL,admission_state='closed',
                    admission_epoch=admission_epoch+1,
                    lease_expires_at=NULL,heartbeat_at=?
                WHERE lease_name=? AND owner_instance_id=? AND epoch=?
                  AND admission_epoch=? AND admission_state=?
                """,
                (
                    _iso_utc(self._now()),
                    _WRITER_LEASE_NAME,
                    lease.owner_instance_id,
                    lease.writer_lease_epoch,
                    lease.writer_admission_epoch,
                    lease.admission_state,
                ),
            )
            await connection.commit()
        return update.rowcount == 1

    async def record_decision(self, request: DecisionRecordRequest) -> RecordedDecision:
        """Atomically consume a clarification/approval source and record its v1 decision."""

        if not isinstance(request, DecisionRecordRequest):
            raise TypeError("decision record request is invalid")
        expected_decision_id = stable_decision_id(
            scope=request.context.scope,
            source_kind=request.context.source_kind,
            source_id=request.context.source_id,
            action=request.context.action,
        )
        if request.context.decision_id != expected_decision_id:
            raise ValueError("decision id does not match its stable source identity")
        carries_system_authority = request.context.action is DecisionAction.EXPIRE or (
            request.context.source_kind is DecisionSourceKind.SYSTEM_CONTROL and request.context.action is DecisionAction.REVOKE
        )
        if carries_system_authority and not is_narrow_system_decision(request.context):
            raise DecisionAccessRevokedError("system decision authority is invalid")
        if (
            request.context.source_kind is DecisionSourceKind.SYSTEM_CONTROL
            and request.context.action is DecisionAction.CANCEL
            and not is_web_cancel_decision(request.context)
        ):
            raise DecisionAccessRevokedError("system cancel authority is invalid")
        source_projection_json = canonical_json_v1(canonical_decision_source_audit_projection(request))
        source_projection_digest = canonical_decision_payload_digest(request)
        if request.decision_payload_digest != source_projection_digest:
            raise ValueError("decision payload digest changed")
        digest = decision_identity_digest(request)
        control_outcome_code = _decision_control_outcome_code(request)
        result: RecordedDecision
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await _read_decision_protocol_row(connection, request.context.scope)
                _validate_decision_protocol_row(row, request.context)
                await _validate_current_decision_auth(connection, request.context)

                replay = await _read_decision_replay(
                    connection,
                    request=request,
                    decision_digest=digest,
                )
                if replay is not None:
                    source = await _read_decision_source(connection, request.context)
                    assert row is not None
                    _validate_decision_replay_source(source, request, row=row)
                    await connection.rollback()
                    return replay
                if await _has_competing_decision(connection, request.context):
                    raise DecisionConflictError("prior generation already has a different decision")

                assert row is not None
                _validate_decision_pause_truth(row, request.context)
                source = await _read_decision_source(connection, request.context)
                _validate_decision_source(source, request, row=row)
                await _validate_pending_source_binding(
                    connection,
                    row=row,
                    request=request,
                )
                self._inject(TerminalProtocolFaultPoint.DECISION_BEFORE_SOURCE_CAS)
                decided_at = _iso_utc(self._now())
                if request.context.source_kind is DecisionSourceKind.SYSTEM_CONTROL:
                    # System controls have no source-row transition.  The raw
                    # card remains immutable audit evidence; actionable reads
                    # are gated by the Turn's current terminal receipt.
                    pass
                else:
                    await _consume_decision_source(
                        connection,
                        source=source,
                        request=request,
                        decided_at=decided_at,
                    )
                self._inject(TerminalProtocolFaultPoint.DECISION_AFTER_SOURCE_CAS_BEFORE_INSERT)
                now_text = _iso_utc(self._now())
                await connection.execute(
                    """
                    INSERT INTO turn_control_decisions(
                      decision_id,source_kind,source_id,turn_id,prior_generation,action,
                      decision_digest,state,target_generation,actor_ref,requester_ref,
                      effect_principal_ref,source_projection_json,source_projection_digest,
                      prior_receipt_id,prior_generation_transition_epoch,
                      authz_snapshot_digest,created_at,recorded_at,consumed_at,
                      control_outcome_code
                    ) VALUES (?,?,?,?,?,?,?,'decision_recorded',NULL,?,?,?,?,?,?,?,?,?,?,NULL,?)
                    """,
                    (
                        request.context.decision_id,
                        request.context.source_kind.value,
                        request.context.source_id,
                        request.context.scope.turn_id,
                        request.context.scope.generation,
                        request.context.action.value,
                        digest,
                        request.context.actor_ref,
                        request.context.principals.requester_ref,
                        request.context.principals.effect_principal_ref,
                        source_projection_json,
                        source_projection_digest,
                        request.context.prior_receipt_id,
                        _positive_int(
                            row["generation_transition_epoch"],
                            "decision.generation_transition_epoch",
                        ),
                        request.context.authz_snapshot_digest,
                        now_text,
                        now_text,
                        control_outcome_code,
                    ),
                )
                for effect in request.side_effects:
                    await connection.execute(
                        """
                        INSERT INTO turn_decision_effects(
                          effect_id,decision_id,effect_kind,target_ref,payload_json,
                          payload_digest,state,created_at
                        ) VALUES (?,?,?,?,?,?,'pending',?)
                        """,
                        (
                            effect.effect_id,
                            request.context.decision_id,
                            effect.effect_kind,
                            effect.target_ref,
                            effect.payload_json,
                            effect.payload_digest,
                            now_text,
                        ),
                    )
                result = RecordedDecision(
                    context=request.context,
                    state=DecisionState.DECISION_RECORDED,
                    decision_digest=digest,
                    target_generation=None,
                    replayed=False,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.DECISION_AFTER_COMMIT_BEFORE_ACK)
        return result

    async def open_resume_generation(self, request: ResumeGenerationRequest) -> ResumeGenerationResult:
        """Open N+1 and persist ``turn.resumed`` with every mutable pointer in one CAS."""

        if not isinstance(request, ResumeGenerationRequest):
            raise TypeError("resume generation request is invalid")
        context = request.decision.context
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                stored = await _read_stored_decision(connection, context.decision_id)
                _validate_stored_decision(stored, request.decision)
                assert stored is not None
                state = DecisionState(_required_text(stored["state"], "decision.state"))
                target = stored["target_generation"]
                if state is DecisionState.GENERATION_OPENED:
                    target_generation = _positive_int(target, "decision.target_generation")
                    scope = replace(context.scope, generation=target_generation)
                    await _validate_resume_generation_replay(
                        connection,
                        scope=scope,
                        request=request,
                    )
                    await connection.rollback()
                    return ResumeGenerationResult(
                        disposition=DecisionOpenDisposition.REPLAY,
                        decision=replace(
                            request.decision,
                            state=state,
                            target_generation=target_generation,
                            replayed=True,
                        ),
                        scope=scope,
                    )
                current_auth = await _read_auth_fence_vector(
                    connection,
                    _decision_auth_fence_scopes(context),
                )
                if current_auth.auth_fence_vector_digest != request.current_authz_snapshot_digest:
                    await connection.rollback()
                    return ResumeGenerationResult(
                        disposition=DecisionOpenDisposition.ACCESS_REVOKED,
                        decision=request.decision,
                    )
                if state is not DecisionState.DECISION_RECORDED:
                    await connection.rollback()
                    return ResumeGenerationResult(
                        disposition=DecisionOpenDisposition.ALREADY_TERMINAL,
                        decision=request.decision,
                    )

                row = await _read_decision_protocol_row(connection, context.scope)
                _validate_decision_protocol_row(row, context)
                assert row is not None
                if row["recovery_lock_turn_id"] is not None:
                    await connection.rollback()
                    return ResumeGenerationResult(
                        disposition=DecisionOpenDisposition.RECOVERY_LOCKED,
                        decision=request.decision,
                    )
                if row["deleted_at"] is not None or await _has_active_session_lifecycle(
                    connection,
                    context.scope.session_id,
                ):
                    await connection.rollback()
                    return ResumeGenerationResult(
                        disposition=DecisionOpenDisposition.ACCESS_REVOKED,
                        decision=request.decision,
                    )
                if any(
                    row[name] is not None
                    for name in (
                        "steer_freeze_turn_id",
                        "steer_freeze_generation",
                        "steer_freeze_claim_epoch",
                        "steer_frozen_revision",
                    )
                ):
                    raise TerminalProtocolMalformedError("resume Session retains a stale steer freeze")
                if row["active_turn_id"] is not None or row["steer_buffer_state"] != "collecting" or _parse_steer_buffer(row["steer_buffer_json"]):
                    await connection.rollback()
                    return ResumeGenerationResult(
                        disposition=DecisionOpenDisposition.BUSY,
                        decision=request.decision,
                    )
                if row["generation_transition_epoch"] != request.expected_generation_transition_epoch or request.expected_session_claim_epoch != 0:
                    await connection.rollback()
                    return ResumeGenerationResult(
                        disposition=DecisionOpenDisposition.BUSY,
                        decision=request.decision,
                    )
                if not await _writer_lease_matches_identity(
                    connection,
                    owner_instance_id=request.writer_instance_id,
                    writer_lease_epoch=request.writer_lease_epoch,
                    now=self._now(),
                    allowed_states=frozenset({"open", "draining"}),
                ):
                    await connection.rollback()
                    return ResumeGenerationResult(
                        disposition=DecisionOpenDisposition.RECOVERY_LOCKED,
                        decision=request.decision,
                    )

                _validate_decision_pause_truth(row, context)
                target_generation = context.scope.generation + 1
                now_text = _iso_utc(self._now())
                scope = replace(context.scope, generation=target_generation)
                await connection.execute(
                    """
                    INSERT INTO turn_generations(
                      turn_id,generation,protocol_version,kind,prior_generation,decision_id,
                      execution_intent_id,execution_intent_digest,admission_request_digest,
                      execution_intent_json,execution_authz_snapshot_digest,opened_at,state
                    ) VALUES (?,?,1,'execution',?,?,?,?,?,?,?,?,'opened')
                    """,
                    (
                        scope.turn_id,
                        target_generation,
                        context.scope.generation,
                        context.decision_id,
                        request.execution_intent_id,
                        request.execution_intent_digest,
                        request.decision.decision_digest,
                        canonical_json_v1(dict(request.execution_intent)),
                        request.current_authz_snapshot_digest,
                        now_text,
                    ),
                )
                self._inject(TerminalProtocolFaultPoint.RESUME_BEFORE_EVENT_ALLOC)
                event_seq = _positive_int(row["event_sequence_next"], "turn.event_sequence_next")
                async with connection.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM turn_events WHERE turn_id=?",
                    (scope.turn_id,),
                ) as cursor:
                    legacy_row = await cursor.fetchone()
                legacy_seq = int(legacy_row[0]) if legacy_row is not None else 1
                event_payload = {
                    "decision_id": context.decision_id,
                    "generation_transition_epoch": request.expected_generation_transition_epoch + 1,
                    "prior_generation": context.scope.generation,
                    "prior_receipt_id": context.prior_receipt_id,
                    "protocol": 1,
                    "target_generation": target_generation,
                }
                event_id = f"evt_{canonical_digest_v1(event_payload)[:32]}"
                payload_json = canonical_json_v1(event_payload)
                await connection.execute(
                    """
                    INSERT INTO turn_events(
                      id,turn_id,seq,event_type,payload_json,occurred_at,protocol_version,
                      generation,event_seq,actor_scope,phase_scope,payload_digest,
                      terminal_receipt_id,source_proposal_id,accept_ordinal
                    ) VALUES (?,?,?,?,?,?,1,?,?, 'root','control_transition',?,NULL,NULL,NULL)
                    """,
                    (
                        event_id,
                        scope.turn_id,
                        legacy_seq,
                        "turn.resumed",
                        payload_json,
                        now_text,
                        target_generation,
                        event_seq,
                        canonical_digest_v1(event_payload),
                    ),
                )
                self._inject(TerminalProtocolFaultPoint.RESUME_AFTER_EVENT_BEFORE_POINTER)
                turn_update = await connection.execute(
                    """
                    UPDATE turns
                    SET current_generation=?,generation_transition_epoch=generation_transition_epoch+1,
                        current_terminal_receipt_id=NULL,event_sequence_next=event_sequence_next+1,
                        status='running',ended_at=NULL,final_answer=NULL,error=NULL,error_kind=NULL
                    WHERE id=? AND terminal_protocol_version=1 AND current_generation=?
                      AND generation_transition_epoch=? AND current_terminal_receipt_id=?
                    """,
                    (
                        target_generation,
                        scope.turn_id,
                        context.scope.generation,
                        request.expected_generation_transition_epoch,
                        context.prior_receipt_id,
                    ),
                )
                if turn_update.rowcount != 1:
                    raise TerminalProtocolMalformedError("resume Turn pointer CAS changed")
                session_update = await connection.execute(
                    """
                    UPDATE sessions
                    SET active_turn_id=?,active_turn_protocol_version=1,
                        active_turn_generation=?,active_turn_claim_epoch=0
                    WHERE id=? AND active_turn_id IS NULL AND active_turn_protocol_version IS NULL
                      AND active_turn_generation IS NULL AND active_turn_claim_epoch IS NULL
                      AND recovery_lock_turn_id IS NULL AND steer_buffer_state='collecting'
                      AND steer_buffer_json='[]'
                    """,
                    (scope.turn_id, target_generation, scope.session_id),
                )
                if session_update.rowcount != 1:
                    raise TerminalProtocolMalformedError("resume Session active tuple CAS changed")
                decision_update = await connection.execute(
                    """
                    UPDATE turn_control_decisions
                    SET state='generation_opened',target_generation=?,consumed_at=?
                    WHERE decision_id=? AND state='decision_recorded' AND target_generation IS NULL
                    """,
                    (target_generation, now_text, context.decision_id),
                )
                if decision_update.rowcount != 1:
                    raise DecisionConflictError("resume decision was consumed concurrently")
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.RESUME_AFTER_OPEN_BEFORE_CLAIM)
        return ResumeGenerationResult(
            disposition=DecisionOpenDisposition.OPENED,
            decision=replace(
                request.decision,
                state=DecisionState.GENERATION_OPENED,
                target_generation=target_generation,
                replayed=False,
            ),
            scope=scope,
        )

    async def commit_control_generation(self, request: ControlGenerationRequest) -> CommittedReceipt:
        """Commit N+1 control generation and receipt without creating a worker."""

        if not isinstance(request, ControlGenerationRequest):
            raise TypeError("control generation request is invalid")
        context = request.decision.context
        result: CommittedReceipt
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                stored = await _read_stored_decision(connection, context.decision_id)
                _validate_stored_decision(stored, request.decision)
                assert stored is not None
                _validate_control_outcome_mapping(stored, request)
                current_auth = await _read_auth_fence_vector(
                    connection,
                    _decision_auth_fence_scopes(context),
                )
                if current_auth.auth_fence_vector_digest != request.current_authz_snapshot_digest:
                    raise DecisionAccessRevokedError("control authorization changed")
                state = DecisionState(_required_text(stored["state"], "decision.state"))
                if state is DecisionState.TERMINAL_CONTROL_COMMITTED:
                    result = await _read_control_replay_receipt(
                        connection,
                        decision=request.decision,
                        target_generation=_positive_int(
                            stored["target_generation"],
                            "decision.target_generation",
                        ),
                    )
                    await connection.rollback()
                    return result
                if state is not DecisionState.DECISION_RECORDED:
                    raise DecisionConflictError("decision cannot open a control generation")
                if not await _writer_lease_matches_control_request(connection, request, self._now()):
                    raise DecisionAccessRevokedError("control writer lease is unavailable")

                row = await _read_decision_protocol_row(connection, context.scope)
                _validate_decision_protocol_row(row, context)
                assert row is not None
                _validate_decision_pause_truth(row, context)
                if row["active_turn_id"] == context.scope.turn_id:
                    raise TerminalProtocolMalformedError("control cannot preserve an active tuple for its own Turn")
                target_generation = context.scope.generation + 1
                scope = replace(context.scope, generation=target_generation)
                now = self._now()
                now_text = _iso_utc(now)
                finalization_owner = f"{request.writer_instance_id}:control"
                finalization_expiry = _iso_utc(now + timedelta(seconds=_FINALIZATION_LEASE_SECONDS))
                await connection.execute(
                    """
                    INSERT INTO turn_generations(
                      turn_id,generation,protocol_version,kind,prior_generation,decision_id,
                      opened_at,state,row_version,closing_reason,closing_accepted_through,
                      owner_instance_id,claim_epoch,finalization_owner_instance_id,
                      finalization_epoch,finalization_lease_expires_at,finalization_started_at
                    ) VALUES (?,?,1,'control',?,?,?,'closing',1,?,0,?,0,?,1,?,?)
                    """,
                    (
                        scope.turn_id,
                        target_generation,
                        context.scope.generation,
                        context.decision_id,
                        now_text,
                        request.reason_code,
                        request.writer_instance_id,
                        finalization_owner,
                        finalization_expiry,
                        now_text,
                    ),
                )
                turn_update = await connection.execute(
                    """
                    UPDATE turns
                    SET current_generation=?,generation_transition_epoch=generation_transition_epoch+1,
                        current_terminal_receipt_id=NULL
                    WHERE id=? AND terminal_protocol_version=1 AND current_generation=?
                      AND current_terminal_receipt_id=?
                    """,
                    (
                        target_generation,
                        scope.turn_id,
                        context.scope.generation,
                        context.prior_receipt_id,
                    ),
                )
                if turn_update.rowcount != 1:
                    raise DecisionConflictError("control Turn pointer CAS changed")
                highwater = _positive_int(row["event_sequence_next"], "turn.event_sequence_next") - 1
                plan = _control_candidate_plan(
                    request=request,
                    scope=scope,
                    persisted_event_highwater=highwater,
                )
                _validate_candidate_plan_envelopes(plan)
                self._inject(TerminalProtocolFaultPoint.CONTROL_BEFORE_CANDIDATE)
                finalization = FinalizationContext(
                    scope=scope,
                    execution_owner_instance_id=request.writer_instance_id,
                    execution_claim_epoch=0,
                    finalization_owner_instance_id=finalization_owner,
                    finalization_epoch=1,
                    generation_row_version=1,
                    closing_accepted_through=0,
                    lease_expires_at=finalization_expiry,
                )
                await _insert_terminal_candidate(
                    connection,
                    context=finalization,
                    plan=plan,
                    prepared_at=now_text,
                )
                generation_update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET terminal_candidate_id=?,row_version=row_version+1
                    WHERE turn_id=? AND generation=? AND protocol_version=1 AND state='closing'
                      AND row_version=1 AND terminal_candidate_id IS NULL AND terminal_receipt_id IS NULL
                      AND finalization_owner_instance_id=? AND finalization_epoch=1
                    """,
                    (
                        plan.candidate_id,
                        scope.turn_id,
                        target_generation,
                        finalization_owner,
                    ),
                )
                if generation_update.rowcount != 1:
                    raise TerminalProtocolMalformedError("control candidate prepare CAS changed")
                prepared = PreparedFinalizationContext(
                    finalization=replace(finalization, generation_row_version=2),
                    candidate_id=plan.candidate_id,
                    candidate_projection_digest=plan.candidate_projection_digest,
                )
                result = await _commit_terminal_candidate(
                    connection,
                    context=prepared,
                    plan=plan,
                    selected=plan.variants[0],
                    authz_snapshot_digest=request.current_authz_snapshot_digest,
                    writer_admission_epoch=request.writer_admission_epoch,
                    committed_at=now_text,
                    fault_injector=self._inject,
                )
                decision_update = await connection.execute(
                    """
                    UPDATE turn_control_decisions
                    SET state='terminal_control_committed',target_generation=?,consumed_at=?
                    WHERE decision_id=? AND state='decision_recorded' AND target_generation IS NULL
                    """,
                    (target_generation, now_text, context.decision_id),
                )
                if decision_update.rowcount != 1:
                    raise DecisionConflictError("control decision was consumed concurrently")
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.CONTROL_AFTER_RECEIPT_BEFORE_ACK)
        return result

    async def authorize_active_v1_control(
        self,
        *,
        context: AdmissionContext,
        expected_turn_id: str,
    ) -> ActiveV1ControlAuthorization:
        """Bind an in-process control to the current Session owner and v1 tuple.

        Cancellation signalling itself is process-local, so it cannot be made
        part of the SQLite transaction.  This read-only ``BEGIN IMMEDIATE``
        fence nevertheless guarantees that the caller, Team, current auth
        vector and exact active Turn/generation were one authoritative state at
        the decision boundary.  A later Turn swap makes signalling the old
        identity harmless; a caller mismatch never reaches the signal.
        """

        if not isinstance(context, AdmissionContext):
            raise TypeError("active v1 control context is invalid")
        if not isinstance(expected_turn_id, str) or not expected_turn_id:
            raise ValueError("active v1 control turn id is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                session = await _read_session_for_admission(
                    connection,
                    context.session_id,
                )
                if session is None:
                    return ActiveV1ControlAuthorization(
                        scope=None,
                        code=AdmissionCode.SESSION_NOT_FOUND,
                    )
                if session["bound_team_id"] != context.team_id or session["user_id"] != context.actor:
                    return ActiveV1ControlAuthorization(
                        scope=None,
                        code=AdmissionCode.SESSION_ACCESS_DENIED,
                    )
                if session["deleted_at"] is not None:
                    return ActiveV1ControlAuthorization(
                        scope=None,
                        code=AdmissionCode.SESSION_DELETED,
                    )
                current_auth = await _read_auth_fence_vector(
                    connection,
                    _admission_auth_fence_scopes(context),
                )
                if current_auth.auth_fence_vector_digest != context.auth_fence_vector_digest:
                    return ActiveV1ControlAuthorization(
                        scope=None,
                        code=AdmissionCode.AUTH_FENCE_CHANGED,
                    )
                if session["active_turn_id"] != expected_turn_id:
                    return ActiveV1ControlAuthorization(
                        scope=None,
                        code=AdmissionCode.SESSION_TURN_ACTIVE,
                    )
                protocol_version, generation, _claim_epoch = _active_protocol_tuple(session)
                if protocol_version != 1:
                    if protocol_version is not None and protocol_version > 1:
                        raise TerminalProtocolFutureVersionError("active Turn uses a future terminal protocol")
                    raise TerminalProtocolMalformedError("active v1 control tuple is malformed")
                assert generation is not None
                async with connection.execute(
                    """
                    SELECT terminal_protocol_version,current_generation,session_id
                    FROM turns WHERE id=?
                    """,
                    (expected_turn_id,),
                ) as cursor:
                    turn = await cursor.fetchone()
                if (
                    turn is None
                    or turn["session_id"] != context.session_id
                    or turn["terminal_protocol_version"] != 1
                    or turn["current_generation"] != generation
                ):
                    raise TerminalProtocolMalformedError("active v1 control Turn identity changed")
                return ActiveV1ControlAuthorization(
                    scope=TerminalScope(
                        team_id=context.team_id,
                        session_id=context.session_id,
                        turn_id=expected_turn_id,
                        generation=generation,
                    ),
                    code=None,
                )
            finally:
                if connection.in_transaction:
                    await connection.rollback()

    async def admit_or_steer_v1(self, request: TerminalAdmissionRequest) -> TerminalAdmissionResult:
        """线性化一个首轮 v1 admission 或当前 active Turn 的稳定 steer。

        Gateway 必须先完成 §6.1 preflight。本方法仍在 ``BEGIN IMMEDIATE`` 内重读
        Session/team/user/recovery/writer fences；任何 rejection 都不会留下协议写入。
        ``deferred`` 的消费依赖 S5 的 immutable disposition plan，S1 只 fail closed 保留。
        """

        intent_payload_copy = _canonical_mapping_copy(request.execution_intent)
        if request.user_source is not None:
            request = replace(
                request,
                execution_intent=intent_payload_copy,
                user_source=snapshot_chat_turn_user_source(request.user_source),
            )
        request_digest = canonical_admission_request_digest(
            execution_intent=intent_payload_copy,
            request_id=request.context.request_id,
            session_id=request.context.session_id,
            team_id=request.context.team_id,
            turn_id=request.turn_id,
            user_text=request.user_text,
            expected_execution_generation=request.expected_execution_generation,
            user_message_id=request.user_message_id,
        )
        result: TerminalAdmissionResult
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                now_text = _iso_utc(now)
                if request.create_session_if_missing:
                    assert request.user_source is not None
                    try:
                        await _ensure_chat_session_in_tx(
                            connection,
                            session_id=request.context.session_id,
                            workspace_id=request.user_source.workspace_id,
                            user_id=request.context.actor,
                            now=now_text,
                        )
                    except ValueError as exc:
                        if str(exc) != "chat_session_scope_conflict":
                            raise
                        await connection.rollback()
                        return _admission_rejection(
                            request.context,
                            request_digest,
                            AdmissionCode.REQUEST_CONFLICT,
                        )
                session = await _read_session_for_admission(connection, request.context.session_id)
                rejection = _validate_admission_session(session, request.context, request_digest)
                if rejection is not None:
                    await connection.rollback()
                    return rejection
                assert session is not None
                current_auth_fence_vector = await _read_auth_fence_vector(
                    connection,
                    _admission_auth_fence_scopes(request.context),
                )
                if current_auth_fence_vector.auth_fence_vector_digest != request.context.auth_fence_vector_digest:
                    await connection.rollback()
                    return _admission_rejection(
                        request.context,
                        request_digest,
                        AdmissionCode.AUTH_FENCE_CHANGED,
                    )

                protocol_facts = await read_session_protocol_facts_in_tx(
                    connection,
                    session,
                )
                if request.require_existing_v1_history and not protocol_facts.has_v1_history:
                    await connection.rollback()
                    return _admission_rejection(
                        request.context,
                        request_digest,
                        AdmissionCode.REQUEST_CONFLICT,
                    )

                if request.expected_execution_generation is not None:
                    matching_steers = [
                        item for item in _parse_steer_buffer(session["steer_buffer_json"]) if item.get("steer_id") == request.context.request_id
                    ]
                    if len(matching_steers) > 1:
                        raise TerminalProtocolMalformedError("steer request identity is not unique")
                    if matching_steers and (
                        matching_steers[0].get("request_digest") != request_digest or matching_steers[0].get("requested_turn_id") != request.turn_id
                    ):
                        await connection.rollback()
                        return _admission_rejection(
                            request.context,
                            request_digest,
                            AdmissionCode.REQUEST_CONFLICT,
                        )
                    protocol_version, generation, _claim_epoch = _active_protocol_tuple(
                        session,
                    )
                    if session["active_turn_id"] != request.turn_id or protocol_version != 1 or generation != request.expected_execution_generation:
                        await connection.rollback()
                        return _admission_rejection(
                            request.context,
                            request_digest,
                            AdmissionCode.STEER_TARGET_CHANGED,
                        )

                if request.user_source is not None:
                    replay = await _read_start_admission_replay(
                        connection,
                        request=request,
                        request_digest=request_digest,
                    )
                    if replay is not None and replay.disposition is not AdmissionDisposition.ADMITTED_REPLAY:
                        await connection.rollback()
                        return replay
                    if replay is not None:
                        await connection.rollback()
                        return replay
                    if request.execution_intent.get("datarc_selection") is not None:
                        from bowhead_core.datarc_selection import DatarcSelectionSnapshot, require_datarc_context_migration

                        await require_datarc_context_migration(connection)
                        source = DatarcSelectionSnapshot.model_validate(request.execution_intent["datarc_selection"])
                        if (source.team_id, source.user_id) != (request.context.team_id, request.context.actor):
                            raise TerminalProtocolMalformedError("Datarc source identity differs from admission")
                    if not _typed_execution_context_matches(
                        request.execution_intent,
                        request.presentation_locale,
                    ):
                        await connection.rollback()
                        return _admission_rejection(
                            request.context,
                            request_digest,
                            AdmissionCode.REQUEST_CONFLICT,
                        )

                if await _has_active_session_lifecycle(
                    connection,
                    request.context.session_id,
                ):
                    await connection.rollback()
                    return _admission_rejection(
                        request.context,
                        request_digest,
                        AdmissionCode.SESSION_TRANSITIONING,
                    )
                if session["recovery_lock_turn_id"] is not None:
                    await connection.rollback()
                    return _admission_rejection(request.context, request_digest, AdmissionCode.SESSION_RECOVERY_LOCKED)
                if await _has_prior_effect_unknown(connection, request.context.session_id):
                    await connection.rollback()
                    return _admission_rejection(
                        request.context,
                        request_digest,
                        AdmissionCode.SESSION_PRIOR_EFFECT_UNKNOWN,
                    )

                if request.user_source is None and request.expected_execution_generation is None:
                    replay = await _read_legacy_admission_replay(
                        connection,
                        request=request,
                        request_digest=request_digest,
                    )
                    if replay is not None:
                        await connection.rollback()
                        return replay

                if not await _writer_lease_matches(
                    connection,
                    context=request.context,
                    now=now,
                    allowed_states=(
                        frozenset({"open", "draining"})
                        if request.expected_execution_generation is not None or request.require_existing_v1_history
                        else frozenset({"open"})
                    ),
                ):
                    await connection.rollback()
                    return _admission_rejection(request.context, request_digest, AdmissionCode.WRITER_NOT_READY)

                buffer_state = _required_text(session["steer_buffer_state"], "sessions.steer_buffer_state")
                if buffer_state == "deferred":
                    if session["active_turn_id"] is not None:
                        raise TerminalProtocolMalformedError("deferred Session cannot retain an active Turn")
                    if request.user_source is not None:
                        await connection.rollback()
                        return _admission_rejection(
                            request.context,
                            request_digest,
                            AdmissionCode.SESSION_DEFERRED_PENDING,
                        )
                    result = await _insert_deferred_admitted_turn(
                        connection,
                        request=request,
                        request_digest=request_digest,
                        session=session,
                        current_intent=intent_payload_copy,
                        admitted_at=now_text,
                    )
                elif buffer_state == "frozen":
                    await connection.rollback()
                    return _admission_rejection(
                        request.context,
                        request_digest,
                        AdmissionCode.SESSION_TRANSITIONING,
                    )

                elif session["active_turn_id"] is not None:
                    result = await _append_or_replay_steer(
                        connection,
                        request=request,
                        request_digest=request_digest,
                        intent_payload=intent_payload_copy,
                        session=session,
                        accepted_now=now,
                        accepted_at=now_text,
                    )
                else:
                    result = await _insert_admitted_turn(
                        connection,
                        request=request,
                        request_digest=request_digest,
                        session=session,
                        effective_intent=intent_payload_copy,
                        admitted_at=now_text,
                    )
                if not result.accepted:
                    await connection.rollback()
                    return result
                self._inject(TerminalProtocolFaultPoint.ADMISSION_BEFORE_COMMIT)
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.ADMISSION_AFTER_COMMIT)
        return result

    async def claim_generation(self, context: GenerationClaimContext) -> GenerationClaimResult:
        """原子 claim generation 与 Session claim epoch；commit 前不得创建 task。"""

        result: GenerationClaimResult
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                now_text = _iso_utc(now)
                claim_lease_expires_at = _iso_utc(now + timedelta(seconds=_EXECUTION_LEASE_SECONDS))
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None:
                    await connection.rollback()
                    return GenerationClaimResult(
                        disposition=GenerationClaimDisposition.REJECTED,
                        code=GenerationClaimCode.SCOPE_NOT_FOUND,
                    )
                if not await _writer_lease_matches_claim(connection, context=context, now=now):
                    await connection.rollback()
                    return _claim_rejection(row, GenerationClaimCode.WRITER_LEASE_UNAVAILABLE)

                if context.auth_principal_ref is not None:
                    current_auth = await _read_auth_fence_vector(
                        connection,
                        (
                            AuthFenceScope(
                                AuthFenceScopeKind.TEAM_MEMBERSHIP,
                                context.scope.team_id,
                                context.scope.team_id,
                            ),
                            AuthFenceScope(
                                AuthFenceScopeKind.RESOURCE_GRANT,
                                context.scope.team_id,
                                context.scope.team_id,
                            ),
                            AuthFenceScope(
                                AuthFenceScopeKind.SESSION_VISIBILITY,
                                context.scope.team_id,
                                context.scope.session_id,
                            ),
                            AuthFenceScope(
                                AuthFenceScopeKind.USER_ACCOUNT,
                                "__global__",
                                context.auth_principal_ref,
                            ),
                        ),
                    )
                    if current_auth.auth_fence_vector_digest != context.current_authz_snapshot_digest:
                        await connection.rollback()
                        return _claim_rejection(
                            row,
                            GenerationClaimCode.AUTH_FENCE_CHANGED,
                        )

                replay = _claim_replay(row, context, now)
                if replay is not None:
                    await connection.rollback()
                    return replay
                state = _generation_state(row["state"])
                if state is GenerationState.RUNNING:
                    await connection.rollback()
                    return _claim_rejection(row, GenerationClaimCode.GENERATION_ALREADY_CLAIMED)
                if state is not GenerationState.OPENED or int(row["claim_epoch"]) != context.expected_session_claim_epoch:
                    await connection.rollback()
                    return _claim_rejection(row, GenerationClaimCode.GENERATION_NOT_OPENED)
                if int(row["row_version"]) != context.expected_generation_row_version:
                    await connection.rollback()
                    return _claim_rejection(row, GenerationClaimCode.GENERATION_VERSION_CHANGED)
                if not _session_claim_matches(row, context):
                    await connection.rollback()
                    return _claim_rejection(row, GenerationClaimCode.SESSION_CLAIM_CHANGED)

                next_claim_epoch = context.expected_session_claim_epoch + 1
                generation_update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET state='running', row_version=row_version+1, owner_instance_id=?,
                        claim_epoch=?, writer_lease_epoch_at_claim=?, claimed_at=?,
                        lease_expires_at=?, last_heartbeat_at=?,
                        claim_authz_snapshot_digest=COALESCE(?,claim_authz_snapshot_digest)
                    WHERE turn_id=? AND generation=? AND protocol_version=1 AND state='opened'
                      AND row_version=? AND claim_epoch=? AND owner_instance_id IS NULL
                      AND terminal_receipt_id IS NULL AND recovery_resolution IS NULL
                    """,
                    (
                        context.owner_instance_id,
                        next_claim_epoch,
                        context.writer_lease_epoch,
                        now_text,
                        claim_lease_expires_at,
                        now_text,
                        context.current_authz_snapshot_digest,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.expected_generation_row_version,
                        context.expected_session_claim_epoch,
                    ),
                )
                if generation_update.rowcount != 1:
                    await connection.rollback()
                    return _claim_rejection(row, GenerationClaimCode.GENERATION_VERSION_CHANGED)
                self._inject(TerminalProtocolFaultPoint.CLAIM_AFTER_GENERATION_CAS)

                session_update = await connection.execute(
                    """
                    UPDATE sessions
                    SET active_turn_claim_epoch=?
                    WHERE id=? AND active_turn_id=? AND active_turn_protocol_version=1
                      AND active_turn_generation=? AND active_turn_claim_epoch=?
                      AND recovery_lock_turn_id IS NULL
                    """,
                    (
                        next_claim_epoch,
                        context.scope.session_id,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.expected_session_claim_epoch,
                    ),
                )
                if session_update.rowcount != 1:
                    await connection.rollback()
                    return _claim_rejection(row, GenerationClaimCode.SESSION_CLAIM_CHANGED)
                self._inject(TerminalProtocolFaultPoint.CLAIM_AFTER_SESSION_EPOCH_CAS)

                execution_context = ExecutionGenerationContext(
                    scope=context.scope,
                    owner_instance_id=context.owner_instance_id,
                    claim_epoch=next_claim_epoch,
                    generation_row_version=context.expected_generation_row_version + 1,
                    writer_lease_epoch=context.writer_lease_epoch,
                    lease_expires_at=claim_lease_expires_at,
                )
                result = GenerationClaimResult(
                    disposition=GenerationClaimDisposition.CLAIMED,
                    code=None,
                    context=execution_context,
                    observed_state=GenerationState.RUNNING,
                    observed_owner_instance_id=context.owner_instance_id,
                    observed_claim_epoch=next_claim_epoch,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.CLAIM_AFTER_COMMIT)
        return result

    async def mark_execution_started(self, context: ExecutionGenerationContext) -> ExecutionGenerationContext:
        """在任何 event/model/tool/cost 前 durable 标记 execution 已开始。"""

        result: ExecutionGenerationContext
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                now_text = _iso_utc(now)
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None or not _execution_context_matches(row, context, now):
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution generation ownership fence changed")
                if not await _writer_lease_matches_execution(connection, context=context, now=now):
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution writer lease is unavailable")
                if row["execution_started_at"] is not None:
                    await connection.rollback()
                    return ExecutionGenerationContext(
                        scope=context.scope,
                        owner_instance_id=context.owner_instance_id,
                        claim_epoch=context.claim_epoch,
                        generation_row_version=_positive_int(row["row_version"], "generation.row_version"),
                        writer_lease_epoch=context.writer_lease_epoch,
                        lease_expires_at=_required_text(row["lease_expires_at"], "generation.lease_expires_at"),
                    )
                if row["row_version"] != context.generation_row_version:
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution generation row version changed")

                self._inject(TerminalProtocolFaultPoint.EXECUTION_BEFORE_STARTED)
                update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET execution_started_at=?, row_version=row_version+1
                    WHERE turn_id=? AND generation=? AND protocol_version=1 AND state='running'
                      AND row_version=? AND owner_instance_id=? AND claim_epoch=?
                      AND writer_lease_epoch_at_claim=? AND execution_started_at IS NULL
                    """,
                    (
                        now_text,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.generation_row_version,
                        context.owner_instance_id,
                        context.claim_epoch,
                        context.writer_lease_epoch,
                    ),
                )
                if update.rowcount != 1:
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution generation ownership fence changed")
                result = ExecutionGenerationContext(
                    scope=context.scope,
                    owner_instance_id=context.owner_instance_id,
                    claim_epoch=context.claim_epoch,
                    generation_row_version=context.generation_row_version + 1,
                    writer_lease_epoch=context.writer_lease_epoch,
                    lease_expires_at=context.lease_expires_at,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def handoff_execution_to_recovery(
        self,
        context: ExecutionGenerationContext,
        *,
        reason_code: str,
    ) -> bool:
        """Durably expire a claimed worker lease so the bounded reconciler can take over."""

        if not isinstance(context, ExecutionGenerationContext):
            raise TypeError("execution recovery handoff context is invalid")
        if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 128:
            raise ValueError("execution recovery handoff reason is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now_text = _iso_utc(self._now())
                update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET lease_expires_at=?,last_heartbeat_at=?,recovery_reason=?
                    WHERE turn_id=? AND generation=? AND protocol_version=1 AND state='running'
                      AND owner_instance_id=? AND claim_epoch=? AND writer_lease_epoch_at_claim=?
                      AND terminal_receipt_id IS NULL AND recovery_resolution IS NULL
                    """,
                    (
                        now_text,
                        now_text,
                        reason_code,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.owner_instance_id,
                        context.claim_epoch,
                        context.writer_lease_epoch,
                    ),
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return update.rowcount == 1

    async def heartbeat_execution(
        self,
        context: ExecutionGenerationContext,
    ) -> ExecutionGenerationContext:
        """Renew one live generation lease under the original ownership fences.

        The heartbeat deliberately does not advance ``row_version``: event
        persistence and finalization capabilities remain valid while the
        lease timestamp moves forward.  An expired generation or writer lease
        is never revived by this method; recovery must claim it explicitly.
        """

        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None or not _execution_context_matches(row, context, now):
                    raise TerminalProtocolMalformedError("execution generation ownership fence changed")
                if not await _writer_lease_matches_execution(connection, context=context, now=now):
                    raise TerminalProtocolMalformedError("execution writer lease is unavailable")
                lease_expires_at = _iso_utc(now + timedelta(seconds=_EXECUTION_LEASE_SECONDS))
                update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET lease_expires_at=?,last_heartbeat_at=?
                    WHERE turn_id=? AND generation=? AND protocol_version=1
                      AND state='running' AND owner_instance_id=? AND claim_epoch=?
                      AND writer_lease_epoch_at_claim=?
                      AND terminal_candidate_id IS NULL AND terminal_receipt_id IS NULL
                    """,
                    (
                        lease_expires_at,
                        _iso_utc(now),
                        context.scope.turn_id,
                        context.scope.generation,
                        context.owner_instance_id,
                        context.claim_epoch,
                        context.writer_lease_epoch,
                    ),
                )
                if update.rowcount != 1:
                    raise TerminalProtocolMalformedError("execution generation ownership fence changed")
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return replace(context, lease_expires_at=lease_expires_at)

    async def read_frozen_steer_snapshot(
        self,
        scope: TerminalScope,
        *,
        claim_epoch: int,
    ) -> FrozenSteerSnapshot:
        """Read the immutable buffer frozen by ``begin_finalization``."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("frozen steer scope is invalid")
        if not isinstance(claim_epoch, int) or isinstance(claim_epoch, bool) or claim_epoch < 0:
            raise ValueError("frozen steer claim epoch is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT s.steer_buffer_json,s.steer_buffer_revision,s.steer_buffer_state,
                       s.steer_freeze_turn_id,s.steer_freeze_generation,
                       s.steer_freeze_claim_epoch,s.steer_frozen_revision,w.team_id
                FROM sessions s
                JOIN workspaces w ON w.id=s.workspace_id
                WHERE s.id=?
                """,
                (scope.session_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if (
            row is None
            or row["team_id"] != scope.team_id
            or row["steer_buffer_state"] != "frozen"
            or row["steer_freeze_turn_id"] != scope.turn_id
            or row["steer_freeze_generation"] != scope.generation
            or row["steer_freeze_claim_epoch"] != claim_epoch
            or row["steer_frozen_revision"] != row["steer_buffer_revision"]
        ):
            raise TerminalProtocolMalformedError("frozen steer fence changed")
        raw_items = _parse_steer_buffer(row["steer_buffer_json"])
        items: list[FrozenSteerItem] = []
        for raw in raw_items:
            intent = raw.get("execution_intent")
            user_text = intent.get("text") if isinstance(intent, dict) else None
            if not isinstance(user_text, str):
                raise TerminalProtocolMalformedError("frozen steer text is malformed")
            attachments = intent.get("attachments")
            planning_mode = intent.get("planning_mode")
            workspace_id = intent.get("workspace_id")
            if (
                not isinstance(attachments, list)
                or any(not isinstance(value, str) or not value for value in attachments)
                or (planning_mode is not None and not isinstance(planning_mode, str))
                or not isinstance(workspace_id, str)
                or not workspace_id
            ):
                raise TerminalProtocolMalformedError("frozen steer execution intent is malformed")
            items.append(
                FrozenSteerItem(
                    steer_id=_required_text(raw.get("steer_id"), "steer.steer_id"),
                    request_digest=_required_text(
                        raw.get("request_digest"),
                        "steer.request_digest",
                    ),
                    user_text=user_text,
                    revision=_positive_int(raw.get("revision"), "steer.revision"),
                    attachments=tuple(attachments),
                    planning_mode=planning_mode,
                    workspace_id=workspace_id,
                )
            )
        identity = [
            {
                "request_digest": item.request_digest,
                "revision": item.revision,
                "steer_id": item.steer_id,
            }
            for item in items
        ]
        merged_attachments: list[str] = []
        planning_mode: str | None = None
        workspace_ids = {item.workspace_id for item in items}
        if len(workspace_ids) > 1:
            raise TerminalProtocolMalformedError("frozen steer workspace changed")
        for item in items:
            for attachment in item.attachments:
                if attachment not in merged_attachments:
                    merged_attachments.append(attachment)
            if item.planning_mode is not None:
                planning_mode = item.planning_mode
        return FrozenSteerSnapshot(
            scope=scope,
            claim_epoch=claim_epoch,
            frozen_revision=_nonnegative_int(
                row["steer_frozen_revision"],
                "sessions.steer_frozen_revision",
            ),
            items=tuple(items),
            buffer_identity_digest=canonical_digest_v1(identity),
            merged_text=_merge_steer_text([item.user_text for item in items]),
            merged_attachments=tuple(merged_attachments),
            planning_mode=planning_mode,
            workspace_id=next(iter(workspace_ids), ""),
        )

    async def read_recovery_frozen_steer_snapshot(
        self,
        context: RecoveryProcessingContext,
    ) -> FrozenSteerSnapshot:
        """Resolve the recovery-owned freeze claim before reading its immutable buffer."""

        if not isinstance(context, RecoveryProcessingContext):
            raise TypeError("recovery steer context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            generation = await _read_generation_claim_row(connection, context.scope)
            recovery = await _read_active_recovery(connection, context.recovery_id)
            if not _recovery_processing_matches(generation, recovery, context, self._now()):
                raise TerminalProtocolMalformedError("recovery steer ownership changed")
            assert generation is not None
            claim_epoch = _nonnegative_int(
                generation["steer_freeze_claim_epoch"],
                "sessions.steer_freeze_claim_epoch",
            )
        return await self.read_frozen_steer_snapshot(
            context.scope,
            claim_epoch=claim_epoch,
        )

    async def read_context_maintenance_action(self, context: ExecutionGenerationContext):
        """Resolve only the original admitted source under its running owner."""
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            await require_live_execution_in_connection(connection, context=context, now=self._now())
            return await _read_context_maintenance_source(connection, turn_id=context.scope.turn_id)

    async def read_recovery_operation_kind(self, context: RecoveryProcessingContext) -> str | None:
        """Resolve maintenance identity without granting recovery publication rights."""
        if not isinstance(context, RecoveryProcessingContext):
            raise TypeError("Operation recovery requires an original capability")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            generation = await _read_generation_claim_row(connection, context.scope)
            recovery = await _read_active_recovery(connection, context.recovery_id)
            if not _recovery_processing_matches(generation, recovery, context, self._now()):
                raise TerminalProtocolMalformedError("context recovery ownership changed")
            action = await _read_context_maintenance_source(connection, turn_id=context.scope.turn_id)
            return "context_maintenance" if action is not None else None

    async def read_recovery_context_completions(self, context: RecoveryProcessingContext) -> tuple[ContextActivity, ...]:
        """Read context-only completion evidence under the original recovery fence."""
        from bowhead_core.context_activity import missing_context_completions_in_connection

        if not isinstance(context, RecoveryProcessingContext):
            raise TypeError("Context completion recovery requires an original capability")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            generation = await _read_generation_claim_row(connection, context.scope)
            recovery = await _read_active_recovery(connection, context.recovery_id)
            if not _recovery_processing_matches(generation, recovery, context, self._now()):
                raise TerminalProtocolMalformedError("context recovery ownership changed")
            return await missing_context_completions_in_connection(
                connection,
                turn_id=context.scope.turn_id,
                generation=context.scope.generation,
            )

    async def read_recovery_auth_fence_digest(
        self,
        context: RecoveryProcessingContext,
    ) -> str:
        """Read, never create, the current fixed-scope auth vector for recovery audit."""

        if not isinstance(context, RecoveryProcessingContext):
            raise TypeError("recovery auth context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            generation = await _read_generation_claim_row(connection, context.scope)
            recovery = await _read_active_recovery(connection, context.recovery_id)
            if not _recovery_processing_matches(generation, recovery, context, self._now()):
                raise TerminalProtocolMalformedError("recovery auth ownership changed")
            async with connection.execute(
                "SELECT user_id FROM sessions WHERE id=?",
                (context.scope.session_id,),
            ) as cursor:
                session = await cursor.fetchone()
            if session is None:
                raise TerminalProtocolMalformedError("recovery Session is missing")
            scopes = (
                AuthFenceScope(
                    AuthFenceScopeKind.TEAM_MEMBERSHIP,
                    context.scope.team_id,
                    context.scope.team_id,
                ),
                AuthFenceScope(
                    AuthFenceScopeKind.RESOURCE_GRANT,
                    context.scope.team_id,
                    context.scope.team_id,
                ),
                AuthFenceScope(
                    AuthFenceScopeKind.SESSION_VISIBILITY,
                    context.scope.team_id,
                    context.scope.session_id,
                ),
                AuthFenceScope(
                    AuthFenceScopeKind.USER_ACCOUNT,
                    "__global__",
                    _required_text(session["user_id"], "sessions.user_id"),
                ),
            )
            vector = await _read_auth_fence_vector(connection, scopes)
            return vector.auth_fence_vector_digest

    async def read_decision_control_outcome(
        self,
        *,
        decision_id: str,
    ) -> str | None:
        """Read the finite outcome frozen on a system-control decision."""

        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("decision control outcome identity is invalid")
        async with (
            open_terminal_protocol_connection(self._db_path) as connection,
            connection.execute(
                """
                SELECT source_kind,action,control_outcome_code
                FROM turn_control_decisions WHERE decision_id=?
                """,
                (decision_id,),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise DecisionSourceNotReadyError("control decision is missing")
        code = row[2]
        if row[0] != DecisionSourceKind.SYSTEM_CONTROL.value or row[1] != DecisionAction.REVOKE.value:
            return None
        if code not in {
            "access_revoked",
            "dependency_missing",
            "entitlement_denied",
        }:
            raise TerminalProtocolMalformedError("control decision outcome is malformed")
        return str(code)

    async def record_unknown_effect_process_event(
        self,
        context: RecoveryProcessingContext,
    ) -> TerminalProtocolEventRow:
        """Persist the manual-check event while retaining the recovery lock."""

        if not isinstance(context, RecoveryProcessingContext):
            raise TypeError("unknown-effect recovery context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                generation = await _read_generation_claim_row(
                    connection,
                    context.scope,
                )
                recovery = await _read_active_recovery(
                    connection,
                    context.recovery_id,
                )
                if not _recovery_processing_matches(
                    generation,
                    recovery,
                    context,
                    self._now(),
                ):
                    raise TerminalProtocolMalformedError("unknown-effect recovery ownership changed")
                async with connection.execute(
                    """
                    SELECT d.decision_id,d.source_id,a.attempt_id,a.state,
                           a.row_version,
                           t.event_sequence_next,g.claim_epoch,
                           g.accepted_proposal_next
                    FROM turn_generations g
                    JOIN turns t ON t.id=g.turn_id
                    JOIN turn_control_decisions d
                      ON d.decision_id=g.decision_id
                    JOIN turn_effect_attempts a
                      ON a.turn_id=g.turn_id AND a.generation=g.generation
                    WHERE g.turn_id=? AND g.generation=?
                      AND a.state IN ('intent','dispatched','unknown')
                    """,
                    (context.scope.turn_id, context.scope.generation),
                ) as cursor:
                    rows = await cursor.fetchall()
                if len(rows) != 1:
                    raise TerminalProtocolMalformedError("unknown-effect recovery projection is invalid")
                row = rows[0]
                if row["state"] != EffectState.UNKNOWN.value:
                    update = await connection.execute(
                        """
                        UPDATE turn_effect_attempts
                        SET state='unknown',row_version=row_version+1
                        WHERE attempt_id=? AND turn_id=? AND generation=?
                          AND state IN ('intent','dispatched')
                          AND row_version=?
                        """,
                        (
                            row["attempt_id"],
                            context.scope.turn_id,
                            context.scope.generation,
                            row["row_version"],
                        ),
                    )
                    if update.rowcount != 1:
                        raise TerminalProtocolMalformedError("unknown-effect attempt transition changed")
                idempotency_key = "approval_effect_unknown:" + row["decision_id"]
                stable_suffix = canonical_digest_v1(
                    {
                        "generation": context.scope.generation,
                        "idempotency_key": idempotency_key,
                        "turn_id": context.scope.turn_id,
                    }
                )[:32]
                event_id = f"evt_{stable_suffix}"
                payload = {
                    "approval_id": row["source_id"],
                    "attempt_id": row["attempt_id"],
                    "message_key": "turn.interrupted_check_actions",
                    "reason_code": "effect_outcome_unknown",
                }
                payload_json = canonical_json_v1(payload)
                payload_digest = canonical_digest_v1(payload)
                async with connection.execute(
                    "SELECT * FROM turn_events WHERE id=?",
                    (event_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    if (
                        existing["event_type"] != "interrupted_check_actions"
                        or existing["payload_json"] != payload_json
                        or existing["payload_digest"] != payload_digest
                        or existing["turn_id"] != context.scope.turn_id
                        or existing["generation"] != context.scope.generation
                        or existing["protocol_version"] != 1
                        or existing["actor_scope"] != ActorScope.ROOT.value
                        or existing["phase_scope"] != PhaseScope.IN_GENERATION.value
                        or existing["terminal_receipt_id"] is not None
                        or not isinstance(
                            existing["source_proposal_id"],
                            str,
                        )
                        or not existing["source_proposal_id"]
                        or not isinstance(existing["accept_ordinal"], int)
                        or existing["accept_ordinal"] < 1
                    ):
                        raise TerminalProtocolMalformedError("unknown-effect process event conflicts")
                    await connection.commit()
                    return TerminalProtocolEventRow(
                        event_id=event_id,
                        turn_id=context.scope.turn_id,
                        generation=context.scope.generation,
                        event_seq=_positive_int(
                            existing["event_seq"],
                            "turn_events.event_seq",
                        ),
                        event_type="interrupted_check_actions",
                        actor_scope=ActorScope.ROOT.value,
                        phase_scope=PhaseScope.IN_GENERATION.value,
                        terminal_receipt_id=None,
                        payload=payload,
                        payload_digest=payload_digest,
                        occurred_at=_required_text(
                            existing["occurred_at"],
                            "turn_events.occurred_at",
                        ),
                    )
                event_seq = _positive_int(
                    row["event_sequence_next"],
                    "turns.event_sequence_next",
                )
                async with connection.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM turn_events WHERE turn_id=?",
                    (context.scope.turn_id,),
                ) as cursor:
                    legacy_row = await cursor.fetchone()
                legacy_seq = int(legacy_row[0])
                occurred_at = _iso_utc(self._now())
                proposal_id = f"tep_{stable_suffix}"
                accept_ordinal = _positive_int(
                    row["accepted_proposal_next"],
                    "generation.accepted_proposal_next",
                )
                claim_epoch = _positive_int(
                    row["claim_epoch"],
                    "generation.claim_epoch",
                )
                await connection.execute(
                    """
                    INSERT INTO turn_event_proposals(
                      proposal_id,event_id,turn_id,generation,accept_ordinal,
                      accepted_claim_epoch,event_type,actor_scope,phase_scope,
                      payload_json,payload_digest,durability,state,accepted_at,
                      persisted_at,persisted_event_seq
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'committed','persisted',?,?,?)
                    """,
                    (
                        proposal_id,
                        event_id,
                        context.scope.turn_id,
                        context.scope.generation,
                        accept_ordinal,
                        claim_epoch,
                        "interrupted_check_actions",
                        ActorScope.ROOT.value,
                        PhaseScope.IN_GENERATION.value,
                        payload_json,
                        payload_digest,
                        occurred_at,
                        occurred_at,
                        event_seq,
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO turn_events(
                      id,turn_id,seq,event_type,payload_json,occurred_at,
                      protocol_version,generation,event_seq,actor_scope,
                      phase_scope,payload_digest,terminal_receipt_id,
                      source_proposal_id,accept_ordinal
                    ) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,NULL,?,?)
                    """,
                    (
                        event_id,
                        context.scope.turn_id,
                        legacy_seq,
                        "interrupted_check_actions",
                        payload_json,
                        occurred_at,
                        context.scope.generation,
                        event_seq,
                        ActorScope.ROOT.value,
                        PhaseScope.IN_GENERATION.value,
                        payload_digest,
                        proposal_id,
                        accept_ordinal,
                    ),
                )
                update = await connection.execute(
                    """
                    UPDATE turns
                    SET event_sequence_next=event_sequence_next+1
                    WHERE id=? AND terminal_protocol_version=1
                      AND event_sequence_next=?
                    """,
                    (context.scope.turn_id, event_seq),
                )
                if update.rowcount != 1:
                    raise TerminalProtocolMalformedError("unknown-effect event allocator changed")
                generation_update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET accepted_proposal_next=accepted_proposal_next+1
                    WHERE turn_id=? AND generation=? AND protocol_version=1
                      AND accepted_proposal_next=?
                    """,
                    (
                        context.scope.turn_id,
                        context.scope.generation,
                        accept_ordinal,
                    ),
                )
                if generation_update.rowcount != 1:
                    raise TerminalProtocolMalformedError("unknown-effect proposal allocator changed")
                await connection.commit()
                return TerminalProtocolEventRow(
                    event_id=event_id,
                    turn_id=context.scope.turn_id,
                    generation=context.scope.generation,
                    event_seq=event_seq,
                    event_type="interrupted_check_actions",
                    actor_scope=ActorScope.ROOT.value,
                    phase_scope=PhaseScope.IN_GENERATION.value,
                    terminal_receipt_id=None,
                    payload=payload,
                    payload_digest=payload_digest,
                    occurred_at=occurred_at,
                )
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def discard_recovery_effect_intents(
        self,
        context: RecoveryProcessingContext,
    ) -> int:
        """Discard only provider-unreached intents under a recovery fence."""

        if not isinstance(context, RecoveryProcessingContext):
            raise TypeError("effect-intent recovery context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                generation = await _read_generation_claim_row(
                    connection,
                    context.scope,
                )
                recovery = await _read_active_recovery(
                    connection,
                    context.recovery_id,
                )
                if not _recovery_processing_matches(
                    generation,
                    recovery,
                    context,
                    self._now(),
                ):
                    raise TerminalProtocolMalformedError("effect-intent recovery ownership changed")
                async with connection.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM turn_effect_attempts
                    WHERE turn_id=? AND generation=?
                      AND effect_class='side_effect_possible'
                      AND state='intent'
                    """,
                    (context.scope.turn_id, context.scope.generation),
                ) as cursor:
                    row = await cursor.fetchone()
                intent_count = int(row["n"]) if row is not None else 0
                if not intent_count:
                    await connection.rollback()
                    return 0
                deleted = await connection.execute(
                    """
                    DELETE FROM turn_effect_attempts
                    WHERE turn_id=? AND generation=?
                      AND effect_class='side_effect_possible' AND state='intent'
                    """,
                    (context.scope.turn_id, context.scope.generation),
                )
                if deleted.rowcount != intent_count:
                    raise TerminalProtocolMalformedError("effect-intent recovery projection changed")
                await connection.commit()
                return deleted.rowcount
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def count_recovery_inventory(self) -> int:
        """Count durable S5 work that prevents writer readiness from claiming convergence."""

        now = self._now()
        now_text = _iso_utc(now)
        opened_cutoff = _iso_utc(
            now - timedelta(seconds=TERMINAL_TASK_START_GRACE_SECONDS),
        )
        async with (
            open_terminal_protocol_connection(self._db_path) as connection,
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM turn_control_decisions
                   WHERE state='decision_recorded')
                  +
                  (SELECT COUNT(*) FROM turn_decision_effects
                   WHERE state NOT IN ('applied','skipped'))
                  +
                  (SELECT COUNT(*) FROM turn_generations g
                   JOIN turns t ON t.id=g.turn_id AND t.current_generation=g.generation
                   WHERE g.protocol_version=1 AND g.terminal_receipt_id IS NULL
                     AND g.recovery_resolution IS NULL
                     AND (
                       (g.state='opened' AND g.owner_instance_id IS NULL
                         AND g.opened_at<=?) OR
                       (g.state='running' AND (g.lease_expires_at IS NULL
                         OR g.lease_expires_at<=?)) OR
                       (g.state='closing' AND (
                         g.finalization_lease_expires_at IS NULL
                         OR g.finalization_lease_expires_at<=?)) OR
                       g.state='recovery_pending'
                     ))
                """,
                (
                    opened_cutoff,
                    now_text,
                    now_text,
                ),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def read_recovery_next_due_at(
        self,
        *,
        include_writer_lease: bool = True,
    ) -> str | None:
        """Return the earliest durable fence time that can unblock a sweep.

        The writer lease matters while acquisition is blocked.  Once a sweep
        already owns that lease, including its heartbeat would manufacture
        endless work even when every recovery inventory is empty.
        """

        if not isinstance(include_writer_lease, bool):
            raise TypeError("writer lease due selection is invalid")

        now = self._now()
        now_text = _iso_utc(now)
        opened_due_fallback = _iso_utc(
            now + timedelta(seconds=TERMINAL_TASK_START_GRACE_SECONDS),
        )
        opened_grace_modifier = f"+{TERMINAL_TASK_START_GRACE_SECONDS} seconds"
        async with (
            open_terminal_protocol_connection(self._db_path) as connection,
            connection.execute(
                """
                SELECT MIN(due_at) FROM (
                  SELECT COALESCE(recovery_next_attempt_at, ?) AS due_at
                  FROM turn_control_decisions
                  WHERE state='decision_recorded'
                  UNION ALL
                  SELECT CASE WHEN state='pending' THEN ?
                              ELSE COALESCE(claim_expires_at, ?) END AS due_at
                  FROM turn_decision_effects
                  WHERE state NOT IN ('applied','skipped')
                  UNION ALL
                  SELECT CASE
                    WHEN g.state='opened' THEN COALESCE(
                      strftime('%Y-%m-%dT%H:%M:%fZ', g.opened_at, ?), ?)
                    WHEN g.state='running' THEN COALESCE(g.lease_expires_at, ?)
                    WHEN g.state='closing' THEN COALESCE(g.finalization_lease_expires_at, ?)
                    WHEN g.state='recovery_pending' THEN COALESCE(
                      (SELECT MIN(COALESCE(rr.next_attempt_at,rr.recovery_lease_expires_at,?))
                       FROM turn_recovery_records rr
                       WHERE rr.turn_id=g.turn_id AND rr.generation=g.generation
                         AND rr.scope='terminal' AND rr.state='active'),
                      ?
                    )
                  END AS due_at
                  FROM turn_generations g
                  JOIN turns t ON t.id=g.turn_id AND t.current_generation=g.generation
                  WHERE g.protocol_version=1 AND g.terminal_receipt_id IS NULL
                    AND g.recovery_resolution IS NULL
                    AND g.state IN ('opened','running','closing','recovery_pending')
                  UNION ALL
                  SELECT lease_expires_at AS due_at
                  FROM terminal_protocol_writer_lease
                  WHERE ?=1 AND lease_name=? AND owner_instance_id IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                ) WHERE due_at IS NOT NULL
                """,
                (
                    now_text,
                    now_text,
                    now_text,
                    opened_grace_modifier,
                    opened_due_fallback,
                    now_text,
                    now_text,
                    now_text,
                    now_text,
                    1 if include_writer_lease else 0,
                    _WRITER_LEASE_NAME,
                ),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return _optional_nonempty(
            row[0] if row is not None else None,
            "recovery.next_due_at",
        )

    async def read_recovery_max_due_at(
        self,
        *,
        include_writer_lease: bool = True,
    ) -> str | None:
        """Return the maximum lease/backoff fence observed at startup."""

        if not isinstance(include_writer_lease, bool):
            raise TypeError("writer lease due selection is invalid")

        now = self._now()
        now_text = _iso_utc(now)
        opened_due_fallback = _iso_utc(
            now + timedelta(seconds=TERMINAL_TASK_START_GRACE_SECONDS),
        )
        opened_grace_modifier = f"+{TERMINAL_TASK_START_GRACE_SECONDS} seconds"
        async with (
            open_terminal_protocol_connection(self._db_path) as connection,
            connection.execute(
                """
                SELECT MAX(due_at) FROM (
                  SELECT COALESCE(recovery_next_attempt_at, ?) AS due_at
                  FROM turn_control_decisions
                  WHERE state='decision_recorded'
                  UNION ALL
                  SELECT CASE WHEN state='pending' THEN ?
                              ELSE COALESCE(claim_expires_at, ?) END
                  FROM turn_decision_effects
                  WHERE state NOT IN ('applied','skipped')
                  UNION ALL
                  SELECT CASE
                    WHEN g.state='opened' THEN COALESCE(
                      strftime('%Y-%m-%dT%H:%M:%fZ', g.opened_at, ?), ?)
                    WHEN g.state='running' THEN COALESCE(g.lease_expires_at, ?)
                    WHEN g.state='closing' THEN COALESCE(
                      g.finalization_lease_expires_at, ?)
                    WHEN g.state='recovery_pending' THEN COALESCE(
                      (SELECT MAX(COALESCE(
                         rr.next_attempt_at,
                         rr.recovery_lease_expires_at,
                         ?
                       )) FROM turn_recovery_records rr
                       WHERE rr.turn_id=g.turn_id
                         AND rr.generation=g.generation
                         AND rr.scope='terminal' AND rr.state='active'),
                      ?
                    )
                  END
                  FROM turn_generations g
                  JOIN turns t ON t.id=g.turn_id
                    AND t.current_generation=g.generation
                  WHERE g.protocol_version=1
                    AND g.terminal_receipt_id IS NULL
                    AND g.recovery_resolution IS NULL
                    AND g.state IN (
                      'opened','running','closing','recovery_pending'
                    )
                  UNION ALL
                  SELECT lease_expires_at
                  FROM terminal_protocol_writer_lease
                  WHERE ?=1 AND lease_name=? AND owner_instance_id IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                ) WHERE due_at IS NOT NULL
                """,
                (
                    now_text,
                    now_text,
                    now_text,
                    opened_grace_modifier,
                    opened_due_fallback,
                    now_text,
                    now_text,
                    now_text,
                    now_text,
                    1 if include_writer_lease else 0,
                    _WRITER_LEASE_NAME,
                ),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return _optional_nonempty(
            row[0] if row is not None else None,
            "recovery.max_due_at",
        )

    async def record_execution_usage(
        self,
        context: ExecutionGenerationContext,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_amount: float,
    ) -> None:
        """Record the generation usage projection before finalization.

        Usage does not advance ``row_version``: the execution capability still
        owns the same running generation and ``begin_finalization`` performs the
        subsequent versioned CAS.  Historical generations retain overwrite
        semantics while running.  Contract-v2 freezes through the dedicated
        billing seam; after that only an exact aggregate replay is accepted.
        """

        for value in (input_tokens, output_tokens):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("terminal execution token usage is invalid")
        if (
            not isinstance(cost_amount, (int, float))
            or isinstance(cost_amount, bool)
            or float(cost_amount) < 0
            or float(cost_amount) != float(cost_amount)
            or float(cost_amount) == float("inf")
        ):
            raise ValueError("terminal execution cost usage is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None or not _execution_context_matches(row, context, self._now()):
                    raise TerminalProtocolMalformedError("execution generation ownership fence changed")
                contract = resolve_effect_execution_contract(
                    _parse_payload(row["execution_intent_json"]),
                )
                if contract.disposition is EffectIntentDisposition.RECOVERY_ONLY and row["usage_digest"] is not None:
                    frozen = _local_billing_fact_from_generation(
                        context.scope,
                        row,
                    )
                    if (
                        frozen.input_tokens != input_tokens
                        or frozen.output_tokens != output_tokens
                        or frozen.cost_amount != f"{float(cost_amount):.12f}"
                        or row["usage_digest"] != frozen.usage_digest
                    ):
                        raise TerminalProtocolMalformedError(
                            "contract-v2 execution usage is already frozen",
                        )
                    await connection.rollback()
                    return
                model_usage_json = None
                if contract.billing_rate_card_version == "v2":
                    from bowhead_core.physical_usage import turn_model_usage_lines

                    lines = await turn_model_usage_lines(
                        connection,
                        turn_id=context.scope.turn_id,
                        generation=context.scope.generation,
                        projected_input=input_tokens,
                        projected_output=output_tokens,
                    )
                    model_usage_json = canonical_json_v1([line.as_json() for line in lines])
                update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET input_tokens=?,output_tokens=?,cost_amount=?,billing_usage_json=?
                    WHERE turn_id=? AND generation=? AND protocol_version=1
                      AND state='running' AND owner_instance_id=? AND claim_epoch=?
                      AND terminal_candidate_id IS NULL AND terminal_receipt_id IS NULL
                    """,
                    (
                        input_tokens,
                        output_tokens,
                        f"{float(cost_amount):.12f}",
                        model_usage_json,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.owner_instance_id,
                        context.claim_epoch,
                    ),
                )
                if update.rowcount != 1:
                    raise TerminalProtocolMalformedError("terminal execution usage fence changed")
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def verify_execution_usage(
        self,
        context: ExecutionGenerationContext,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_amount: float,
    ) -> None:
        """Read-only exact aggregate check used by contract-v2 finalization."""

        for value in (input_tokens, output_tokens):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("terminal execution token usage is invalid")
        if (
            not isinstance(cost_amount, (int, float))
            or isinstance(cost_amount, bool)
            or float(cost_amount) < 0
            or float(cost_amount) != float(cost_amount)
            or float(cost_amount) == float("inf")
        ):
            raise ValueError("terminal execution cost usage is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            row = await _read_generation_claim_row(connection, context.scope)
            if row is None or not _execution_context_matches(row, context, self._now()):
                raise TerminalProtocolMalformedError("execution generation ownership fence changed")
            contract = resolve_effect_execution_contract(
                _parse_payload(row["execution_intent_json"]),
            )
            if contract.disposition is not EffectIntentDisposition.RECOVERY_ONLY:
                raise TerminalProtocolMalformedError("execution usage verification requires contract-v2")
            if (
                _nonnegative_int(row["input_tokens"], "execution usage input") != input_tokens
                or _nonnegative_int(row["output_tokens"], "execution usage output") != output_tokens
                or f"{_nonnegative_float(row['cost_amount'], 'execution usage cost'):.12f}" != f"{float(cost_amount):.12f}"
            ):
                raise TerminalProtocolMalformedError("contract-v2 execution usage aggregate differs")

    async def freeze_usage_and_claim_billing_attempt(
        self,
        context: ExecutionGenerationContext | RecoveryProcessingContext,
    ) -> EffectAttemptContext | None:
        """Freeze persisted usage and claim the one deterministic local charge."""

        if not isinstance(
            context,
            (ExecutionGenerationContext, RecoveryProcessingContext),
        ):
            raise TypeError("local billing capability is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                generation = await _read_generation_claim_row(
                    connection,
                    context.scope,
                )
                await _require_local_billing_capability_in_tx(
                    connection,
                    context=context,
                    generation=generation,
                    now=self._now(),
                )
                assert generation is not None
                result = await _freeze_usage_and_claim_billing_in_tx(
                    connection,
                    generation=generation,
                    scope=context.scope,
                    now=self._now(),
                    inject=self._inject,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def record_local_billing_settlement(
        self,
        context: ExecutionGenerationContext | RecoveryProcessingContext,
        attempt: EffectAttemptContext,
    ) -> EffectAttemptContext:
        """Atomically settle stable credit ledger truth under a typed owner fence."""

        if not isinstance(
            context,
            (ExecutionGenerationContext, RecoveryProcessingContext),
        ) or not isinstance(attempt, EffectAttemptContext):
            raise TypeError("local billing settlement capability is invalid")
        if attempt.scope != context.scope:
            raise ValueError("local billing settlement scope mismatch")
        self._inject(TerminalProtocolFaultPoint.EFFECT_BEFORE_SETTLE)
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                generation = await _read_generation_claim_row(
                    connection,
                    context.scope,
                )
                await _require_local_billing_capability_in_tx(
                    connection,
                    context=context,
                    generation=generation,
                    now=self._now(),
                )
                assert generation is not None
                result = await _settle_local_billing_in_tx(
                    connection,
                    generation=generation,
                    attempt=attempt,
                    now=self._now(),
                    inject=self._inject,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(
            TerminalProtocolFaultPoint.LOCAL_BILLING_AFTER_COMMIT_BEFORE_ACK,
        )
        return result

    async def accept_event(
        self,
        context: ExecutionGenerationContext,
        *,
        event_type: str,
        actor_scope: ActorScope,
        phase_scope: PhaseScope,
        payload: Mapping[str, object],
        publish_mode: EventPublishMode = EventPublishMode.NORMAL,
        root_durability: RootDurability = RootDurability.COMMITTED,
        idempotency_key: str | None = None,
    ) -> AcceptedEventProposal:
        """先 durable accept，再让 runtime writer 排队 materialize。

        ``publish_mode`` 只决定 10ms batch 或立即 flush；proposal 的 ``durability``
        列记录 root event 可信性。二者故意使用不同 enum，不能互相转换。
        """

        _validate_event_accept_input(
            event_type=event_type,
            actor_scope=actor_scope,
            phase_scope=phase_scope,
            publish_mode=publish_mode,
            root_durability=root_durability,
            payload=payload,
        )
        payload_copy = _canonical_mapping_copy(payload)
        payload_json = canonical_json_v1(payload_copy)
        payload_digest = canonical_digest_v1(payload_copy)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValueError("event idempotency key is invalid")
        result: AcceptedEventProposal
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                now_text = _iso_utc(now)
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None or not _execution_context_matches(row, context, now):
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution generation ownership fence changed")
                if row["execution_started_at"] is None:
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution generation has not started")
                if row["terminal_candidate_id"] is not None:
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution generation event acceptance is sealed")
                if not await _writer_lease_matches_execution(connection, context=context, now=now):
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution writer lease is unavailable")
                result, inserted = await _insert_or_replay_event_proposal_in_tx(
                    connection,
                    context=context,
                    generation_row=row,
                    event_type=event_type,
                    actor_scope=actor_scope,
                    phase_scope=phase_scope,
                    payload_json=payload_json,
                    payload_digest=payload_digest,
                    publish_mode=publish_mode,
                    root_durability=root_durability,
                    idempotency_key=idempotency_key,
                    accepted_at=now_text,
                )
                if not inserted:
                    await connection.rollback()
                    return result
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.EVENT_AFTER_DURABLE_ACCEPT)
        return result

    async def read_execution_accepted_highwater(
        self,
        context: ExecutionGenerationContext,
    ) -> int:
        """Read the started generation's accepted prefix under exact writer fences."""

        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            try:
                now = self._now()
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None or not _execution_context_matches(row, context, now):
                    raise TerminalProtocolMalformedError(
                        "execution generation ownership fence changed",
                    )
                if row["execution_started_at"] is None:
                    raise TerminalProtocolMalformedError(
                        "execution generation has not started",
                    )
                if row["terminal_candidate_id"] is not None or row["terminal_receipt_id"] is not None or row["closing_accepted_through"] is not None:
                    raise TerminalProtocolMalformedError(
                        "execution generation event materialization is sealed",
                    )
                if not await _writer_lease_matches_execution(
                    connection,
                    context=context,
                    now=now,
                ):
                    raise TerminalProtocolMalformedError(
                        "execution writer lease is unavailable",
                    )
                return (
                    _positive_int(
                        row["accepted_proposal_next"],
                        "generation.accepted_proposal_next",
                    )
                    - 1
                )
            finally:
                if connection.in_transaction:
                    await connection.rollback()

    async def flush_through(
        self,
        context: ExecutionGenerationContext,
        *,
        accepted_through: int,
    ) -> tuple[EventTicket, ...]:
        """materialize 当前 claim 的 durable proposal prefix，并返回稳定 tickets。

        S2 只接受 running execution owner；S3 takeover/finalizer 会使用自己的 typed
        context 增加另一条授权路径，不会伪造 execution claim。
        """

        if not isinstance(accepted_through, int) or isinstance(accepted_through, bool) or accepted_through < 0:
            raise ValueError("accepted_through must be a non-negative integer")
        if accepted_through == 0:
            return ()
        result: tuple[EventTicket, ...]
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None or not _execution_context_matches(row, context, now):
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution generation ownership fence changed")
                if row["execution_started_at"] is None:
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution generation has not started")
                if row["terminal_candidate_id"] is not None:
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution generation event materialization is sealed")
                if not await _writer_lease_matches_execution(connection, context=context, now=now):
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("execution writer lease is unavailable")
                accepted_next = _positive_int(
                    row["accepted_proposal_next"],
                    "generation.accepted_proposal_next",
                )
                if accepted_through >= accepted_next:
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("accepted event fence exceeds durable allocator")
                proposals = await _read_event_proposal_prefix(
                    connection,
                    scope=context.scope,
                    accepted_through=accepted_through,
                )
                _validate_event_proposal_prefix(
                    proposals,
                    context=context,
                    accepted_through=accepted_through,
                )
                self._inject(TerminalProtocolFaultPoint.EVENT_BEFORE_PERSIST)
                result = await _materialize_event_proposal_prefix(
                    connection,
                    context=context,
                    proposals=proposals,
                    event_sequence_next=_positive_int(
                        row["event_sequence_next"],
                        "turns.event_sequence_next",
                    ),
                    persisted_at=_iso_utc(now),
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.EVENT_AFTER_PERSIST)
        return result

    async def append_post_terminal_auxiliary(
        self,
        context: PostTerminalAuxiliaryContext,
        *,
        resolve_context: AuthFenceResolveContext,
        expected_auth_fence_vector: AuthFenceVector,
        event_type: str,
        actor_scope: ActorScope,
        payload: Mapping[str, object],
    ) -> TerminalProtocolEventRow:
        """在已提交 receipt 后幂等 append auxiliary，不改写 root pointer。"""

        if not isinstance(context, PostTerminalAuxiliaryContext):
            raise TypeError("post-terminal auxiliary context is invalid")
        if not isinstance(resolve_context, AuthFenceResolveContext) or not isinstance(
            expected_auth_fence_vector,
            AuthFenceVector,
        ):
            raise TypeError("post-terminal auxiliary auth fence is invalid")
        if (
            context.scope.team_id != resolve_context.team_id
            or context.scope.session_id != resolve_context.session_id
            or context.scope.turn_id != resolve_context.resource_ref
        ):
            raise ValueError("post-terminal auxiliary auth identity mismatch")
        if context.auth_fence_vector_digest != expected_auth_fence_vector.auth_fence_vector_digest:
            raise ValueError("post-terminal auxiliary auth digest mismatch")
        if not isinstance(event_type, str) or not event_type or len(event_type) > 128:
            raise ValueError("post-terminal auxiliary event type is invalid")
        if event_type in {"done", "final_answer", "error"}:
            raise ValueError("post-terminal auxiliary cannot carry root terminal semantics")
        if actor_scope not in {ActorScope.ROOT, ActorScope.TRANSPORT}:
            raise ValueError("post-terminal auxiliary actor scope is invalid")
        if not isinstance(payload, Mapping):
            raise TypeError("post-terminal auxiliary payload must be a mapping")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                current_auth_fence_vector = await _read_auth_fence_vector(
                    connection,
                    resolve_context.required_scopes,
                )
                if current_auth_fence_vector != expected_auth_fence_vector:
                    await connection.rollback()
                    raise TerminalProtocolMalformedError("post-terminal auxiliary auth fence changed")
                result = await _append_post_terminal_auxiliary_in_tx(
                    connection,
                    context=context,
                    event_type=event_type,
                    actor_scope=actor_scope,
                    payload=payload,
                    occurred_at=_iso_utc(self._now()),
                )
                await connection.commit()
                return result
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def begin_effect_attempt(
        self,
        context: ExecutionGenerationContext,
        *,
        attempt_id: str,
        effect_class: EffectClass,
        provider: str,
        action_ref: str,
        idempotency_or_fence_token: str | None,
        non_retriable: bool,
    ) -> EffectAttemptContext:
        """dispatch 前 durable 写 effect intent，并重验 execution/Session/writer fence。"""

        if not isinstance(context, ExecutionGenerationContext):
            raise TypeError("execution generation context is invalid")
        if not isinstance(effect_class, EffectClass):
            raise TypeError("effect class is invalid")
        if any(not isinstance(value, str) or not value for value in (attempt_id, provider, action_ref)):
            raise ValueError("effect attempt identity is invalid")
        if idempotency_or_fence_token is not None and (not isinstance(idempotency_or_fence_token, str) or not idempotency_or_fence_token):
            raise ValueError("effect provider token is invalid")
        if idempotency_or_fence_token is None and not non_retriable:
            raise ValueError("effect without provider token must be non-retriable")
        result: EffectAttemptContext
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                generation = await _read_generation_claim_row(connection, context.scope)
                if generation is None or not _execution_context_matches(generation, context, now):
                    raise TerminalProtocolMalformedError("effect execution ownership fence changed")
                if not await _writer_lease_matches_execution(connection, context=context, now=now):
                    raise TerminalProtocolMalformedError("effect writer lease is unavailable")
                async with connection.execute(
                    """
                    SELECT * FROM turn_effect_attempts
                    WHERE turn_id=? AND generation=? AND action_ref=?
                    """,
                    (context.scope.turn_id, context.scope.generation, action_ref),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    result = _effect_attempt_from_row(context.scope, existing)
                    if (
                        result.attempt_id != attempt_id
                        or result.provider != provider
                        or result.effect_class is not effect_class
                        or result.claim_epoch != context.claim_epoch
                        or result.idempotency_or_fence_token != idempotency_or_fence_token
                        or result.non_retriable is not non_retriable
                    ):
                        raise TerminalProtocolMalformedError("effect attempt identity conflict")
                    if result.state is not EffectState.INTENT:
                        await connection.rollback()
                        return result
                _require_effect_dispatch_before_usage_freeze(generation)
                if await _has_effect_unknown_recovery_latch(connection, context.scope):
                    raise TerminalProtocolMalformedError("effect generation is poisoned by UNKNOWN recovery")
                try:
                    await _validate_effect_dispatch_auth(
                        connection,
                        context,
                    )
                except DecisionAccessRevokedError:
                    if existing is not None and existing["state"] == EffectState.INTENT.value:
                        deleted = await connection.execute(
                            """
                            DELETE FROM turn_effect_attempts
                            WHERE attempt_id=? AND turn_id=? AND generation=?
                              AND claim_epoch=? AND state='intent' AND row_version=?
                            """,
                            (
                                existing["attempt_id"],
                                context.scope.turn_id,
                                context.scope.generation,
                                context.claim_epoch,
                                existing["row_version"],
                            ),
                        )
                        if deleted.rowcount != 1:
                            raise TerminalProtocolMalformedError("effect pre-dispatch denial fence changed")
                        await connection.commit()
                    raise
                if existing is not None:
                    await connection.rollback()
                    return result
                await connection.execute(
                    """
                    INSERT INTO turn_effect_attempts(
                      attempt_id,turn_id,generation,claim_epoch,effect_class,provider,
                      action_ref,idempotency_or_fence_token,non_retriable,state,row_version,started_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,'intent',1,?)
                    """,
                    (
                        attempt_id,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.claim_epoch,
                        effect_class.value,
                        provider,
                        action_ref,
                        idempotency_or_fence_token,
                        1 if non_retriable else 0,
                        _iso_utc(now),
                    ),
                )
                result = EffectAttemptContext(
                    scope=context.scope,
                    attempt_id=attempt_id,
                    action_ref=action_ref,
                    provider=provider,
                    effect_class=effect_class,
                    state=EffectState.INTENT,
                    claim_epoch=context.claim_epoch,
                    row_version=1,
                    idempotency_or_fence_token=idempotency_or_fence_token,
                    non_retriable=non_retriable,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.EFFECT_AFTER_INTENT)
        return result

    async def read_effect_attempt(
        self,
        scope: TerminalScope,
        *,
        action_ref: str,
    ) -> EffectAttemptContext | None:
        """Read durable provider-boundary truth before replay authorization."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("effect attempt scope is invalid")
        if not isinstance(action_ref, str) or not action_ref:
            raise ValueError("effect attempt action identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT * FROM turn_effect_attempts
                WHERE turn_id=? AND generation=? AND action_ref=?
                """,
                (scope.turn_id, scope.generation, action_ref),
            ) as cursor:
                row = await cursor.fetchone()
        return _effect_attempt_from_row(scope, row) if row is not None else None

    async def discard_effect_intent(
        self,
        context: ExecutionGenerationContext,
        attempt: EffectAttemptContext,
    ) -> bool:
        """Discard an exact pre-provider intent after semantic denial.

        This transition is intentionally unavailable after the dispatch CAS:
        DISPATCHED/UNKNOWN/SETTLED rows are durable provider-boundary truth and
        must be projected or recovered even if authorization later changes.
        """

        if not isinstance(context, ExecutionGenerationContext) or not isinstance(attempt, EffectAttemptContext):
            raise TypeError("effect intent discard context is invalid")
        if attempt.scope != context.scope or attempt.claim_epoch != context.claim_epoch or attempt.state is not EffectState.INTENT:
            raise ValueError("effect intent discard identity mismatch")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                generation = await _read_generation_claim_row(
                    connection,
                    context.scope,
                )
                if generation is None or not _execution_context_matches(
                    generation,
                    context,
                    now,
                ):
                    raise TerminalProtocolMalformedError("effect intent discard ownership fence changed")
                if not await _writer_lease_matches_execution(
                    connection,
                    context=context,
                    now=now,
                ):
                    raise TerminalProtocolMalformedError("effect intent discard writer lease is unavailable")
                deleted = await connection.execute(
                    """
                    DELETE FROM turn_effect_attempts
                    WHERE attempt_id=? AND turn_id=? AND generation=?
                      AND claim_epoch=? AND state='intent' AND row_version=?
                    """,
                    (
                        attempt.attempt_id,
                        attempt.scope.turn_id,
                        attempt.scope.generation,
                        attempt.claim_epoch,
                        attempt.row_version,
                    ),
                )
                if deleted.rowcount == 1:
                    await connection.commit()
                    return True
                async with connection.execute(
                    "SELECT state FROM turn_effect_attempts WHERE attempt_id=?",
                    (attempt.attempt_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is None:
                    await connection.rollback()
                    return False
                raise TerminalProtocolMalformedError("effect intent crossed provider boundary before denial")
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def mark_effect_dispatched(
        self,
        context: ExecutionGenerationContext,
        attempt: EffectAttemptContext,
    ) -> EffectAttemptContext:
        """网络边界前最后一次 claim fence；CAS commit 后 caller 才能 dispatch。"""

        return (await self.claim_effect_dispatch(context, attempt)).attempt

    async def claim_effect_dispatch(
        self,
        context: ExecutionGenerationContext,
        attempt: EffectAttemptContext,
    ) -> EffectDispatchClaim:
        """Return the attempt plus an unambiguous single provider-call winner."""

        if not isinstance(context, ExecutionGenerationContext) or not isinstance(attempt, EffectAttemptContext):
            raise TypeError("effect dispatch context is invalid")
        if attempt.scope != context.scope or attempt.claim_epoch != context.claim_epoch:
            raise ValueError("effect dispatch identity mismatch")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                generation = await _read_generation_claim_row(connection, context.scope)
                if generation is None or not _execution_context_matches(generation, context, now):
                    raise TerminalProtocolMalformedError("effect dispatch ownership fence changed")
                if not await _writer_lease_matches_execution(connection, context=context, now=now):
                    raise TerminalProtocolMalformedError("effect dispatch writer lease is unavailable")
                async with connection.execute(
                    "SELECT * FROM turn_effect_attempts WHERE attempt_id=?",
                    (attempt.attempt_id,),
                ) as cursor:
                    attempt_row = await cursor.fetchone()
                if attempt_row is None:
                    raise TerminalProtocolMalformedError("effect attempt is missing")
                current_attempt = _effect_attempt_from_row(attempt.scope, attempt_row)
                if not _same_effect_attempt_identity(current_attempt, attempt):
                    raise TerminalProtocolMalformedError("effect attempt identity changed")
                if current_attempt.state is not EffectState.INTENT:
                    if current_attempt.state not in {
                        EffectState.DISPATCHED,
                        EffectState.UNKNOWN,
                        EffectState.SETTLED,
                    }:
                        raise TerminalProtocolMalformedError("effect dispatch state changed")
                    await connection.rollback()
                    return EffectDispatchClaim(
                        attempt=current_attempt,
                        dispatched_here=False,
                    )
                _require_effect_dispatch_before_usage_freeze(generation)
                if await _has_effect_unknown_recovery_latch(connection, context.scope):
                    raise TerminalProtocolMalformedError("effect generation is poisoned by UNKNOWN recovery")
                try:
                    await _validate_effect_dispatch_auth(
                        connection,
                        context,
                    )
                except DecisionAccessRevokedError:
                    # The provider boundary was never crossed. Remove the
                    # exact INTENT winner in the same transaction so recovery
                    # cannot misclassify a proven-not-applied action as
                    # UNKNOWN/manual-check work.
                    deleted = await connection.execute(
                        """
                        DELETE FROM turn_effect_attempts
                        WHERE attempt_id=? AND turn_id=? AND generation=?
                          AND claim_epoch=? AND state='intent' AND row_version=?
                        """,
                        (
                            attempt.attempt_id,
                            attempt.scope.turn_id,
                            attempt.scope.generation,
                            attempt.claim_epoch,
                            attempt.row_version,
                        ),
                    )
                    if deleted.rowcount != 1:
                        raise TerminalProtocolMalformedError("effect pre-dispatch denial fence changed")
                    await connection.commit()
                    raise
                update = await connection.execute(
                    """
                    UPDATE turn_effect_attempts SET state='dispatched',row_version=row_version+1
                    WHERE attempt_id=? AND turn_id=? AND generation=? AND claim_epoch=?
                      AND state='intent' AND row_version=?
                    """,
                    (
                        attempt.attempt_id,
                        attempt.scope.turn_id,
                        attempt.scope.generation,
                        attempt.claim_epoch,
                        attempt.row_version,
                    ),
                )
                if update.rowcount != 1:
                    async with connection.execute(
                        "SELECT * FROM turn_effect_attempts WHERE attempt_id=?",
                        (attempt.attempt_id,),
                    ) as cursor:
                        replay = await cursor.fetchone()
                    if replay is None:
                        raise TerminalProtocolMalformedError("effect attempt is missing")
                    result = _effect_attempt_from_row(attempt.scope, replay)
                    if not _same_effect_attempt_identity(result, attempt) or result.state not in {
                        EffectState.DISPATCHED,
                        EffectState.UNKNOWN,
                        EffectState.SETTLED,
                    }:
                        raise TerminalProtocolMalformedError("effect dispatch CAS changed")
                    await connection.rollback()
                    return EffectDispatchClaim(
                        attempt=result,
                        dispatched_here=False,
                    )
                result = replace(
                    attempt,
                    state=EffectState.DISPATCHED,
                    row_version=attempt.row_version + 1,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.EFFECT_AFTER_DISPATCH)
        return EffectDispatchClaim(
            attempt=result,
            dispatched_here=True,
        )

    async def mark_effect_unknown(self, attempt: EffectAttemptContext) -> EffectAttemptContext:
        """provider outcome不可确认时只标 unknown，不授权任何 retry。"""

        if not isinstance(attempt, EffectAttemptContext) or attempt.state is not EffectState.DISPATCHED:
            raise TypeError("effect unknown transition requires dispatched attempt")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                update = await connection.execute(
                    """
                    UPDATE turn_effect_attempts SET state='unknown',row_version=row_version+1
                    WHERE attempt_id=? AND turn_id=? AND generation=? AND claim_epoch=?
                      AND state='dispatched' AND row_version=?
                    """,
                    (
                        attempt.attempt_id,
                        attempt.scope.turn_id,
                        attempt.scope.generation,
                        attempt.claim_epoch,
                        attempt.row_version,
                    ),
                )
                if update.rowcount == 1:
                    result = replace(
                        attempt,
                        state=EffectState.UNKNOWN,
                        row_version=attempt.row_version + 1,
                    )
                    await connection.commit()
                else:
                    async with connection.execute(
                        "SELECT * FROM turn_effect_attempts WHERE attempt_id=?",
                        (attempt.attempt_id,),
                    ) as cursor:
                        replay = await cursor.fetchone()
                    if replay is None:
                        raise TerminalProtocolMalformedError("effect attempt is missing")
                    result = _effect_attempt_from_row(attempt.scope, replay)
                    if not _same_effect_attempt_identity(result, attempt) or result.state not in {
                        EffectState.UNKNOWN,
                        EffectState.SETTLED,
                    }:
                        raise TerminalProtocolMalformedError("effect unknown CAS changed")
                    await connection.rollback()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def mark_effect_unknown_and_discover_recovery(
        self,
        context: ExecutionGenerationContext,
        attempt: EffectAttemptContext,
        *,
        scanner_id: str,
    ) -> EffectUnknownRecoveryTransition:
        """Atomically poison one dispatched attempt and its whole generation."""

        if not isinstance(context, ExecutionGenerationContext) or not isinstance(
            attempt,
            EffectAttemptContext,
        ):
            raise TypeError("effect UNKNOWN recovery context is invalid")
        if attempt.scope != context.scope or attempt.claim_epoch != context.claim_epoch or attempt.state is not EffectState.DISPATCHED:
            raise ValueError("effect UNKNOWN recovery identity mismatch")
        if not isinstance(scanner_id, str) or not scanner_id:
            raise ValueError("effect UNKNOWN recovery scanner identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                generation = await _read_generation_claim_row(
                    connection,
                    context.scope,
                )
                if generation is None:
                    raise TerminalProtocolMalformedError("effect UNKNOWN generation is missing")
                async with connection.execute(
                    "SELECT * FROM turn_effect_attempts WHERE attempt_id=?",
                    (attempt.attempt_id,),
                ) as cursor:
                    attempt_row = await cursor.fetchone()
                if attempt_row is None:
                    raise TerminalProtocolMalformedError("effect UNKNOWN attempt is missing")
                current_attempt = _effect_attempt_from_row(context.scope, attempt_row)
                if not _same_effect_attempt_identity(current_attempt, attempt):
                    raise TerminalProtocolMalformedError("effect UNKNOWN attempt identity changed")
                active = await _read_active_generation_recovery(
                    connection,
                    context.scope,
                )
                if (
                    generation["state"] == GenerationState.RECOVERY_PENDING
                    and active is not None
                    and active["kind"] == RecoveryKind.EFFECT_UNKNOWN.value
                    and current_attempt.state
                    in {
                        EffectState.UNKNOWN,
                        EffectState.SETTLED,
                    }
                ):
                    result = EffectUnknownRecoveryTransition(
                        attempt=current_attempt,
                        recovery=_recovery_record_from_row(
                            context.scope,
                            active,
                            generation,
                            created=False,
                        ),
                    )
                    await connection.rollback()
                    return result
                now = self._now()
                if not _execution_context_matches(generation, context, now):
                    raise TerminalProtocolMalformedError("effect UNKNOWN execution fence changed")
                if not await _writer_lease_matches_execution(
                    connection,
                    context=context,
                    now=now,
                ):
                    raise TerminalProtocolMalformedError("effect UNKNOWN writer lease is unavailable")
                if current_attempt.state is not EffectState.DISPATCHED or current_attempt.row_version != attempt.row_version:
                    raise TerminalProtocolMalformedError("effect UNKNOWN attempt fence changed")
                update = await connection.execute(
                    """
                    UPDATE turn_effect_attempts
                    SET state='unknown',row_version=row_version+1
                    WHERE attempt_id=? AND turn_id=? AND generation=? AND claim_epoch=?
                      AND state='dispatched' AND row_version=?
                    """,
                    (
                        attempt.attempt_id,
                        attempt.scope.turn_id,
                        attempt.scope.generation,
                        attempt.claim_epoch,
                        attempt.row_version,
                    ),
                )
                if update.rowcount != 1:
                    raise TerminalProtocolMalformedError("effect UNKNOWN attempt CAS changed")
                unknown = replace(
                    attempt,
                    state=EffectState.UNKNOWN,
                    row_version=attempt.row_version + 1,
                )
                self._inject(
                    TerminalProtocolFaultPoint.EFFECT_UNKNOWN_AFTER_ATTEMPT_CAS,
                )
                discovery = RecoveryDiscoveryContext(
                    scope=context.scope,
                    observed_state=GenerationState.RUNNING,
                    observed_generation_row_version=context.generation_row_version,
                    observed_execution_claim_epoch=context.claim_epoch,
                    observed_finalization_epoch=_nonnegative_int(
                        generation["finalization_epoch"],
                        "effect UNKNOWN finalization epoch",
                    ),
                    scanner_id=scanner_id,
                )
                recovery = await _discover_recovery_in_tx(
                    connection,
                    context=discovery,
                    generation=generation,
                    now=now,
                    inject=self._inject,
                )
                if recovery.kind is not RecoveryKind.EFFECT_UNKNOWN:
                    raise TerminalProtocolMalformedError("effect UNKNOWN recovery kind changed")
                await connection.commit()
                return EffectUnknownRecoveryTransition(
                    attempt=unknown,
                    recovery=recovery,
                )
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def read_effect_disclosure_snapshot(
        self,
        scope: TerminalScope,
    ) -> EffectDisclosureSnapshot | None:
        """Return a content-free, digest-bound snapshot for frozen recovery disclosure."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("effect disclosure scope is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            try:
                generation = await _read_generation_claim_row(connection, scope)
                if generation is None:
                    raise TerminalProtocolMalformedError("effect disclosure generation is missing")
                result = await _read_effect_disclosure_snapshot(connection, scope)
                await connection.rollback()
                return result
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def read_effect_execution_contract(
        self,
        scope: TerminalScope,
    ) -> EffectExecutionContract:
        """Resolve persisted generation intent; contract-v2 stays recovery-only."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("effect execution contract scope is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            generation = await _read_generation_claim_row(connection, scope)
            if generation is None:
                raise TerminalProtocolMalformedError("effect execution contract generation is missing")
            intent = _parse_payload(generation["execution_intent_json"])
            return resolve_effect_execution_contract(intent)

    async def read_current_effect_disclosure(
        self,
        scope: TerminalScope,
    ) -> EffectDisclosureOverlay | None:
        """Read the content-free v2 overlay without rewriting its root receipt."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("effect disclosure overlay scope is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            try:
                generation = await _read_generation_claim_row(connection, scope)
                if generation is None:
                    raise TerminalProtocolMalformedError("effect disclosure overlay generation is missing")
                intent = _parse_payload(generation["execution_intent_json"])
                contract = resolve_effect_execution_contract(intent)
                if contract.disposition is EffectIntentDisposition.LEGACY:
                    await connection.rollback()
                    return None
                snapshot = await _read_effect_disclosure_snapshot_v2(
                    connection,
                    scope,
                )
                async with connection.execute(
                    """
                    SELECT r.receipt_id,r.status,c.projection_extensions_json
                    FROM turn_terminal_receipts r
                    JOIN turn_terminal_candidates c ON c.candidate_id=r.candidate_id
                    WHERE r.turn_id=? AND r.generation=?
                    """,
                    (scope.turn_id, scope.generation),
                ) as cursor:
                    receipt = await cursor.fetchone()
                if receipt is None:
                    result = await _effect_overlay_from_snapshot(
                        connection,
                        snapshot=snapshot,
                        receipt_id=None,
                        finalized=False,
                        now=self._now(),
                    )
                    await connection.rollback()
                    return result
                receipt_id = _required_text(
                    receipt["receipt_id"],
                    "effect disclosure receipt",
                )
                frozen = _effect_disclosure_snapshot_from_extensions(
                    scope,
                    receipt["projection_extensions_json"],
                )
                if frozen is None:
                    raise TerminalProtocolMalformedError("contract-v2 receipt is missing its effect disclosure manifest")
                if receipt["status"] != TerminalStatus.CANCELLED.value and frozen.unsettled_count:
                    raise TerminalProtocolMalformedError("non-cancelled receipt carries unsettled contract-v2 effects")
                if not snapshot.attempts and frozen.attempts:
                    await _require_aggregated_effect_tombstone(
                        connection,
                        frozen,
                    )
                    if frozen.unsettled_count:
                        result = await _read_final_effect_overlay_event(
                            connection,
                            scope=scope,
                            receipt_id=receipt_id,
                            frozen=frozen,
                        )
                    else:
                        result = await _effect_overlay_from_snapshot(
                            connection,
                            snapshot=frozen,
                            receipt_id=receipt_id,
                            finalized=True,
                            now=self._now(),
                        )
                    await connection.rollback()
                    return result
                _validate_effect_disclosure_monotonic(frozen, snapshot)
                requires_final_event = bool(
                    receipt["status"] == TerminalStatus.CANCELLED.value and frozen.unsettled_count and not snapshot.unsettled_count
                )
                if requires_final_event:
                    event_overlay = await _read_final_effect_overlay_event(
                        connection,
                        scope=scope,
                        receipt_id=receipt_id,
                        frozen=frozen,
                    )
                    _validate_final_effect_overlay_snapshot(
                        event_overlay,
                        snapshot,
                        error="effect disclosure final event proof changed",
                    )
                result = await _effect_overlay_from_snapshot(
                    connection,
                    snapshot=snapshot,
                    receipt_id=receipt_id,
                    finalized=not snapshot.unsettled_count,
                    now=self._now(),
                )
                await connection.rollback()
                return result
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def read_committed_followup_template(
        self,
        receipt: CommittedReceipt,
    ) -> dict[str, object] | None:
        """Rehydrate a certified child solely from the committed ledger."""

        if not isinstance(receipt, CommittedReceipt):
            raise TypeError("follow-up receipt identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            try:
                async with connection.execute(
                    """
                    SELECT c.turn_id,c.generation,t.session_id,w.team_id
                    FROM turn_terminal_candidates c
                    JOIN turns t ON t.id=c.turn_id
                    JOIN sessions s ON s.id=t.session_id
                    JOIN workspaces w ON w.id=s.workspace_id
                    WHERE c.candidate_id=?
                    """,
                    (receipt.candidate_id,),
                ) as cursor:
                    source = await cursor.fetchone()
                if source is None:
                    raise TerminalProtocolMalformedError("follow-up candidate source is missing")
                scope = TerminalScope(
                    team_id=_required_text(
                        source["team_id"],
                        "followup.team_id",
                    ),
                    session_id=_required_text(
                        source["session_id"],
                        "followup.session_id",
                    ),
                    turn_id=_required_text(
                        source["turn_id"],
                        "followup.source_turn_id",
                    ),
                    generation=_positive_int(
                        source["generation"],
                        "followup.source_generation",
                    ),
                )
                durable = await _read_committed_receipt(
                    connection,
                    candidate_id=receipt.candidate_id,
                    candidate_projection_digest=(receipt.candidate_projection_digest),
                )
                if durable != receipt:
                    raise TerminalProtocolMalformedError("follow-up receipt identity changed")
                if durable.selected_session_variant is not SessionVariant.FOLLOWUP:
                    await connection.rollback()
                    return None
                candidate = await _load_terminal_candidate(
                    connection,
                    scope,
                    receipt.candidate_id,
                )
                selected = next(
                    (variant for variant in candidate.variants if variant.variant_key == receipt.selected_variant_key),
                    None,
                )
                if selected is None:
                    raise TerminalProtocolMalformedError("follow-up selected variant is missing")
                raw = selected.session_mutation_template.get("next_turn")
                if not isinstance(raw, Mapping):
                    raise TerminalProtocolMalformedError("follow-up durable template is missing")
                template = _canonical_mapping_copy(raw)
                intent = template.get("execution_intent")
                if not isinstance(intent, dict):
                    raise TerminalProtocolMalformedError("follow-up execution intent is malformed")
                turn_id = _required_text(
                    template.get("turn_id"),
                    "followup.turn_id",
                )
                request_id = _required_text(
                    template.get("request_id"),
                    "followup.request_id",
                )
                user_text = template.get("user_text")
                if not isinstance(user_text, str):
                    raise TerminalProtocolMalformedError("follow-up user text is malformed")
                canonical = {
                    "execution_intent": intent,
                    "request_id": request_id,
                    "session_id": scope.session_id,
                    "team_id": scope.team_id,
                    "turn_id": turn_id,
                    "user_text": user_text,
                }
                intent_digest = canonical_digest_v1(canonical)
                if template.get("execution_intent_digest") != intent_digest:
                    raise TerminalProtocolMalformedError("follow-up execution intent digest changed")
                async with connection.execute(
                    """
                    SELECT t.session_id,t.user_text,t.terminal_protocol_version,
                           t.current_generation,t.presentation_locale,
                           g.execution_intent_id,g.execution_intent_digest,
                           g.admission_request_digest,g.execution_intent_json,
                           g.execution_authz_snapshot_digest
                    FROM turns t
                    JOIN turn_generations g
                      ON g.turn_id=t.id AND g.generation=1
                    WHERE t.id=?
                    """,
                    (turn_id,),
                ) as cursor:
                    child = await cursor.fetchone()
                if child is None or any(
                    child[name] != value
                    for name, value in {
                        "session_id": scope.session_id,
                        "user_text": user_text,
                        "terminal_protocol_version": 1,
                        "current_generation": 1,
                        "presentation_locale": intent.get("presentation_locale"),
                        "execution_intent_id": request_id,
                        "execution_intent_digest": intent_digest,
                        "admission_request_digest": intent_digest,
                        "execution_intent_json": canonical_json_v1(intent),
                        "execution_authz_snapshot_digest": intent.get("source_authz_snapshot_digest"),
                    }.items()
                ):
                    raise TerminalProtocolMalformedError("follow-up committed child envelope changed")
                await connection.rollback()
                return template
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def read_recovery_effect_context(
        self,
        processing: RecoveryProcessingContext,
    ) -> RecoveryEffectContext:
        """Read the sole decision-bound provider attempt under a recovery fence."""

        if not isinstance(processing, RecoveryProcessingContext):
            raise TypeError("recovery effect processing context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            generation = await _read_generation_claim_row(connection, processing.scope)
            recovery = await _read_active_recovery(connection, processing.recovery_id)
            if not _recovery_processing_matches(generation, recovery, processing, self._now()):
                raise TerminalProtocolMalformedError("recovery effect fence changed")
            execution_contract = resolve_effect_execution_contract(_parse_payload(generation["execution_intent_json"]))
            if execution_contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
                await _read_effect_disclosure_snapshot_v2(
                    connection,
                    processing.scope,
                )
            async with connection.execute(
                """
                SELECT a.*,d.decision_id,d.source_kind,d.source_id,d.action,
                       g.execution_authz_snapshot_digest,g.execution_intent_json
                FROM turn_effect_attempts a
                JOIN turn_generations g
                  ON g.turn_id=a.turn_id AND g.generation=a.generation
                JOIN turn_control_decisions d ON d.decision_id=g.decision_id
                WHERE a.turn_id=? AND a.generation=?
                  AND a.effect_class='side_effect_possible'
                ORDER BY a.attempt_id
                """,
                (processing.scope.turn_id, processing.scope.generation),
            ) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            raise TerminalProtocolMalformedError("recovery effect decision cardinality is invalid")
        if execution_contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
            approval_rows = tuple(
                row
                for row in rows
                if row["provider"] == "approval_decision/v1" or (isinstance(row["provider"], str) and row["provider"].startswith("approval:"))
            )
        else:
            approval_rows = tuple(row for row in rows if isinstance(row["provider"], str) and row["provider"].startswith("approval:"))
            if len(rows) != 1:
                raise TerminalProtocolMalformedError("recovery effect decision cardinality is invalid")
        if len(approval_rows) != 1:
            raise TerminalProtocolMalformedError("recovery effect decision cardinality is invalid")
        row = approval_rows[0]
        try:
            source_kind = DecisionSourceKind(row["source_kind"])
            action = DecisionAction(row["action"])
        except ValueError as exc:
            raise TerminalProtocolMalformedError("recovery effect decision enum is malformed") from exc
        return RecoveryEffectContext(
            scope=processing.scope,
            decision_id=_required_text(row["decision_id"], "recovery_effect.decision_id"),
            source_kind=source_kind,
            source_id=_required_text(row["source_id"], "recovery_effect.source_id"),
            action=action,
            attempt=_effect_attempt_from_row(processing.scope, row),
            execution_authz_snapshot_digest=_required_text(
                row["execution_authz_snapshot_digest"],
                "recovery_effect.execution_authz_snapshot_digest",
            ),
        )

    async def record_effect_settlement(
        self,
        context: EffectSettlementContext,
    ) -> EffectAttemptContext:
        """写入唯一 evidence/settlement winner，并原子 settle attempt。"""

        return await self._record_effect_settlement(
            context,
            allow_v2_provider_control=False,
            allow_v2_approval_dispatched=False,
        )

    async def _record_effect_settlement(
        self,
        context: EffectSettlementContext,
        *,
        allow_v2_provider_control: bool,
        allow_v2_approval_dispatched: bool,
    ) -> EffectAttemptContext:
        """Shared transaction; public seams choose the contract capability."""

        if not isinstance(context, EffectSettlementContext):
            raise TypeError("effect settlement context is invalid")
        self._inject(TerminalProtocolFaultPoint.EFFECT_BEFORE_SETTLE)
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                result = await _record_effect_settlement_in_tx(
                    connection,
                    context=context,
                    now=now,
                    inject=self._inject,
                    allow_v2_manual=False,
                    allow_v2_provider_control=allow_v2_provider_control,
                    allow_v2_approval_dispatched=allow_v2_approval_dispatched,
                    allow_v2_local_billing=False,
                    allow_v2_llm_completion=False,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def record_provider_effect_settlement(
        self,
        context: EffectSettlementContext,
    ) -> EffectAttemptContext:
        """Adapter-only alias; the immutable provider contract is checked in tx."""

        if not isinstance(context, EffectSettlementContext):
            raise TypeError("provider effect settlement context is invalid")
        if context.source is not EffectSettlementSource.PROVIDER:
            raise ValueError("provider effect settlement requires provider source")
        return await self._record_effect_settlement(
            context,
            allow_v2_provider_control=True,
            allow_v2_approval_dispatched=False,
        )

    async def record_llm_effect_settlement_with_usage(
        self,
        execution: ExecutionGenerationContext,
        context: EffectSettlementContext,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_amount: float,
    ) -> LLMEffectSettlementResult:
        """Settle one physical LLM attempt and increment exact usage atomically."""

        return await self._record_runtime_effect_settlement(
            execution,
            context,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_amount=cost_amount,
            completion_kind="llm",
        )

    async def record_tool_effect_settlement(
        self,
        execution: ExecutionGenerationContext,
        context: EffectSettlementContext,
    ) -> LLMEffectSettlementResult:
        """Settle one preclaimed tool mutation and preserve the execution fence."""

        return await self._record_runtime_effect_settlement(
            execution,
            context,
            input_tokens=0,
            output_tokens=0,
            cost_amount=0.0,
            completion_kind="tool",
        )

    async def _record_runtime_effect_settlement(
        self,
        execution: ExecutionGenerationContext,
        context: EffectSettlementContext,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_amount: float,
        completion_kind: str,
    ) -> LLMEffectSettlementResult:
        """Shared active/recovery/Stop completion transaction for physical effects."""

        if not isinstance(execution, ExecutionGenerationContext) or not isinstance(context, EffectSettlementContext):
            raise TypeError("runtime effect completion context is invalid")
        if execution.scope != context.scope:
            raise ValueError("runtime effect completion scope mismatch")
        if completion_kind not in {"llm", "tool"}:
            raise ValueError("runtime effect completion kind is invalid")
        for value in (input_tokens, output_tokens):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("runtime effect token usage is invalid")
        if (
            not isinstance(cost_amount, (int, float))
            or isinstance(cost_amount, bool)
            or float(cost_amount) < 0
            or float(cost_amount) != float(cost_amount)
            or float(cost_amount) == float("inf")
        ):
            raise ValueError("runtime effect cost usage is invalid")
        if context.source is not EffectSettlementSource.PROVIDER:
            raise ValueError("runtime completion requires provider settlement")
        if completion_kind == "llm":
            if context.outcome not in {
                EffectSettlementOutcome.CONFIRMED_APPLIED,
                EffectSettlementOutcome.CONFIRMED_NOT_APPLIED,
            }:
                raise ValueError("LLM completion outcome is unsupported")
            if context.outcome is EffectSettlementOutcome.CONFIRMED_NOT_APPLIED and (input_tokens or output_tokens or float(cost_amount)):
                raise ValueError("not-applied LLM completion cannot carry usage")
        elif (
            context.outcome
            not in get_effect_provider_contract(
                "tool_external/v1",
            ).allowed_outcomes
        ):
            raise ValueError("tool completion outcome is unsupported")
        if completion_kind == "tool" and (input_tokens or output_tokens or float(cost_amount)):
            raise ValueError("tool completion cannot carry LLM usage")

        self._inject(TerminalProtocolFaultPoint.EFFECT_BEFORE_SETTLE)
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                generation = await _read_generation_claim_row(connection, execution.scope)
                if generation is None:
                    raise TerminalProtocolMalformedError("runtime effect completion generation is missing")
                contract = resolve_effect_execution_contract(
                    _parse_payload(generation["execution_intent_json"]),
                )
                if contract.disposition is not EffectIntentDisposition.RECOVERY_ONLY:
                    raise TerminalProtocolMalformedError("runtime effect completion requires contract-v2")
                if (
                    generation["owner_instance_id"] != execution.owner_instance_id
                    or generation["claim_epoch"] != execution.claim_epoch
                    or generation["writer_lease_epoch_at_claim"] != execution.writer_lease_epoch
                ):
                    raise TerminalProtocolMalformedError("runtime effect completion execution identity changed")
                async with connection.execute(
                    "SELECT 1 FROM turn_effect_settlements WHERE attempt_id=?",
                    (context.attempt_id,),
                ) as cursor:
                    replay = await cursor.fetchone()
                created_here = replay is None
                settled = await _record_effect_settlement_in_tx(
                    connection,
                    context=context,
                    now=now,
                    inject=self._inject,
                    allow_v2_manual=False,
                    allow_v2_provider_control=False,
                    allow_v2_approval_dispatched=False,
                    allow_v2_local_billing=False,
                    allow_v2_llm_completion=completion_kind == "llm",
                    allow_v2_tool_completion=completion_kind == "tool",
                )
                state = _generation_state(generation["state"])
                usage_recorded = False
                if (
                    completion_kind == "llm"
                    and created_here
                    and context.outcome is EffectSettlementOutcome.CONFIRMED_APPLIED
                    and state
                    in {
                        GenerationState.RUNNING,
                        GenerationState.RECOVERY_PENDING,
                    }
                ):
                    if generation["usage_digest"] is not None:
                        raise TerminalProtocolMalformedError("runtime effect completion cannot change frozen usage")
                    current_cost = _nonnegative_float(
                        generation["cost_amount"],
                        "runtime effect completion generation cost",
                    )
                    usage_update = await connection.execute(
                        """
                        UPDATE turn_generations
                        SET input_tokens=input_tokens+?,output_tokens=output_tokens+?,cost_amount=?
                        WHERE turn_id=? AND generation=? AND protocol_version=1
                          AND state IN ('running','recovery_pending') AND usage_digest IS NULL
                          AND owner_instance_id=? AND claim_epoch=?
                        """,
                        (
                            input_tokens,
                            output_tokens,
                            f"{current_cost + float(cost_amount):.12f}",
                            execution.scope.turn_id,
                            execution.scope.generation,
                            execution.owner_instance_id,
                            execution.claim_epoch,
                        ),
                    )
                    if usage_update.rowcount != 1:
                        raise TerminalProtocolMalformedError("runtime effect completion usage fence changed")
                    usage_recorded = True
                    self._inject(TerminalProtocolFaultPoint.LLM_COMPLETION_AFTER_USAGE)

                continue_execution = False
                if state is GenerationState.RUNNING:
                    auth_valid = True
                    try:
                        await _validate_effect_dispatch_auth(connection, execution)
                    except DecisionAccessRevokedError:
                        auth_valid = False
                    ownership_valid = _execution_context_matches(generation, execution, now) and await _writer_lease_matches_execution(
                        connection,
                        context=execution,
                        now=now,
                    )
                    continue_execution = bool(auth_valid and ownership_valid)
                    if not continue_execution:
                        await _discover_recovery_in_tx(
                            connection,
                            context=RecoveryDiscoveryContext(
                                scope=execution.scope,
                                observed_state=GenerationState.RUNNING,
                                observed_generation_row_version=_positive_int(
                                    generation["row_version"],
                                    "LLM completion generation row version",
                                ),
                                observed_execution_claim_epoch=execution.claim_epoch,
                                observed_finalization_epoch=_nonnegative_int(
                                    generation["finalization_epoch"],
                                    "LLM completion finalization epoch",
                                ),
                                scanner_id=f"{completion_kind}_completion:{context.attempt_id}",
                            ),
                            generation=generation,
                            now=now,
                            inject=self._inject,
                        )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return LLMEffectSettlementResult(
            attempt=settled,
            continue_execution=continue_execution,
            usage_recorded=usage_recorded,
        )

    async def record_admin_probed_effect_settlement(
        self,
        context: EffectSettlementContext,
        *,
        actor_user_id: str,
        resolve_context: AuthFenceResolveContext,
        expected_auth_fence_vector: AuthFenceVector,
    ) -> EffectAttemptContext:
        """Atomically persist adapter evidence and the admin probe audit."""

        if not isinstance(context, EffectSettlementContext):
            raise TypeError("provider probe settlement context is invalid")
        if context.source is not EffectSettlementSource.PROVIDER:
            raise ValueError("provider probe settlement requires provider source")
        if not isinstance(actor_user_id, str) or not actor_user_id:
            raise ValueError("provider probe settlement actor is invalid")
        if not isinstance(resolve_context, AuthFenceResolveContext) or not isinstance(
            expected_auth_fence_vector,
            AuthFenceVector,
        ):
            raise TypeError("provider probe settlement auth fence is invalid")
        expected_scopes = {
            AuthFenceScope(
                AuthFenceScopeKind.TEAM_MEMBERSHIP,
                context.scope.team_id,
                context.scope.team_id,
            ),
            AuthFenceScope(
                AuthFenceScopeKind.RESOURCE_GRANT,
                context.scope.team_id,
                context.scope.team_id,
            ),
            AuthFenceScope(
                AuthFenceScopeKind.SESSION_VISIBILITY,
                context.scope.team_id,
                context.scope.session_id,
            ),
            AuthFenceScope(
                AuthFenceScopeKind.USER_ACCOUNT,
                "__global__",
                actor_user_id,
            ),
        }
        if (
            resolve_context.team_id != context.scope.team_id
            or resolve_context.session_id != context.scope.session_id
            or resolve_context.resource_ref != context.scope.turn_id
            or resolve_context.viewer != actor_user_id
            or set(resolve_context.required_scopes) != expected_scopes
        ):
            raise ValueError("provider probe settlement auth identity mismatch")
        self._inject(TerminalProtocolFaultPoint.EFFECT_BEFORE_SETTLE)
        created_here = False
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                current_vector = await _read_auth_fence_vector(
                    connection,
                    resolve_context.required_scopes,
                )
                role = await _read_team_role_in_tx(
                    connection,
                    team_id=context.scope.team_id,
                    user_id=actor_user_id,
                    require_active_principal=True,
                )
                if current_vector != expected_auth_fence_vector or role not in {"owner", "team_admin"}:
                    raise DecisionAccessRevokedError("provider probe admin authority changed")
                async with connection.execute(
                    "SELECT state FROM turn_effect_attempts WHERE attempt_id=?",
                    (context.attempt_id,),
                ) as cursor:
                    prior_attempt = await cursor.fetchone()
                created_here = bool(prior_attempt is not None and prior_attempt["state"] != EffectState.SETTLED.value)
                result = await _record_effect_settlement_in_tx(
                    connection,
                    context=context,
                    now=self._now(),
                    inject=self._inject,
                    allow_v2_manual=False,
                    allow_v2_provider_control=True,
                    allow_v2_approval_dispatched=False,
                    allow_v2_local_billing=False,
                    allow_v2_llm_completion=False,
                )
                audit_id = (
                    "aud_"
                    + canonical_digest_v1(
                        {
                            "action": AuditAction.TERMINAL_EFFECT_PROVIDER_PROBED,
                            "actor_user_id": actor_user_id,
                            "settlement_id": context.settlement_id,
                            "team_id": context.scope.team_id,
                        }
                    )[:32]
                )
                try:
                    await UserStore(self._db_path).write_admin_audit_log(
                        actor_user_id=actor_user_id,
                        team_id=context.scope.team_id,
                        action=AuditAction.TERMINAL_EFFECT_PROVIDER_PROBED,
                        target_kind="terminal_effect_attempt",
                        target_id=context.attempt_id,
                        target_team_id=context.scope.team_id,
                        scope="team",
                        payload={
                            "attempt_id": context.attempt_id,
                            "provider_adapter": context.principal_ref,
                            "settlement_id": context.settlement_id,
                            "turn_id": context.scope.turn_id,
                        },
                        conn=connection,
                        audit_id=audit_id,
                    )
                except ValueError as exc:
                    raise TerminalProtocolMalformedError("provider probe audit identity conflict") from exc
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        if created_here:
            _record_effect_resolution_metric(
                source="provider",
                outcome=context.outcome.value,
            )
        return result

    async def record_approval_projection_effect_settlement(
        self,
        context: EffectSettlementContext,
    ) -> EffectAttemptContext:
        """Bind an exact durable approval projection after provider dispatch."""

        if not isinstance(context, EffectSettlementContext):
            raise TypeError("approval projection settlement context is invalid")
        if context.source is not EffectSettlementSource.PROVIDER or context.principal_ref != "adapter:approval_decision_projection/v1":
            raise ValueError("approval projection settlement identity is invalid")
        return await self._record_effect_settlement(
            context,
            allow_v2_provider_control=True,
            allow_v2_approval_dispatched=True,
        )

    async def record_manual_effect_settlement(
        self,
        context: EffectSettlementContext,
        *,
        actor_user_id: str,
        resolve_context: AuthFenceResolveContext,
        expected_auth_fence_vector: AuthFenceVector,
    ) -> EffectAttemptContext:
        """Settle a contract-allowed UNKNOWN after same-tx admin/auth recheck."""

        if not isinstance(context, EffectSettlementContext):
            raise TypeError("manual effect settlement context is invalid")
        if context.source is not EffectSettlementSource.MANUAL:
            raise ValueError("manual effect settlement requires manual source")
        if not isinstance(actor_user_id, str) or not actor_user_id:
            raise ValueError("manual effect settlement actor is invalid")
        if not isinstance(resolve_context, AuthFenceResolveContext) or not isinstance(
            expected_auth_fence_vector,
            AuthFenceVector,
        ):
            raise TypeError("manual effect settlement auth fence is invalid")
        if (
            resolve_context.team_id != context.scope.team_id
            or resolve_context.session_id != context.scope.session_id
            or resolve_context.resource_ref != context.scope.turn_id
            or resolve_context.viewer != actor_user_id
            or context.principal_ref != f"admin:{actor_user_id}"
            or set(resolve_context.required_scopes)
            != {
                AuthFenceScope(
                    AuthFenceScopeKind.TEAM_MEMBERSHIP,
                    context.scope.team_id,
                    context.scope.team_id,
                ),
                AuthFenceScope(
                    AuthFenceScopeKind.RESOURCE_GRANT,
                    context.scope.team_id,
                    context.scope.team_id,
                ),
                AuthFenceScope(
                    AuthFenceScopeKind.SESSION_VISIBILITY,
                    context.scope.team_id,
                    context.scope.session_id,
                ),
                AuthFenceScope(
                    AuthFenceScopeKind.USER_ACCOUNT,
                    "__global__",
                    actor_user_id,
                ),
            }
        ):
            raise ValueError("manual effect settlement auth identity mismatch")
        self._inject(TerminalProtocolFaultPoint.EFFECT_BEFORE_SETTLE)
        created_here = False
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                current_vector = await _read_auth_fence_vector(
                    connection,
                    resolve_context.required_scopes,
                )
                if current_vector != expected_auth_fence_vector:
                    raise TerminalProtocolMalformedError("manual effect settlement auth fence changed")
                if context.authz_snapshot_digest != expected_auth_fence_vector.auth_fence_vector_digest:
                    async with connection.execute(
                        "SELECT authz_snapshot_digest FROM turn_effect_settlements WHERE attempt_id=?",
                        (context.attempt_id,),
                    ) as cursor:
                        persisted_auth = await cursor.fetchone()
                    if persisted_auth is None or persisted_auth["authz_snapshot_digest"] != context.authz_snapshot_digest:
                        raise TerminalProtocolMalformedError("manual effect settlement auth fence changed")
                role = await _read_team_role_in_tx(
                    connection,
                    team_id=context.scope.team_id,
                    user_id=actor_user_id,
                    require_active_principal=True,
                )
                expected_role = "owner" if role == "owner" else "admin"
                if role not in {"owner", "team_admin"} or context.principal_role != expected_role:
                    raise DecisionAccessRevokedError("manual effect settlement admin authority changed")
                async with connection.execute(
                    "SELECT state FROM turn_effect_attempts WHERE attempt_id=?",
                    (context.attempt_id,),
                ) as cursor:
                    prior_attempt = await cursor.fetchone()
                created_here = bool(prior_attempt is not None and prior_attempt["state"] != EffectState.SETTLED.value)
                now = self._now()
                result = await _record_effect_settlement_in_tx(
                    connection,
                    context=context,
                    now=now,
                    inject=self._inject,
                    allow_v2_manual=True,
                    allow_v2_provider_control=False,
                    allow_v2_approval_dispatched=False,
                    allow_v2_local_billing=False,
                    allow_v2_llm_completion=False,
                )
                audit_id = (
                    "aud_"
                    + canonical_digest_v1(
                        {
                            "action": AuditAction.TERMINAL_EFFECT_MANUAL_SETTLED,
                            "actor_user_id": actor_user_id,
                            "settlement_id": context.settlement_id,
                            "team_id": context.scope.team_id,
                        }
                    )[:32]
                )
                try:
                    await UserStore(self._db_path).write_admin_audit_log(
                        actor_user_id=actor_user_id,
                        team_id=context.scope.team_id,
                        action=AuditAction.TERMINAL_EFFECT_MANUAL_SETTLED,
                        target_kind="terminal_effect_attempt",
                        target_id=context.attempt_id,
                        target_team_id=context.scope.team_id,
                        scope="team",
                        payload={
                            "attempt_id": context.attempt_id,
                            "outcome": context.outcome.value,
                            "settlement_id": context.settlement_id,
                            "turn_id": context.scope.turn_id,
                        },
                        conn=connection,
                        audit_id=audit_id,
                    )
                except ValueError as exc:
                    raise TerminalProtocolMalformedError("manual effect settlement audit identity conflict") from exc
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        if created_here:
            _record_effect_resolution_metric(
                source="manual",
                outcome=context.outcome.value,
            )
        return result

    async def read_effect_attempt_details(
        self,
        scope: TerminalScope,
        *,
        actor_user_id: str,
    ) -> tuple[EffectAttemptDetail, ...]:
        """Return exact proof only after persisted team-admin revalidation."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("effect attempt detail scope is invalid")
        if not isinstance(actor_user_id, str) or not actor_user_id:
            raise ValueError("effect attempt detail actor is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            try:
                role = await _read_team_role_in_tx(
                    connection,
                    team_id=scope.team_id,
                    user_id=actor_user_id,
                    require_active_principal=True,
                )
                if role not in {"owner", "team_admin"}:
                    raise DecisionAccessRevokedError("effect attempt detail requires current team admin")
                generation = await _read_generation_claim_row(connection, scope)
                if generation is None:
                    raise TerminalProtocolMalformedError("effect attempt detail generation is missing")
                execution_contract = resolve_effect_execution_contract(_parse_payload(generation["execution_intent_json"]))
                if execution_contract.disposition is EffectIntentDisposition.LEGACY:
                    return ()
                await _read_effect_disclosure_snapshot_v2(connection, scope)
                async with connection.execute(
                    """
                SELECT a.*,
                       s.settlement_id AS persisted_settlement_id,
                       s.source AS settlement_source,s.outcome,s.settlement_code,
                       s.actor_audit_ref,s.authz_snapshot_digest,s.evidence_link_id,
                       s.evidence_digest AS evidence_digest,
                       s.created_at AS settlement_created_at,
                       s.provider_external_ref_digest,
                       s.provider_external_ref_key_version,
                       e.team_id AS evidence_team_id,
                       e.evidence_digest AS linked_evidence_digest,
                       e.object_version_digest,e.audit_object_ref,
                       e.retention_state AS evidence_retention_state,
                       e.purge_after AS evidence_purge_after,
                       e.purge_request_id,
                       e.purged_at AS evidence_purged_at,
                       e.created_at AS evidence_created_at,
                       t.session_id,w.team_id
                FROM turn_effect_attempts a
                JOIN turns t ON t.id=a.turn_id
                JOIN sessions ss ON ss.id=t.session_id
                JOIN workspaces w ON w.id=ss.workspace_id
                LEFT JOIN turn_effect_settlements s ON s.attempt_id=a.attempt_id
                LEFT JOIN turn_effect_evidence_links e
                  ON e.evidence_link_id=s.evidence_link_id
                WHERE a.turn_id=? AND a.generation=?
                  AND a.effect_class='side_effect_possible'
                ORDER BY a.attempt_id
                """,
                    (scope.turn_id, scope.generation),
                ) as cursor:
                    rows = await cursor.fetchall()
            finally:
                if connection.in_transaction:
                    await connection.rollback()
        details: list[EffectAttemptDetail] = []
        for row in rows:
            if row["session_id"] != scope.session_id or row["team_id"] != scope.team_id:
                raise TerminalProtocolMalformedError("effect attempt detail scope changed")
            contract = get_effect_provider_contract(_required_text(row["provider"], "effect attempt detail provider"))
            try:
                outcome = EffectSettlementOutcome(row["outcome"]) if row["outcome"] is not None else None
                settlement_source = EffectSettlementSource(row["settlement_source"]) if row["settlement_source"] is not None else None
                state = EffectState(row["state"])
            except ValueError as exc:
                raise TerminalProtocolMalformedError("effect attempt detail enum is malformed") from exc
            if state is EffectState.SETTLED and (
                settlement_source not in contract.allowed_settlement_sources or outcome not in contract.allowed_outcomes
            ):
                raise TerminalProtocolMalformedError("effect attempt detail violates provider contract")
            if state is EffectState.SETTLED:
                _validate_settled_effect_projection(row)
                if row["evidence_team_id"] != scope.team_id or row["linked_evidence_digest"] != row["evidence_digest"]:
                    raise TerminalProtocolMalformedError("effect attempt detail evidence binding changed")
                _validate_v2_effect_control_evidence(row, contract.key)
            evidence_retention_state = row["evidence_retention_state"]
            if (
                evidence_retention_state == "purged" and (row["audit_object_ref"] is not None or not _is_aware_timestamp(row["evidence_purged_at"]))
            ) or (evidence_retention_state in {"active", "purge_pending"} and row["evidence_purged_at"] is not None):
                raise TerminalProtocolMalformedError("effect attempt detail retention proof is malformed")
            try:
                detail = EffectAttemptDetail(
                    attempt=_effect_attempt_from_row(scope, row),
                    disclosure_kind=contract.disclosure_kind.value,
                    outcome=outcome,
                    result_digest=row["result_digest"],
                    settlement_id=row["settlement_id"],
                    settlement_source=settlement_source,
                    settlement_code=row["settlement_code"],
                    actor_audit_ref=row["actor_audit_ref"],
                    authz_snapshot_digest=row["authz_snapshot_digest"],
                    evidence_link_id=row["evidence_link_id"],
                    evidence_digest=row["evidence_digest"],
                    object_version_digest=row["object_version_digest"],
                    audit_object_ref=row["audit_object_ref"],
                    evidence_retention_state=evidence_retention_state,
                    evidence_purge_after=row["evidence_purge_after"],
                    provider_external_ref_digest=row["provider_external_ref_digest"],
                    provider_external_ref_key_version=row["provider_external_ref_key_version"],
                    attempt_started_at=_required_text(
                        row["started_at"],
                        "effect attempt detail start time",
                    ),
                    manual_settlement_allowed=contract.allows_manual_settlement,
                )
            except (TypeError, ValueError) as exc:
                raise TerminalProtocolMalformedError("effect attempt detail proof is malformed") from exc
            details.append(detail)
        return tuple(details)

    async def list_recovery_candidates(
        self,
        *,
        scanner_id: str,
        limit: int = 100,
        task_start_grace_seconds: int = TERMINAL_TASK_START_GRACE_SECONDS,
    ) -> tuple[RecoveryDiscoveryContext, ...]:
        """读取 overdue generation snapshots；scanner 本身不拥有收敛授权。"""

        if not isinstance(scanner_id, str) or not scanner_id:
            raise ValueError("recovery scanner identity is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("recovery scan limit must be between 1 and 100")
        if not isinstance(task_start_grace_seconds, int) or isinstance(task_start_grace_seconds, bool) or task_start_grace_seconds < 0:
            raise ValueError("recovery task-start grace is invalid")
        now = self._now()
        now_text = _iso_utc(now)
        opened_cutoff = _iso_utc(now - timedelta(seconds=task_start_grace_seconds))
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT g.turn_id,g.generation,g.state,g.row_version,g.claim_epoch,
                       g.finalization_epoch,t.session_id,w.team_id
                FROM turn_generations g
                JOIN turns t ON t.id=g.turn_id AND t.current_generation=g.generation
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                WHERE g.protocol_version=1 AND g.terminal_receipt_id IS NULL
                  AND g.recovery_resolution IS NULL
                  AND (
                    (g.state='opened' AND g.owner_instance_id IS NULL AND g.opened_at<=?) OR
                    (g.state='running' AND (g.lease_expires_at IS NULL OR g.lease_expires_at<=?)) OR
                    (g.state='closing' AND (g.finalization_lease_expires_at IS NULL OR g.finalization_lease_expires_at<=?)) OR
                    (g.state='recovery_pending' AND (
                      NOT EXISTS (
                        SELECT 1 FROM turn_recovery_records rr
                        WHERE rr.turn_id=g.turn_id AND rr.generation=g.generation
                          AND rr.scope='terminal' AND rr.state='active'
                      ) OR EXISTS (
                        SELECT 1 FROM turn_recovery_records rr
                        WHERE rr.turn_id=g.turn_id AND rr.generation=g.generation
                          AND rr.scope='terminal' AND rr.state='active'
                          AND (rr.next_attempt_at IS NULL OR rr.next_attempt_at<=?)
                          AND (rr.recovery_owner_instance_id IS NULL
                               OR rr.recovery_lease_expires_at IS NULL
                               OR rr.recovery_lease_expires_at<=?)
                      )
                    ))
                  )
                ORDER BY g.opened_at,g.turn_id,g.generation
                LIMIT ?
                """,
                (opened_cutoff, now_text, now_text, now_text, now_text, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        self._inject(TerminalProtocolFaultPoint.RECOVERY_DISCOVERY_AFTER_SCAN)
        return tuple(
            RecoveryDiscoveryContext(
                scope=TerminalScope(
                    team_id=_required_text(row["team_id"], "recovery.team_id"),
                    session_id=_required_text(row["session_id"], "recovery.session_id"),
                    turn_id=_required_text(row["turn_id"], "recovery.turn_id"),
                    generation=_positive_int(row["generation"], "recovery.generation"),
                ),
                observed_state=GenerationState(row["state"]),
                observed_generation_row_version=_positive_int(row["row_version"], "recovery.row_version"),
                observed_execution_claim_epoch=_nonnegative_int(row["claim_epoch"], "recovery.claim_epoch"),
                observed_finalization_epoch=_nonnegative_int(
                    row["finalization_epoch"],
                    "recovery.finalization_epoch",
                ),
                scanner_id=scanner_id,
            )
            for row in rows
        )

    async def discover_recovery(
        self,
        context: RecoveryDiscoveryContext,
    ) -> RecoveryRecord:
        """幂等建立 ownerless active recovery、generation fence 与 Session lock。"""

        if not isinstance(context, RecoveryDiscoveryContext):
            raise TypeError("recovery discovery context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None:
                    raise TerminalProtocolMalformedError("recovery generation is missing")
                result = await _discover_recovery_in_tx(
                    connection,
                    context=context,
                    generation=row,
                    now=self._now(),
                    inject=self._inject,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def read_recovery_discovery_for_scope(
        self,
        scope: TerminalScope,
        *,
        scanner_id: str,
    ) -> RecoveryDiscoveryContext:
        """Resolve one exact generation for synchronous worker-start recovery."""

        if not isinstance(scope, TerminalScope):
            raise TypeError("recovery scope is invalid")
        if not isinstance(scanner_id, str) or not scanner_id:
            raise ValueError("recovery scanner identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            row = await _read_generation_claim_row(connection, scope)
        if row is None or row["terminal_receipt_id"] is not None or row["recovery_resolution"] is not None:
            raise TerminalProtocolMalformedError("recovery generation is unavailable")
        return RecoveryDiscoveryContext(
            scope=scope,
            observed_state=GenerationState(row["state"]),
            observed_generation_row_version=_positive_int(
                row["row_version"],
                "recovery.row_version",
            ),
            observed_execution_claim_epoch=_nonnegative_int(
                row["claim_epoch"],
                "recovery.claim_epoch",
            ),
            observed_finalization_epoch=_nonnegative_int(
                row["finalization_epoch"],
                "recovery.finalization_epoch",
            ),
            scanner_id=scanner_id,
        )

    async def claim_recovery(
        self,
        record: RecoveryRecord,
        *,
        owner_instance_id: str,
    ) -> RecoveryProcessingContext:
        """processor 对 ownerless/expired recovery 取得独立 recovery/finalization epoch。"""

        if not isinstance(record, RecoveryRecord):
            raise TypeError("recovery record is invalid")
        if not isinstance(owner_instance_id, str) or not owner_instance_id:
            raise ValueError("recovery owner identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                async with connection.execute(
                    "SELECT * FROM turn_recovery_records WHERE recovery_id=?",
                    (record.recovery_id,),
                ) as cursor:
                    recovery = await cursor.fetchone()
                generation = await _read_generation_claim_row(connection, record.scope)
                if recovery is None or generation is None:
                    raise TerminalProtocolMalformedError("recovery claim target is missing")
                if (
                    recovery["turn_id"] != record.scope.turn_id
                    or recovery["generation"] != record.scope.generation
                    or recovery["kind"] != record.kind.value
                    or recovery["discovery_identity"] != record.discovery_identity
                    or not _recovery_session_lock_matches(generation, recovery, record.scope)
                ):
                    raise TerminalProtocolMalformedError("recovery claim identity changed")
                if (
                    recovery["state"] == "active"
                    and generation["state"] == GenerationState.RECOVERY_PENDING
                    and generation["terminal_receipt_id"] is None
                    and recovery["recovery_owner_instance_id"] == owner_instance_id
                    and _timestamp_after(recovery["recovery_lease_expires_at"], now)
                    and generation["finalization_owner_instance_id"] == owner_instance_id
                    and _timestamp_after(generation["finalization_lease_expires_at"], now)
                ):
                    await connection.rollback()
                    return _recovery_processing_from_rows(record.scope, recovery, generation)
                if (
                    recovery["state"] != "active"
                    or generation["state"] != GenerationState.RECOVERY_PENDING
                    or generation["terminal_receipt_id"] is not None
                    or generation["row_version"] != record.generation_row_version
                ):
                    raise TerminalProtocolMalformedError("recovery claim fence changed")
                if recovery["recovery_owner_instance_id"] is not None and _timestamp_after(
                    recovery["recovery_lease_expires_at"],
                    now,
                ):
                    if recovery["recovery_owner_instance_id"] != owner_instance_id:
                        raise TerminalProtocolMalformedError("recovery is owned by another processor")
                    if generation["finalization_owner_instance_id"] != owner_instance_id or not _timestamp_after(
                        generation["finalization_lease_expires_at"],
                        now,
                    ):
                        raise TerminalProtocolMalformedError("recovery finalization lease is inconsistent")
                    await connection.rollback()
                    return _recovery_processing_from_rows(record.scope, recovery, generation)
                next_claim_epoch = (
                    _nonnegative_int(
                        recovery["recovery_claim_epoch"],
                        "recovery.recovery_claim_epoch",
                    )
                    + 1
                )
                next_finalization_epoch = (
                    _nonnegative_int(
                        generation["finalization_epoch"],
                        "generation.finalization_epoch",
                    )
                    + 1
                )
                lease_expires_at = _iso_utc(now + timedelta(seconds=_RECOVERY_LEASE_SECONDS))
                now_text = _iso_utc(now)
                recovery_update = await connection.execute(
                    """
                    UPDATE turn_recovery_records
                    SET recovery_owner_instance_id=?,recovery_claim_epoch=?,
                        recovery_lease_expires_at=?,recovery_heartbeat_at=?,
                        attempt_count=attempt_count+1,next_attempt_at=NULL,phase='processing'
                    WHERE recovery_id=? AND state='active' AND recovery_claim_epoch=?
                      AND (recovery_owner_instance_id IS NULL OR recovery_lease_expires_at IS NULL
                           OR recovery_lease_expires_at<=?)
                    """,
                    (
                        owner_instance_id,
                        next_claim_epoch,
                        lease_expires_at,
                        now_text,
                        record.recovery_id,
                        record.recovery_claim_epoch,
                        now_text,
                    ),
                )
                if recovery_update.rowcount != 1:
                    raise TerminalProtocolMalformedError("recovery owner claim CAS changed")
                generation_update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET row_version=row_version+1,finalization_owner_instance_id=?,
                        finalization_epoch=?,finalization_lease_expires_at=?
                    WHERE turn_id=? AND generation=? AND protocol_version=1
                      AND state='recovery_pending' AND row_version=?
                      AND terminal_receipt_id IS NULL AND recovery_resolution IS NULL
                    """,
                    (
                        owner_instance_id,
                        next_finalization_epoch,
                        lease_expires_at,
                        record.scope.turn_id,
                        record.scope.generation,
                        record.generation_row_version,
                    ),
                )
                if generation_update.rowcount != 1:
                    raise TerminalProtocolMalformedError("recovery finalization takeover CAS changed")
                self._inject(TerminalProtocolFaultPoint.FINALIZATION_AFTER_OWNER_TRANSFER)
                result = RecoveryProcessingContext(
                    scope=record.scope,
                    recovery_id=record.recovery_id,
                    recovery_owner_instance_id=owner_instance_id,
                    recovery_claim_epoch=next_claim_epoch,
                    recovery_lease_expires_at=lease_expires_at,
                    finalization_epoch=next_finalization_epoch,
                    finalization_lease_expires_at=lease_expires_at,
                    generation_row_version=record.generation_row_version + 1,
                    closing_accepted_through=_nonnegative_int(
                        generation["closing_accepted_through"],
                        "generation.closing_accepted_through",
                    ),
                    cancelled_finalization=(generation["closing_reason"] == _CANCELLED_FINALIZATION_REASON),
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def defer_recovery(
        self,
        context: RecoveryProcessingContext,
        *,
        code: str,
        delay_seconds: int,
    ) -> bool:
        """Release an unsuccessful bounded attempt with a durable next-due fence."""

        if not isinstance(context, RecoveryProcessingContext):
            raise TypeError("recovery deferral context is invalid")
        if not isinstance(code, str) or not code or len(code) > 128:
            raise ValueError("recovery deferral code is invalid")
        if not isinstance(delay_seconds, int) or isinstance(delay_seconds, bool) or not 1 <= delay_seconds <= 3600:
            raise ValueError("recovery deferral delay is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                next_attempt = _iso_utc(now + timedelta(seconds=delay_seconds))
                recovery_update = await connection.execute(
                    """
                    UPDATE turn_recovery_records
                    SET recovery_owner_instance_id=NULL,recovery_lease_expires_at=NULL,
                        recovery_heartbeat_at=?,next_attempt_at=?,last_code=?,phase='deferred'
                    WHERE recovery_id=? AND state='active' AND recovery_owner_instance_id=?
                      AND recovery_claim_epoch=?
                    """,
                    (
                        _iso_utc(now),
                        next_attempt,
                        code,
                        context.recovery_id,
                        context.recovery_owner_instance_id,
                        context.recovery_claim_epoch,
                    ),
                )
                generation_update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET finalization_owner_instance_id=NULL,finalization_lease_expires_at=NULL
                    WHERE turn_id=? AND generation=? AND protocol_version=1
                      AND state='recovery_pending' AND finalization_owner_instance_id=?
                      AND finalization_epoch=? AND terminal_receipt_id IS NULL
                    """,
                    (
                        context.scope.turn_id,
                        context.scope.generation,
                        context.recovery_owner_instance_id,
                        context.finalization_epoch,
                    ),
                )
                if recovery_update.rowcount != generation_update.rowcount:
                    raise TerminalProtocolMalformedError("recovery deferral fence changed")
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return recovery_update.rowcount == 1

    async def defer_discovered_recovery(
        self,
        record: RecoveryRecord,
        *,
        code: str,
        delay_seconds: int,
    ) -> bool:
        """Back off a discovered row when ownership claim itself fails."""

        if not isinstance(record, RecoveryRecord):
            raise TypeError("discovered recovery deferral record is invalid")
        if not isinstance(code, str) or not code or len(code) > 128:
            raise ValueError("discovered recovery deferral code is invalid")
        if not isinstance(delay_seconds, int) or isinstance(delay_seconds, bool) or not 1 <= delay_seconds <= 3600:
            raise ValueError("discovered recovery deferral delay is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            now = self._now()
            now_text = _iso_utc(now)
            update = await connection.execute(
                """
                UPDATE turn_recovery_records
                SET recovery_owner_instance_id=NULL,recovery_lease_expires_at=NULL,
                    recovery_heartbeat_at=?,attempt_count=attempt_count+1,
                    next_attempt_at=?,last_code=?,phase='deferred'
                WHERE recovery_id=? AND turn_id=? AND generation=?
                  AND scope='terminal' AND state='active'
                  AND discovery_identity=? AND recovery_claim_epoch=?
                  AND (recovery_owner_instance_id IS NULL
                       OR recovery_lease_expires_at IS NULL
                       OR recovery_lease_expires_at<=?)
                """,
                (
                    now_text,
                    _iso_utc(now + timedelta(seconds=delay_seconds)),
                    code,
                    record.recovery_id,
                    record.scope.turn_id,
                    record.scope.generation,
                    record.discovery_identity,
                    record.recovery_claim_epoch,
                    now_text,
                ),
            )
            await connection.commit()
        return update.rowcount == 1

    async def load_candidate_for_recovery(
        self,
        context: RecoveryProcessingContext,
    ) -> TerminalCandidatePlan | None:
        """从 immutable staging rows 重建 candidate；恢复不得依赖进程内 plan。"""

        if not isinstance(context, RecoveryProcessingContext):
            raise TypeError("recovery processing context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            try:
                generation = await _read_generation_claim_row(connection, context.scope)
                recovery = await _read_active_recovery(connection, context.recovery_id)
                if not _recovery_processing_matches(generation, recovery, context, self._now()):
                    raise TerminalProtocolMalformedError("recovery processing fence changed")
                candidate_id = generation["terminal_candidate_id"]
                if candidate_id is None:
                    await connection.rollback()
                    return None
                result = await _load_terminal_candidate(connection, context.scope, candidate_id)
                await connection.rollback()
                return result
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def flush_recovery_through(
        self,
        context: RecoveryProcessingContext,
    ) -> TerminalFlushResult:
        """接管者补齐 frozen accepted prefix；不重跑 producer 或 execution。"""

        if not isinstance(context, RecoveryProcessingContext):
            raise TypeError("recovery processing context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                generation = await _read_generation_claim_row(connection, context.scope)
                recovery = await _read_active_recovery(connection, context.recovery_id)
                now = self._now()
                if not _recovery_processing_matches(generation, recovery, context, now):
                    raise TerminalProtocolMalformedError("recovery processing fence changed")
                proposals = await _read_event_proposal_prefix(
                    connection,
                    scope=context.scope,
                    accepted_through=context.closing_accepted_through,
                )
                synthetic = _finalization_from_recovery(context, generation)
                _validate_event_proposal_prefix(
                    proposals,
                    context=synthetic,
                    accepted_through=context.closing_accepted_through,
                )
                if generation["terminal_candidate_id"] is not None and any(proposal["state"] != "persisted" for proposal in proposals):
                    raise TerminalProtocolMalformedError("sealed recovery candidate has an unpersisted proposal")
                event_sequence_next = _positive_int(
                    generation["event_sequence_next"],
                    "turns.event_sequence_next",
                )
                tickets = await _materialize_event_proposal_prefix(
                    connection,
                    context=synthetic,
                    proposals=proposals,
                    event_sequence_next=event_sequence_next,
                    persisted_at=_iso_utc(now),
                )
                newly_persisted = sum(proposal["state"] != "persisted" for proposal in proposals)
                result = TerminalFlushResult(
                    closing_accepted_through=context.closing_accepted_through,
                    persisted_event_highwater=event_sequence_next + newly_persisted - 1,
                    tickets=tickets,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def prepare_recovery_candidate(
        self,
        context: RecoveryProcessingContext,
        plan: TerminalCandidatePlan,
    ) -> PreparedRecoveryContext:
        """重用 frozen candidate，或在无 candidate 时仅 stage recovery interrupt。"""

        if not isinstance(context, RecoveryProcessingContext) or not isinstance(plan, TerminalCandidatePlan):
            raise TypeError("recovery candidate prepare input is invalid")
        if plan.scope != context.scope or plan.closing_accepted_through != context.closing_accepted_through:
            raise ValueError("recovery candidate fence is invalid")
        _validate_candidate_plan_envelopes(plan)
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                generation = await _read_generation_claim_row(connection, context.scope)
                recovery = await _read_active_recovery(connection, context.recovery_id)
                if not _recovery_processing_matches(generation, recovery, context, now):
                    raise TerminalProtocolMalformedError("recovery processing fence changed")
                await _require_effects_safe_for_recovery(
                    connection,
                    context.scope,
                    allow_cancelled_unknown=bool(
                        generation["closing_reason"] == _CANCELLED_FINALIZATION_REASON
                        and any(
                            variant.outcome_variant is CandidateOutcome.PRIMARY and variant.status is TerminalStatus.CANCELLED
                            for variant in plan.variants
                        )
                    ),
                )
                existing_id = generation["terminal_candidate_id"]
                if existing_id is not None:
                    if existing_id != plan.candidate_id:
                        raise TerminalProtocolMalformedError("recovery candidate identity conflict")
                    stored = await _load_terminal_candidate(connection, context.scope, existing_id)
                    if stored.candidate_projection_digest != plan.candidate_projection_digest:
                        raise TerminalProtocolMalformedError("recovery candidate digest conflict")
                    result = PreparedRecoveryContext(
                        processing=context,
                        candidate_id=plan.candidate_id,
                        candidate_projection_digest=plan.candidate_projection_digest,
                    )
                    await connection.rollback()
                    return result
                if plan.origin is not CandidateOrigin.RECOVERY:
                    raise ValueError("new recovery candidate must use recovery origin")
                highwater = _positive_int(generation["event_sequence_next"], "turns.event_sequence_next") - 1
                if plan.persisted_event_highwater != highwater:
                    raise TerminalProtocolMalformedError("recovery candidate event highwater changed")
                await _validate_recovery_persisted_prefix(connection, context, generation)
                await _validate_candidate_effect_disclosure(connection, plan)
                synthetic = _finalization_from_recovery(context, generation)
                await _insert_terminal_candidate(
                    connection,
                    context=synthetic,
                    plan=plan,
                    prepared_at=_iso_utc(now),
                )
                update = await connection.execute(
                    """
                    UPDATE turn_generations SET terminal_candidate_id=?,row_version=row_version+1
                    WHERE turn_id=? AND generation=? AND protocol_version=1
                      AND state='recovery_pending' AND row_version=?
                      AND finalization_owner_instance_id=? AND finalization_epoch=?
                      AND terminal_candidate_id IS NULL AND terminal_receipt_id IS NULL
                    """,
                    (
                        plan.candidate_id,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.generation_row_version,
                        context.recovery_owner_instance_id,
                        context.finalization_epoch,
                    ),
                )
                if update.rowcount != 1:
                    raise TerminalProtocolMalformedError("recovery candidate prepare CAS changed")
                next_processing = replace(
                    context,
                    generation_row_version=context.generation_row_version + 1,
                )
                result = PreparedRecoveryContext(
                    processing=next_processing,
                    candidate_id=plan.candidate_id,
                    candidate_projection_digest=plan.candidate_projection_digest,
                )
                self._inject(TerminalProtocolFaultPoint.RECOVERY_AFTER_CANDIDATE_PREPARE)
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def commit_recovery_candidate(
        self,
        context: PreparedRecoveryContext,
        plan: TerminalCandidatePlan,
        *,
        selected_variant_key: str,
        authz_snapshot_digest: str,
        writer_admission_epoch: int,
    ) -> CommittedReceipt:
        """恢复可重用 normal candidate 的 primary/fallback；新 candidate 仅可 interrupt。"""

        if not isinstance(context, PreparedRecoveryContext) or not isinstance(plan, TerminalCandidatePlan):
            raise TypeError("recovery candidate commit input is invalid")
        if (
            plan.scope != context.processing.scope
            or plan.candidate_id != context.candidate_id
            or plan.candidate_projection_digest != context.candidate_projection_digest
        ):
            raise ValueError("recovery candidate commit identity mismatch")
        selected = next((item for item in plan.variants if item.variant_key == selected_variant_key), None)
        if (
            selected is None
            or plan.origin not in {CandidateOrigin.NORMAL, CandidateOrigin.RECOVERY}
            or (
                plan.origin is CandidateOrigin.RECOVERY
                and selected.outcome_variant
                not in {
                    CandidateOutcome.RECOVERY_INTERRUPT,
                    CandidateOutcome.PRIMARY,
                }
            )
        ):
            raise ValueError("recovery commit selected variant is not allowed by candidate origin")
        if not isinstance(authz_snapshot_digest, str) or not authz_snapshot_digest:
            raise ValueError("recovery candidate auth snapshot is invalid")
        if not isinstance(writer_admission_epoch, int) or isinstance(writer_admission_epoch, bool) or writer_admission_epoch < 0:
            raise ValueError("recovery writer admission epoch is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                existing = await _read_committed_receipt(
                    connection,
                    candidate_id=plan.candidate_id,
                    candidate_projection_digest=plan.candidate_projection_digest,
                )
                if existing is not None:
                    if existing.selected_variant_key != selected_variant_key:
                        raise TerminalProtocolMalformedError("terminal receipt selected variant conflict")
                    await connection.rollback()
                    return existing
                now = self._now()
                generation = await _read_generation_claim_row(connection, plan.scope)
                recovery = await _read_active_recovery(connection, context.processing.recovery_id)
                if not _recovery_processing_matches(generation, recovery, context.processing, now):
                    raise TerminalProtocolMalformedError("recovery processing fence changed")
                await _require_effects_safe_for_recovery(
                    connection,
                    plan.scope,
                    allow_cancelled_unknown=bool(
                        generation["closing_reason"] == _CANCELLED_FINALIZATION_REASON and selected.status is TerminalStatus.CANCELLED
                    ),
                )
                stored = await _load_terminal_candidate(connection, plan.scope, plan.candidate_id)
                if stored.candidate_projection_digest != plan.candidate_projection_digest:
                    raise TerminalProtocolMalformedError("recovery candidate projection changed")
                await _validate_selected_v2_effect_disclosure(
                    connection,
                    plan=plan,
                    selected=selected,
                )
                result = await _commit_terminal_candidate(
                    connection,
                    context=context,
                    plan=plan,
                    selected=selected,
                    authz_snapshot_digest=authz_snapshot_digest,
                    writer_admission_epoch=writer_admission_epoch,
                    committed_at=_iso_utc(now),
                    fault_injector=self._inject,
                )
                self._inject(TerminalProtocolFaultPoint.TERMINAL_BEFORE_COMMIT)
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK)
        return result

    async def begin_finalization(
        self,
        context: ExecutionGenerationContext,
        *,
        finalization_owner_instance_id: str,
        closing_reason: str,
    ) -> FinalizationContext:
        """冻结 durable accepted fence 并取得唯一 finalization epoch。"""

        if not isinstance(context, ExecutionGenerationContext):
            raise TypeError("execution generation context is invalid")
        if not isinstance(finalization_owner_instance_id, str) or not finalization_owner_instance_id:
            raise ValueError("finalization owner identity is invalid")
        if not isinstance(closing_reason, str) or not closing_reason or len(closing_reason) > 128:
            raise ValueError("closing reason is invalid")
        result: FinalizationContext
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None:
                    raise TerminalProtocolMalformedError("terminal generation is missing")
                contract = resolve_effect_execution_contract(
                    _parse_payload(row["execution_intent_json"]),
                )
                if contract.disposition is EffectIntentDisposition.RECOVERY_ONLY and row["state"] == GenerationState.RUNNING:
                    await _require_v2_normal_finalization_ready_in_tx(
                        connection,
                        scope=context.scope,
                        generation=row,
                    )
                result, created = await _begin_finalization_in_tx(
                    connection,
                    context=context,
                    finalization_owner_instance_id=finalization_owner_instance_id,
                    closing_reason=closing_reason,
                    now=now,
                )
                if not created:
                    await connection.rollback()
                    return result
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.FINALIZE_AFTER_CLOSING)
        return result

    async def begin_cancelled_finalization_with_local_billing(
        self,
        context: ExecutionGenerationContext | FinalizationContext | RecoveryProcessingContext,
        *,
        finalization_owner_instance_id: str | None = None,
    ) -> FinalizationContext | RecoveryProcessingContext:
        """Atomically freeze Stop, local billing, and the complete v2 effect set."""

        if not isinstance(
            context,
            (
                ExecutionGenerationContext,
                FinalizationContext,
                RecoveryProcessingContext,
            ),
        ):
            raise TypeError("cancelled finalization capability is invalid")
        if isinstance(context, ExecutionGenerationContext):
            if not isinstance(finalization_owner_instance_id, str) or not finalization_owner_instance_id:
                raise ValueError("cancelled finalization owner identity is invalid")
        elif finalization_owner_instance_id is not None:
            raise ValueError("cancelled finalization owner is already frozen")
        result: FinalizationContext | RecoveryProcessingContext
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                generation = await _read_generation_claim_row(
                    connection,
                    context.scope,
                )
                if generation is None:
                    raise TerminalProtocolMalformedError(
                        "cancelled finalization generation is missing",
                    )
                contract = resolve_effect_execution_contract(
                    _parse_payload(generation["execution_intent_json"]),
                )
                if contract.disposition is not EffectIntentDisposition.RECOVERY_ONLY:
                    raise TerminalProtocolMalformedError(
                        "cancelled local billing requires contract-v2",
                    )
                if generation["terminal_candidate_id"] is not None or generation["terminal_receipt_id"] is not None:
                    raise TerminalProtocolMalformedError(
                        "cancelled finalization generation is already sealed",
                    )
                recovery_replay: RecoveryProcessingContext | None = None
                if isinstance(context, ExecutionGenerationContext):
                    assert finalization_owner_instance_id is not None
                    state = _generation_state(generation["state"])
                    replay: FinalizationContext | None = None
                    if state is GenerationState.RUNNING:
                        if not _execution_context_matches(generation, context, now):
                            raise TerminalProtocolMalformedError(
                                "cancelled execution ownership fence changed",
                            )
                        if not await _writer_lease_matches_execution(
                            connection,
                            context=context,
                            now=now,
                        ):
                            raise TerminalProtocolMalformedError(
                                "cancelled execution writer lease is unavailable",
                            )
                    elif state is GenerationState.CLOSING:
                        replay = _finalization_context_from_replay(
                            generation,
                            context=context,
                            finalization_owner_instance_id=(finalization_owner_instance_id),
                            now=now,
                        )
                        if replay is None:
                            raise TerminalProtocolMalformedError(
                                "cancelled generation has another finalization owner",
                            )
                        if generation["closing_reason"] != _CANCELLED_FINALIZATION_REASON:
                            raise TerminalProtocolMalformedError(
                                "cancelled finalization reason changed",
                            )
                    else:
                        raise TerminalProtocolMalformedError(
                            "cancelled execution generation is unavailable",
                        )
                elif isinstance(context, FinalizationContext):
                    if not _finalization_context_matches(
                        generation,
                        context,
                        now,
                    ):
                        raise TerminalProtocolMalformedError(
                            "cancelled finalization ownership fence changed",
                        )
                    if generation["closing_reason"] != _CANCELLED_FINALIZATION_REASON:
                        raise TerminalProtocolMalformedError(
                            "cancelled finalization reason changed",
                        )
                else:
                    recovery = await _read_active_recovery(
                        connection,
                        context.recovery_id,
                    )
                    if _recovery_processing_matches(
                        generation,
                        recovery,
                        context,
                        now,
                    ):
                        if context.cancelled_finalization:
                            recovery_replay = context
                    elif (
                        not context.cancelled_finalization
                        and generation["closing_reason"] == _CANCELLED_FINALIZATION_REASON
                        and generation["row_version"] == context.generation_row_version + 1
                    ):
                        refreshed = replace(
                            context,
                            generation_row_version=(context.generation_row_version + 1),
                            cancelled_finalization=True,
                        )
                        if _recovery_processing_matches(
                            generation,
                            recovery,
                            refreshed,
                            now,
                        ):
                            recovery_replay = refreshed
                        else:
                            raise TerminalProtocolMalformedError(
                                "cancelled recovery ownership fence changed",
                            )
                    else:
                        raise TerminalProtocolMalformedError(
                            "cancelled recovery ownership fence changed",
                        )
                billing = await _freeze_usage_and_claim_billing_in_tx(
                    connection,
                    generation=generation,
                    scope=context.scope,
                    now=now,
                    inject=self._inject,
                )
                if isinstance(context, ExecutionGenerationContext):
                    if generation["state"] == GenerationState.RUNNING:
                        result, created = await _begin_finalization_in_tx(
                            connection,
                            context=context,
                            finalization_owner_instance_id=(finalization_owner_instance_id),
                            closing_reason=_CANCELLED_FINALIZATION_REASON,
                            now=now,
                        )
                        if not created:
                            raise TerminalProtocolMalformedError(
                                "cancelled finalization replay changed",
                            )
                    else:
                        assert replay is not None
                        result = replay
                elif isinstance(context, FinalizationContext):
                    result = context
                elif recovery_replay is not None:
                    result = recovery_replay
                else:
                    marker = await connection.execute(
                        """
                        UPDATE turn_generations
                        SET closing_reason=?,row_version=row_version+1
                        WHERE turn_id=? AND generation=? AND protocol_version=1
                          AND state='recovery_pending' AND row_version=?
                          AND finalization_owner_instance_id=? AND finalization_epoch=?
                          AND terminal_candidate_id IS NULL AND terminal_receipt_id IS NULL
                        """,
                        (
                            _CANCELLED_FINALIZATION_REASON,
                            context.scope.turn_id,
                            context.scope.generation,
                            context.generation_row_version,
                            context.recovery_owner_instance_id,
                            context.finalization_epoch,
                        ),
                    )
                    if marker.rowcount != 1:
                        raise TerminalProtocolMalformedError(
                            "cancelled recovery marker CAS changed",
                        )
                    result = replace(
                        context,
                        generation_row_version=(context.generation_row_version + 1),
                        cancelled_finalization=True,
                    )
                generation = await _read_generation_claim_row(
                    connection,
                    context.scope,
                )
                assert generation is not None
                if billing is not None:
                    await _settle_local_billing_in_tx(
                        connection,
                        generation=generation,
                        attempt=billing,
                        now=now,
                        inject=self._inject,
                    )
                await _freeze_cancelled_effect_set_in_tx(
                    connection,
                    scope=context.scope,
                )
                await _require_v2_cancelled_finalization_ready_in_tx(
                    connection,
                    scope=context.scope,
                    generation=generation,
                )
                self._inject(
                    TerminalProtocolFaultPoint.CANCEL_FINALIZATION_BEFORE_COMMIT,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.FINALIZE_AFTER_CLOSING)
        return result

    async def flush_finalization_through(
        self,
        context: FinalizationContext,
    ) -> TerminalFlushResult:
        """finalizer/takeover 按已冻结 fence materialize durable proposal。"""

        if not isinstance(context, FinalizationContext):
            raise TypeError("finalization context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None or not _finalization_context_matches(row, context, now):
                    raise TerminalProtocolMalformedError("terminal finalization ownership fence changed")
                if row["terminal_candidate_id"] is not None:
                    raise TerminalProtocolMalformedError("terminal generation event materialization is sealed")
                proposals = await _read_event_proposal_prefix(
                    connection,
                    scope=context.scope,
                    accepted_through=context.closing_accepted_through,
                )
                _validate_event_proposal_prefix(
                    proposals,
                    context=context,
                    accepted_through=context.closing_accepted_through,
                )
                self._inject(TerminalProtocolFaultPoint.EVENT_BEFORE_PERSIST)
                event_sequence_next = _positive_int(row["event_sequence_next"], "turns.event_sequence_next")
                tickets = await _materialize_event_proposal_prefix(
                    connection,
                    context=context,
                    proposals=proposals,
                    event_sequence_next=event_sequence_next,
                    persisted_at=_iso_utc(now),
                )
                newly_persisted = sum(proposal["state"] != "persisted" for proposal in proposals)
                result = TerminalFlushResult(
                    closing_accepted_through=context.closing_accepted_through,
                    persisted_event_highwater=event_sequence_next + newly_persisted - 1,
                    tickets=tickets,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.EVENT_AFTER_PERSIST)
        return result

    async def prepare_candidate(
        self,
        context: FinalizationContext,
        plan: TerminalCandidatePlan,
    ) -> PreparedFinalizationContext:
        """以 current finalization epoch 原子 stage immutable normal candidate。"""

        if not isinstance(context, FinalizationContext) or not isinstance(plan, TerminalCandidatePlan):
            raise TypeError("terminal candidate prepare input is invalid")
        if plan.scope != context.scope or plan.origin is not CandidateOrigin.NORMAL:
            raise ValueError("normal terminal candidate identity is invalid")
        if plan.closing_accepted_through != context.closing_accepted_through:
            raise ValueError("terminal candidate accepted fence mismatch")
        _validate_candidate_plan_envelopes(plan)
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                row = await _read_generation_claim_row(connection, context.scope)
                if row is None:
                    raise TerminalProtocolMalformedError("terminal generation is missing")
                if row["terminal_candidate_id"] is not None:
                    prepared = await _read_prepared_candidate_context(
                        connection,
                        row=row,
                        context=context,
                        plan=plan,
                    )
                    await connection.rollback()
                    return prepared
                if not _finalization_context_matches(row, context, now):
                    raise TerminalProtocolMalformedError("terminal finalization ownership fence changed")
                highwater = _positive_int(row["event_sequence_next"], "turns.event_sequence_next") - 1
                if plan.persisted_event_highwater != highwater:
                    raise TerminalProtocolMalformedError("terminal candidate persisted highwater changed")
                proposals = await _read_event_proposal_prefix(
                    connection,
                    scope=context.scope,
                    accepted_through=context.closing_accepted_through,
                )
                _validate_event_proposal_prefix(
                    proposals,
                    context=context,
                    accepted_through=context.closing_accepted_through,
                )
                if any(proposal["state"] != "persisted" for proposal in proposals):
                    raise TerminalProtocolMalformedError("terminal candidate accepted fence is not persisted")
                await _validate_precommitted_answers(connection, plan)
                await _validate_candidate_effect_disclosure(connection, plan)
                self._inject(TerminalProtocolFaultPoint.CANDIDATE_BEFORE_PREPARE_CAS)
                await _insert_terminal_candidate(
                    connection,
                    context=context,
                    plan=plan,
                    prepared_at=_iso_utc(now),
                )
                update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET terminal_candidate_id=?,row_version=row_version+1
                    WHERE turn_id=? AND generation=? AND protocol_version=1 AND state='closing'
                      AND row_version=? AND finalization_owner_instance_id=? AND finalization_epoch=?
                      AND terminal_candidate_id IS NULL AND terminal_receipt_id IS NULL
                    """,
                    (
                        plan.candidate_id,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.generation_row_version,
                        context.finalization_owner_instance_id,
                        context.finalization_epoch,
                    ),
                )
                if update.rowcount != 1:
                    raise TerminalProtocolMalformedError("terminal candidate prepare CAS changed")
                prepared = PreparedFinalizationContext(
                    finalization=FinalizationContext(
                        scope=context.scope,
                        execution_owner_instance_id=context.execution_owner_instance_id,
                        execution_claim_epoch=context.execution_claim_epoch,
                        finalization_owner_instance_id=context.finalization_owner_instance_id,
                        finalization_epoch=context.finalization_epoch,
                        generation_row_version=context.generation_row_version + 1,
                        closing_accepted_through=context.closing_accepted_through,
                        lease_expires_at=context.lease_expires_at,
                    ),
                    candidate_id=plan.candidate_id,
                    candidate_projection_digest=plan.candidate_projection_digest,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.CANDIDATE_AFTER_PREPARE)
        return prepared

    async def commit_candidate(
        self,
        context: PreparedFinalizationContext,
        plan: TerminalCandidatePlan,
        *,
        selected_variant_key: str | None,
        authz_snapshot_digest: str,
        writer_admission_epoch: int,
        context_publication: StagedTerminalContext | None = None,
    ) -> CommittedReceipt:
        """在一个 transaction 中提交 selected terminal batch、receipt 与 Session ledger。"""

        if not isinstance(context, PreparedFinalizationContext) or not isinstance(plan, TerminalCandidatePlan):
            raise TypeError("terminal candidate commit input is invalid")
        if (
            plan.scope != context.finalization.scope
            or plan.candidate_id != context.candidate_id
            or plan.candidate_projection_digest != context.candidate_projection_digest
        ):
            raise ValueError("terminal candidate commit identity mismatch")
        if selected_variant_key is not None and (not isinstance(selected_variant_key, str) or not selected_variant_key):
            raise ValueError("terminal candidate selected variant is invalid")
        if not isinstance(authz_snapshot_digest, str) or not authz_snapshot_digest:
            raise ValueError("terminal candidate auth snapshot is invalid")
        if not isinstance(writer_admission_epoch, int) or isinstance(writer_admission_epoch, bool) or writer_admission_epoch < 0:
            raise ValueError("terminal candidate writer admission epoch is invalid")
        result: CommittedReceipt
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                existing = await _read_committed_receipt(
                    connection,
                    candidate_id=plan.candidate_id,
                    candidate_projection_digest=plan.candidate_projection_digest,
                )
                if existing is not None:
                    if selected_variant_key is not None and existing.selected_variant_key != selected_variant_key:
                        raise TerminalProtocolMalformedError("terminal receipt selected variant conflict")
                    await connection.rollback()
                    return existing
                now = self._now()
                row = await _read_generation_claim_row(connection, plan.scope)
                if row is None or not _finalization_context_matches(row, context.finalization, now):
                    raise TerminalProtocolMalformedError("terminal finalization ownership fence changed")
                if row["terminal_candidate_id"] != plan.candidate_id:
                    raise TerminalProtocolMalformedError("terminal generation candidate identity changed")
                await _validate_stored_candidate(connection, context=context, plan=plan)
                selected = (
                    _select_normal_candidate_variant(row, plan)
                    if selected_variant_key is None
                    else next(
                        (item for item in plan.variants if item.variant_key == selected_variant_key),
                        None,
                    )
                )
                if selected is None or selected.outcome_variant is not CandidateOutcome.PRIMARY:
                    raise ValueError("normal finalization can only select a frozen primary variant")
                await _validate_selected_v2_effect_disclosure(
                    connection,
                    plan=plan,
                    selected=selected,
                )
                result = await _commit_terminal_candidate(
                    connection,
                    context=context,
                    plan=plan,
                    selected=selected,
                    authz_snapshot_digest=authz_snapshot_digest,
                    writer_admission_epoch=writer_admission_epoch,
                    committed_at=_iso_utc(now),
                    fault_injector=self._inject,
                    context_publication=context_publication,
                )
                self._inject(TerminalProtocolFaultPoint.TERMINAL_BEFORE_COMMIT)
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        self._inject(TerminalProtocolFaultPoint.TERMINAL_AFTER_COMMIT_BEFORE_ACK)
        return result

    async def confirm_commit(
        self,
        *,
        candidate_id: str,
        candidate_projection_digest: str,
    ) -> CommitConfirmation:
        """DB 可达时只返回 committed 或 confirmed absent；连接错误由 caller 映射 unknown。"""

        if any(not isinstance(value, str) or not value for value in (candidate_id, candidate_projection_digest)):
            raise ValueError("terminal commit confirmation identity is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            try:
                receipt = await _read_committed_receipt(
                    connection,
                    candidate_id=candidate_id,
                    candidate_projection_digest=candidate_projection_digest,
                )
                if receipt is not None:
                    await connection.rollback()
                    return CommitConfirmation(
                        disposition=CommitConfirmationDisposition.COMMITTED,
                        candidate_id=candidate_id,
                        candidate_projection_digest=candidate_projection_digest,
                        receipt=receipt,
                    )
                async with connection.execute(
                    """
                    SELECT candidate_projection_digest,state FROM turn_terminal_candidates
                    WHERE candidate_id=?
                    """,
                    (candidate_id,),
                ) as cursor:
                    candidate = await cursor.fetchone()
                if candidate is None:
                    raise TerminalProtocolMalformedError("terminal commit candidate is missing")
                if candidate["candidate_projection_digest"] != candidate_projection_digest:
                    raise TerminalProtocolMalformedError("terminal commit candidate digest conflict")
                if candidate["state"] != "prepared":
                    raise TerminalProtocolMalformedError("terminal commit candidate state is malformed")
                await connection.rollback()
                return CommitConfirmation(
                    disposition=CommitConfirmationDisposition.ABSENT,
                    candidate_id=candidate_id,
                    candidate_projection_digest=candidate_projection_digest,
                )
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def commit_protocol_delete(
        self,
        context: TerminalDeleteContext,
    ) -> ProtocolDeleteResult:
        """以 content-free fenced_deleted 收敛未提交 generation；绝不伪造 receipt。"""

        if not isinstance(context, TerminalDeleteContext):
            raise TypeError("terminal delete context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                existing = await _read_protocol_delete_result(connection, context)
                if existing is not None:
                    await connection.rollback()
                    return existing
                row = await _read_terminal_commit_row(connection, context.scope)
                if row is None:
                    raise TerminalProtocolMalformedError("terminal delete generation is missing")
                if row["terminal_receipt_id"] is not None or row["current_terminal_receipt_id"] is not None:
                    raise TerminalProtocolMalformedError("committed terminal truth cannot be protocol-deleted")
                if (
                    row["row_version"] != context.expected_generation_row_version
                    or row["terminal_candidate_id"] != context.expected_candidate_id
                    or row["delivery_fence_epoch"] != context.expected_delivery_fence_epoch
                    or row["state"] == GenerationState.CLOSED
                ):
                    raise TerminalProtocolMalformedError("terminal delete fence changed")
                async with connection.execute(
                    """
                    SELECT COUNT(*) FROM turn_delivery_intents d
                    JOIN turn_terminal_receipts r ON r.receipt_id=d.receipt_id
                    WHERE r.turn_id=? AND r.generation=? AND d.status IN ('sending','unknown')
                    """,
                    (context.scope.turn_id, context.scope.generation),
                ) as cursor:
                    delivery_row = await cursor.fetchone()
                if delivery_row is not None and int(delivery_row[0]) != 0:
                    raise TerminalProtocolMalformedError("terminal delete is blocked by unresolved delivery")
                async with connection.execute(
                    """
                    SELECT attempt_id,state FROM turn_effect_attempts
                    WHERE turn_id=? AND generation=? ORDER BY attempt_id
                    """,
                    (context.scope.turn_id, context.scope.generation),
                ) as cursor:
                    effect_rows = await cursor.fetchall()
                unsettled = sum(1 for effect in effect_rows if effect["state"] != EffectState.SETTLED)
                old_active = _active_claim_projection(row)
                old_recovery = {
                    "turn_id": row["recovery_lock_turn_id"],
                    "generation": row["recovery_lock_generation"],
                    "reason": row["recovery_lock_reason"],
                }
                new_delivery_fence_epoch = context.expected_delivery_fence_epoch + 1
                projection = {
                    "actor_audit_ref": context.actor_audit_ref,
                    "authz_snapshot_digest": context.authz_snapshot_digest,
                    "candidate_id": context.expected_candidate_id,
                    "deletion_id": context.deletion_id,
                    "effect_audit_key_version": context.effect_audit_key_version,
                    "effect_bundle_digest": context.effect_bundle_digest,
                    "effect_identity_digest": context.effect_identity_digest,
                    "effect_key_required_until": context.effect_key_required_until,
                    "generation": context.scope.generation,
                    "new_delivery_fence_epoch": new_delivery_fence_epoch,
                    "old_session_active_tuple": old_active,
                    "old_session_recovery_tuple": old_recovery,
                    "policy_code": context.policy_code,
                    "prior_execution_claim_epoch": row["claim_epoch"],
                    "prior_finalization_epoch": row["finalization_epoch"],
                    "prior_generation_state": row["state"],
                    "prior_row_version": row["row_version"],
                    "resolution": "fenced_deleted",
                    "turn_id": context.scope.turn_id,
                    "unsettled_effect_count": unsettled,
                }
                deletion_digest = canonical_digest_v1(projection)
                now_text = _iso_utc(self._now())
                await connection.execute(
                    """
                    INSERT INTO turn_terminal_deletion_ledgers(
                      deletion_id,turn_id,generation,prior_generation_state,prior_row_version,
                      prior_execution_claim_epoch,prior_finalization_epoch,candidate_id,receipt_id,
                      old_session_active_tuple,old_session_recovery_tuple,new_delivery_fence_epoch,
                      unsettled_effect_count,effect_identity_digest,effect_bundle_digest,
                      effect_audit_key_version,effect_key_required_until,resolution,actor_audit_ref,
                      authz_snapshot_digest,policy_code,created_at,deletion_digest
                    ) VALUES (?,?,?,?,?,?,?, ?,NULL, ?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        context.deletion_id,
                        context.scope.turn_id,
                        context.scope.generation,
                        row["state"],
                        row["row_version"],
                        row["claim_epoch"],
                        row["finalization_epoch"],
                        context.expected_candidate_id,
                        canonical_json_v1(old_active),
                        canonical_json_v1(old_recovery),
                        new_delivery_fence_epoch,
                        unsettled,
                        context.effect_identity_digest,
                        context.effect_bundle_digest,
                        context.effect_audit_key_version,
                        context.effect_key_required_until,
                        "fenced_deleted",
                        context.actor_audit_ref,
                        context.authz_snapshot_digest,
                        context.policy_code,
                        now_text,
                        deletion_digest,
                    ),
                )
                self._inject(TerminalProtocolFaultPoint.DELETE_AFTER_LEDGER)
                generation_update = await connection.execute(
                    """
                    UPDATE turn_generations
                    SET state='closed',row_version=row_version+1,claim_epoch=claim_epoch+1,
                        finalization_epoch=finalization_epoch+1,closed_at=?,
                        closing_accepted_through=COALESCE(
                          closing_accepted_through,accepted_proposal_next-1
                        ),
                        recovery_resolution='fenced_deleted',finalization_owner_instance_id=NULL,
                        finalization_lease_expires_at=NULL
                    WHERE turn_id=? AND generation=? AND protocol_version=1 AND row_version=?
                      AND terminal_candidate_id IS ? AND terminal_receipt_id IS NULL
                      AND state IN ('opened','running','closing','recovery_pending')
                    """,
                    (
                        now_text,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.expected_generation_row_version,
                        context.expected_candidate_id,
                    ),
                )
                if generation_update.rowcount != 1:
                    raise TerminalProtocolMalformedError("terminal delete generation CAS changed")
                self._inject(TerminalProtocolFaultPoint.DELETE_AFTER_GENERATION_CAS)
                if context.expected_candidate_id is not None:
                    candidate_update = await connection.execute(
                        """
                        UPDATE turn_terminal_candidates SET state='fenced_deleted'
                        WHERE candidate_id=? AND state='prepared'
                        """,
                        (context.expected_candidate_id,),
                    )
                    if candidate_update.rowcount != 1:
                        raise TerminalProtocolMalformedError("terminal delete candidate CAS changed")
                await connection.execute(
                    """
                    UPDATE turn_recovery_records
                    SET state='resolved',phase='resolved',resolution='fenced_deleted',
                        resolved_at=?,recovery_lease_expires_at=NULL
                    WHERE turn_id=? AND generation=? AND scope='terminal' AND state='active'
                    """,
                    (now_text, context.scope.turn_id, context.scope.generation),
                )
                session_update = await connection.execute(
                    """
                    UPDATE sessions
                    SET active_turn_id=CASE WHEN active_turn_id=? THEN NULL ELSE active_turn_id END,
                        active_turn_protocol_version=CASE WHEN active_turn_id=? THEN NULL ELSE active_turn_protocol_version END,
                        active_turn_generation=CASE WHEN active_turn_id=? THEN NULL ELSE active_turn_generation END,
                        active_turn_claim_epoch=CASE WHEN active_turn_id=? THEN NULL ELSE active_turn_claim_epoch END,
                        recovery_lock_turn_id=NULL,recovery_lock_generation=NULL,recovery_lock_reason=NULL,
                        steer_freeze_turn_id=NULL,steer_freeze_generation=NULL,
                        steer_freeze_claim_epoch=NULL,steer_frozen_revision=NULL,
                        steer_buffer_state=CASE WHEN steer_buffer_state='frozen' THEN 'deferred' ELSE steer_buffer_state END
                    WHERE id=? AND (recovery_lock_turn_id IS NULL OR recovery_lock_turn_id=?)
                      AND (steer_freeze_turn_id IS NULL OR steer_freeze_turn_id=?)
                    """,
                    (
                        context.scope.turn_id,
                        context.scope.turn_id,
                        context.scope.turn_id,
                        context.scope.turn_id,
                        context.scope.session_id,
                        context.scope.turn_id,
                        context.scope.turn_id,
                    ),
                )
                if session_update.rowcount != 1:
                    raise TerminalProtocolMalformedError("terminal delete Session fence changed")
                turn_update = await connection.execute(
                    """
                    UPDATE turns SET delivery_fence_epoch=?,delivery_fenced_at=?
                    WHERE id=? AND terminal_protocol_version=1 AND current_generation=?
                      AND delivery_fence_epoch=? AND current_terminal_receipt_id IS NULL
                    """,
                    (
                        new_delivery_fence_epoch,
                        now_text,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.expected_delivery_fence_epoch,
                    ),
                )
                if turn_update.rowcount != 1:
                    raise TerminalProtocolMalformedError("terminal delete delivery fence CAS changed")
                self._inject(TerminalProtocolFaultPoint.DELETE_BEFORE_COMMIT)
                result = ProtocolDeleteResult(
                    scope=context.scope,
                    deletion_id=context.deletion_id,
                    deletion_digest=deletion_digest,
                    new_delivery_fence_epoch=new_delivery_fence_epoch,
                    unsettled_effect_count=unsettled,
                    created=True,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def claim_evidence_purge(
        self,
        *,
        purge_owner: str,
        limit: int = 100,
    ) -> tuple[EvidencePurgeContext, ...]:
        """Purge expired local evidence, then claim external locator deletes."""

        if not isinstance(purge_owner, str) or not purge_owner:
            raise ValueError("evidence purge owner is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("evidence purge limit is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now = self._now()
                now_text = _iso_utc(now)
                lease_expires = _iso_utc(now + timedelta(seconds=_RECOVERY_LEASE_SECONDS))
                # Some exact settlements (notably approval projection) have no
                # external evidence object to delete. Their retention is still
                # Store-owned: after TTL, record a deterministic local no-op
                # purge proof so aggregate retention cannot remain blocked.
                async with connection.execute(
                    """
                    SELECT evidence_link_id,team_id,object_version_digest
                    FROM turn_effect_evidence_links
                    WHERE audit_object_ref IS NULL AND purge_after<=?
                      AND retention_state='active'
                    ORDER BY purge_after,evidence_link_id LIMIT ?
                    """,
                    (now_text, limit),
                ) as cursor:
                    local_rows = await cursor.fetchall()
                for row in local_rows:
                    request_id = f"{row['evidence_link_id']}:{row['object_version_digest']}"
                    outcome_digest = canonical_digest_v1(
                        {
                            "evidence_link_id": row["evidence_link_id"],
                            "object_version_digest": row["object_version_digest"],
                            "outcome": "no_external_locator",
                            "purge_request_id": request_id,
                            "team_id": row["team_id"],
                        }
                    )
                    update = await connection.execute(
                        """
                        UPDATE turn_effect_evidence_links
                        SET retention_state='purged',purge_request_id=?,
                            purge_owner='local_retention',purged_at=?,
                            delete_idempotency_key=?,delete_outcome_digest=?
                        WHERE evidence_link_id=?
                          AND object_version_digest=?
                          AND audit_object_ref IS NULL
                          AND retention_state='active' AND purge_after<=?
                        """,
                        (
                            request_id,
                            now_text,
                            request_id,
                            outcome_digest,
                            row["evidence_link_id"],
                            row["object_version_digest"],
                            now_text,
                        ),
                    )
                    if update.rowcount != 1:
                        raise TerminalProtocolMalformedError("local evidence purge fence changed")
                async with connection.execute(
                    """
                    SELECT * FROM turn_effect_evidence_links
                    WHERE audit_object_ref IS NOT NULL AND purge_after<=?
                      AND (retention_state='active' OR
                           (retention_state='purge_pending' AND purge_lease_expires_at<=?))
                    ORDER BY purge_after,evidence_link_id LIMIT ?
                    """,
                    (now_text, now_text, limit),
                ) as cursor:
                    rows = await cursor.fetchall()
                result: list[EvidencePurgeContext] = []
                for row in rows:
                    request_id = f"{row['evidence_link_id']}:{row['object_version_digest']}"
                    next_epoch = _nonnegative_int(row["purge_lease_epoch"], "evidence.purge_lease_epoch") + 1
                    update = await connection.execute(
                        """
                        UPDATE turn_effect_evidence_links
                        SET retention_state='purge_pending',purge_request_id=?,purge_owner=?,
                            purge_lease_epoch=?,purge_lease_expires_at=?,delete_idempotency_key=?
                        WHERE evidence_link_id=? AND object_version_digest=?
                          AND (retention_state='active' OR
                               (retention_state='purge_pending' AND purge_lease_expires_at<=?))
                        """,
                        (
                            request_id,
                            purge_owner,
                            next_epoch,
                            lease_expires,
                            request_id,
                            row["evidence_link_id"],
                            row["object_version_digest"],
                            now_text,
                        ),
                    )
                    if update.rowcount != 1:
                        continue
                    result.append(
                        EvidencePurgeContext(
                            evidence_link_id=_required_text(row["evidence_link_id"], "evidence.evidence_link_id"),
                            team_id=_required_text(row["team_id"], "evidence.team_id"),
                            object_version_digest=_required_text(
                                row["object_version_digest"],
                                "evidence.object_version_digest",
                            ),
                            purge_request_id=request_id,
                            purge_owner=purge_owner,
                            purge_lease_epoch=next_epoch,
                            purge_lease_expires_at=lease_expires,
                            audit_object_ref=_required_text(row["audit_object_ref"], "evidence.audit_object_ref"),
                        )
                    )
                self._inject(TerminalProtocolFaultPoint.EVIDENCE_PURGE_AFTER_CLAIM)
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return tuple(result)

    async def commit_evidence_purge(
        self,
        receipt: EvidencePurgeReceipt,
    ) -> EvidencePurgeContext:
        """只接受 adapter 已验签且 exact identity/version 匹配的删除回执。"""

        if not isinstance(receipt, EvidencePurgeReceipt):
            raise TypeError("evidence purge receipt is invalid")
        context = receipt.context
        expected_signed_projection = {
            "delete_outcome_digest": receipt.delete_outcome_digest,
            "evidence_link_id": context.evidence_link_id,
            "object_version_digest": context.object_version_digest,
            "outcome": receipt.outcome,
            "purge_request_id": context.purge_request_id,
            "team_id": context.team_id,
        }
        if canonical_digest_v1(expected_signed_projection) != receipt.signed_receipt_digest:
            raise ValueError("evidence purge signed receipt digest is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                async with connection.execute(
                    "SELECT * FROM turn_effect_evidence_links WHERE evidence_link_id=?",
                    (context.evidence_link_id,),
                ) as cursor:
                    current = await cursor.fetchone()
                if current is None:
                    raise TerminalProtocolMalformedError("evidence purge target is missing")
                if current["retention_state"] == "purged":
                    if (
                        current["team_id"] != context.team_id
                        or current["object_version_digest"] != context.object_version_digest
                        or current["purge_request_id"] != context.purge_request_id
                        or current["purge_owner"] != context.purge_owner
                        or current["purge_lease_epoch"] != context.purge_lease_epoch
                        or current["delete_idempotency_key"] != context.purge_request_id
                        or current["delete_outcome_digest"] != receipt.delete_outcome_digest
                        or current["audit_object_ref"] is not None
                        or current["purged_at"] is None
                    ):
                        raise TerminalProtocolMalformedError("evidence purge replay identity changed")
                    await connection.rollback()
                    return context
                now = self._now()
                if not _timestamp_after(context.purge_lease_expires_at, now):
                    raise TerminalProtocolMalformedError("evidence purge lease expired")
                now_text = _iso_utc(now)
                update = await connection.execute(
                    """
                    UPDATE turn_effect_evidence_links
                    SET retention_state='purged',audit_object_ref=NULL,purged_at=?,
                        delete_outcome_digest=?,purge_lease_expires_at=NULL
                    WHERE evidence_link_id=? AND team_id=? AND object_version_digest=?
                      AND retention_state='purge_pending' AND purge_request_id=?
                      AND purge_owner=? AND purge_lease_epoch=?
                      AND purge_lease_expires_at=? AND delete_idempotency_key=?
                    """,
                    (
                        now_text,
                        receipt.delete_outcome_digest,
                        context.evidence_link_id,
                        context.team_id,
                        context.object_version_digest,
                        context.purge_request_id,
                        context.purge_owner,
                        context.purge_lease_epoch,
                        context.purge_lease_expires_at,
                        context.purge_request_id,
                    ),
                )
                if update.rowcount != 1:
                    raise TerminalProtocolMalformedError("evidence purge fence changed")
                self._inject(TerminalProtocolFaultPoint.EVIDENCE_PURGE_BEFORE_COMMIT)
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return context

    async def aggregate_effect_retention(
        self,
        context: EffectRetentionContext,
    ) -> EffectRetentionResult:
        """在 deferred-FK transaction 中以 tombstone 替换完整 settled effect bundle。"""

        if not isinstance(context, EffectRetentionContext):
            raise TypeError("effect retention context is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                replay = await _read_effect_retention_result(connection, context)
                if replay is not None:
                    await connection.rollback()
                    return replay
                generation = await _read_generation_claim_row(connection, context.scope)
                if generation is None or generation["state"] != GenerationState.CLOSED:
                    raise TerminalProtocolMalformedError("effect retention generation is not sealed")
                execution_contract = resolve_effect_execution_contract(_parse_payload(generation["execution_intent_json"]))
                async with connection.execute(
                    """
                    SELECT effect_bundle_digest,effect_audit_key_version
                    FROM turn_terminal_deletion_ledgers
                    WHERE turn_id=? AND generation=?
                    """,
                    (context.scope.turn_id, context.scope.generation),
                ) as cursor:
                    deletion = await cursor.fetchone()
                if deletion is not None and (
                    deletion["effect_bundle_digest"] != context.effect_bundle_digest
                    or deletion["effect_audit_key_version"] != context.audit_key_version
                ):
                    raise TerminalProtocolMalformedError("effect retention deletion ledger changed")
                async with connection.execute(
                    """
                    SELECT a.*,
                           s.settlement_id AS persisted_settlement_id,
                           s.source AS settlement_source,s.outcome,
                           s.settlement_code,s.actor_audit_ref,
                           s.evidence_link_id,s.evidence_digest,
                           s.provider_external_ref_digest,
                           s.provider_external_ref_key_version,
                           s.created_at AS settlement_created_at,
                           e.team_id AS evidence_team_id,
                           e.evidence_digest AS linked_evidence_digest,
                           e.object_version_digest,e.audit_object_ref,
                           e.retention_state AS evidence_retention_state,
                           e.purge_after AS evidence_purge_after,
                           e.purge_request_id,
                           e.created_at AS evidence_created_at
                    FROM turn_effect_attempts a
                    LEFT JOIN turn_effect_settlements s ON s.attempt_id=a.attempt_id
                    LEFT JOIN turn_effect_evidence_links e ON e.evidence_link_id=s.evidence_link_id
                    WHERE a.turn_id=? AND a.generation=? ORDER BY a.attempt_id
                    """,
                    (context.scope.turn_id, context.scope.generation),
                ) as cursor:
                    attempts = await cursor.fetchall()
                if not attempts or len(attempts) != len(context.members):
                    raise TerminalProtocolMalformedError("effect retention member count changed")
                if any(
                    row["state"] != EffectState.SETTLED
                    or row["retention_state"] != "active"
                    or row["evidence_retention_state"] != "purged"
                    or row["audit_object_ref"] is not None
                    for row in attempts
                ):
                    raise TerminalProtocolMalformedError("effect retention bundle is not eligible")
                if execution_contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
                    for row in attempts:
                        contract = get_effect_provider_contract(
                            _required_text(
                                row["provider"],
                                "effect retention provider",
                            )
                        )
                        if row["evidence_team_id"] != context.scope.team_id or row["linked_evidence_digest"] != row["evidence_digest"]:
                            raise TerminalProtocolMalformedError("effect retention evidence binding changed")
                        _validate_v2_effect_control_evidence(
                            row,
                            contract.key,
                        )
                    async with connection.execute(
                        """
                        SELECT r.receipt_id,c.projection_extensions_json
                        FROM turn_terminal_receipts r
                        JOIN turn_terminal_candidates c
                          ON c.candidate_id=r.candidate_id
                        WHERE r.turn_id=? AND r.generation=?
                        """,
                        (context.scope.turn_id, context.scope.generation),
                    ) as cursor:
                        receipt = await cursor.fetchone()
                    if receipt is None:
                        raise TerminalProtocolMalformedError("effect retention receipt is missing")
                    frozen = _effect_disclosure_snapshot_from_extensions(
                        context.scope,
                        receipt["projection_extensions_json"],
                    )
                    if frozen is None:
                        raise TerminalProtocolMalformedError("effect retention frozen disclosure is missing")
                    current = await _read_effect_disclosure_snapshot_v2(
                        connection,
                        context.scope,
                    )
                    _validate_effect_disclosure_monotonic(frozen, current)
                    if frozen.unsettled_count:
                        final_overlay = await _read_final_effect_overlay_event(
                            connection,
                            scope=context.scope,
                            receipt_id=_required_text(
                                receipt["receipt_id"],
                                "effect retention receipt",
                            ),
                            frozen=frozen,
                        )
                        _validate_final_effect_overlay_snapshot(
                            final_overlay,
                            current,
                            error="effect retention final overlay proof changed",
                        )
                async with connection.execute(
                    """
                    SELECT 1 FROM turn_recovery_records
                    WHERE turn_id=? AND generation=? AND state='active' LIMIT 1
                    """,
                    (context.scope.turn_id, context.scope.generation),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise TerminalProtocolMalformedError("effect retention has active recovery")
                async with connection.execute(
                    """
                    SELECT 1 FROM turn_delivery_intents d
                    JOIN turn_terminal_receipts r ON r.receipt_id=d.receipt_id
                    WHERE r.turn_id=? AND r.generation=?
                      AND d.status IN ('pending','leased','sending','unknown') LIMIT 1
                    """,
                    (context.scope.turn_id, context.scope.generation),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise TerminalProtocolMalformedError("effect retention has active delivery")
                now = self._now()
                now_text = _iso_utc(now)
                if _timestamp_after(context.eligible_at, now):
                    raise TerminalProtocolMalformedError("effect retention TTL has not elapsed")
                if not _timestamp_after(context.expires_at, now):
                    raise TerminalProtocolMalformedError("effect retention tombstone is already expired")
                provider_digests = sorted(member.provider_token_digest for member in context.members if member.provider_token_digest is not None)
                outcomes = sorted(str(row["outcome"]) for row in attempts)
                if (
                    canonical_digest_v1(provider_digests) != context.provider_token_digests_digest
                    or canonical_digest_v1(outcomes) != context.settlement_outcomes_digest
                ):
                    raise TerminalProtocolMalformedError("effect retention aggregate digest changed")
                canonical_members = sorted(
                    (
                        {
                            "attempt_identity_digest": member.attempt_identity_digest,
                            "provider_token_digest": member.provider_token_digest,
                        }
                        for member in context.members
                    ),
                    key=canonical_json_v1,
                )
                expected_bundle_digest = canonical_digest_v1(
                    {
                        "audit_key_version": context.audit_key_version,
                        "generation": context.scope.generation,
                        "members": canonical_members,
                        "team_id": context.scope.team_id,
                        "turn_id": context.scope.turn_id,
                    }
                )
                if expected_bundle_digest != context.effect_bundle_digest:
                    raise TerminalProtocolMalformedError("effect retention bundle digest is invalid")
                await connection.execute(
                    """
                    INSERT INTO turn_effect_retention_tombstones(
                      tombstone_id,team_id,turn_id,generation,effect_bundle_digest,
                      audit_key_version,attempt_count,provider_token_digests_digest,
                      settlement_outcomes_digest,state,eligible_at,provider_dedup_horizon_at,
                      expires_at,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,'aggregate_pending',?,?,?,?)
                    """,
                    (
                        context.tombstone_id,
                        context.scope.team_id,
                        context.scope.turn_id,
                        context.scope.generation,
                        context.effect_bundle_digest,
                        context.audit_key_version,
                        len(attempts),
                        context.provider_token_digests_digest,
                        context.settlement_outcomes_digest,
                        context.eligible_at,
                        context.provider_dedup_horizon_at,
                        context.expires_at,
                        now_text,
                    ),
                )
                for member in context.members:
                    await connection.execute(
                        """
                        INSERT INTO turn_effect_retention_member_keys(
                          member_key_id,team_id,attempt_identity_digest,provider_token_digest,
                          audit_key_version,tombstone_id,expires_at
                        ) VALUES (?,?,?,?,?,?,?)
                        """,
                        (
                            member.member_key_id,
                            context.scope.team_id,
                            member.attempt_identity_digest,
                            member.provider_token_digest,
                            context.audit_key_version,
                            context.tombstone_id,
                            context.expires_at,
                        ),
                    )
                self._inject(TerminalProtocolFaultPoint.RETENTION_AFTER_TOMBSTONE)
                attempt_update = await connection.execute(
                    """
                    UPDATE turn_effect_attempts SET retention_state='aggregate_pending'
                    WHERE turn_id=? AND generation=? AND state='settled' AND retention_state='active'
                    """,
                    (context.scope.turn_id, context.scope.generation),
                )
                if attempt_update.rowcount != len(attempts):
                    raise TerminalProtocolMalformedError("effect retention attempt fence changed")
                settlement_ids = [row["settlement_id"] for row in attempts]
                if any(not isinstance(value, str) or not value for value in settlement_ids):
                    raise TerminalProtocolMalformedError("effect retention settlement identity is missing")
                placeholders = ",".join("?" for _ in settlement_ids)
                async with connection.execute(
                    f"SELECT evidence_link_id FROM turn_effect_settlements WHERE settlement_id IN ({placeholders})",
                    tuple(settlement_ids),
                ) as cursor:
                    evidence_links = [row[0] for row in await cursor.fetchall()]
                if len(evidence_links) != len(attempts) or any(not isinstance(value, str) or not value for value in evidence_links):
                    raise TerminalProtocolMalformedError("effect retention evidence identity changed")
                settlement_delete = await connection.execute(
                    f"DELETE FROM turn_effect_settlements WHERE settlement_id IN ({placeholders})",
                    tuple(settlement_ids),
                )
                if settlement_delete.rowcount != len(attempts):
                    raise TerminalProtocolMalformedError("effect retention settlement delete changed")
                attempt_delete = await connection.execute(
                    "DELETE FROM turn_effect_attempts WHERE turn_id=? AND generation=?",
                    (context.scope.turn_id, context.scope.generation),
                )
                if attempt_delete.rowcount != len(attempts):
                    raise TerminalProtocolMalformedError("effect retention attempt delete changed")
                evidence_placeholders = ",".join("?" for _ in evidence_links)
                evidence_delete = await connection.execute(
                    f"DELETE FROM turn_effect_evidence_links WHERE evidence_link_id IN ({evidence_placeholders})",
                    tuple(evidence_links),
                )
                if evidence_delete.rowcount != len(evidence_links):
                    raise TerminalProtocolMalformedError("effect retention evidence delete changed")
                self._inject(TerminalProtocolFaultPoint.RETENTION_AFTER_BUNDLE_DELETE)
                update = await connection.execute(
                    """
                    UPDATE turn_effect_retention_tombstones
                    SET state='aggregated',row_version=row_version+1,aggregated_at=?
                    WHERE tombstone_id=? AND state='aggregate_pending'
                    """,
                    (now_text, context.tombstone_id),
                )
                if update.rowcount != 1:
                    raise TerminalProtocolMalformedError("effect retention tombstone CAS changed")
                result = EffectRetentionResult(
                    scope=context.scope,
                    tombstone_id=context.tombstone_id,
                    effect_bundle_digest=context.effect_bundle_digest,
                    attempt_count=len(attempts),
                    created=True,
                )
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return result

    async def has_effect_retention_key(
        self,
        *,
        team_id: str,
        attempt_identity_digest: str,
        provider_token_digest: str | None,
        audit_key_version: str,
    ) -> bool:
        """Late callback lookup uses only team-scoped HMAC digests, never a raw token."""

        strings = (team_id, attempt_identity_digest, audit_key_version)
        if any(not isinstance(value, str) or not value for value in strings):
            raise ValueError("effect retention lookup identity is invalid")
        if provider_token_digest is not None and (not isinstance(provider_token_digest, str) or not provider_token_digest):
            raise ValueError("effect retention provider token digest is invalid")
        now_text = _iso_utc(self._now())
        async with (
            open_terminal_protocol_connection(self._db_path) as connection,
            connection.execute(
                """
            SELECT 1 FROM turn_effect_retention_member_keys k
            JOIN turn_effect_retention_tombstones t
              ON t.tombstone_id=k.tombstone_id AND t.team_id=k.team_id
            WHERE k.team_id=? AND k.attempt_identity_digest=?
              AND k.provider_token_digest IS ? AND k.audit_key_version=?
              AND k.expires_at>? AND t.expires_at>?
              AND t.state='aggregated' LIMIT 1
            """,
                (
                    team_id,
                    attempt_identity_digest,
                    provider_token_digest,
                    audit_key_version,
                    now_text,
                    now_text,
                ),
            ) as cursor,
        ):
            return await cursor.fetchone() is not None

    async def purge_expired_effect_retention(self, *, limit: int = 100) -> int:
        """Expire callback keys while retaining the content-free history marker."""

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("effect retention purge limit is invalid")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                now_text = _iso_utc(self._now())
                async with connection.execute(
                    """
                    SELECT tombstone_id FROM turn_effect_retention_tombstones
                    WHERE state='aggregated' AND expires_at<=?
                      AND provider_dedup_horizon_at<=?
                      AND EXISTS (
                        SELECT 1 FROM turn_effect_retention_member_keys k
                        WHERE k.tombstone_id=turn_effect_retention_tombstones.tombstone_id
                      )
                    ORDER BY expires_at,tombstone_id LIMIT ?
                    """,
                    (now_text, now_text, limit),
                ) as cursor:
                    rows = await cursor.fetchall()
                tombstone_ids = [row["tombstone_id"] for row in rows]
                if tombstone_ids:
                    placeholders = ",".join("?" for _ in tombstone_ids)
                    delete = await connection.execute(
                        f"""
                        DELETE FROM turn_effect_retention_member_keys
                        WHERE tombstone_id IN ({placeholders}) AND expires_at<=?
                        """,
                        (*tombstone_ids, now_text),
                    )
                    if delete.rowcount < len(tombstone_ids):
                        raise TerminalProtocolMalformedError("effect retention callback key purge changed")
                    deleted = len(tombstone_ids)
                else:
                    deleted = 0
                await connection.commit()
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise
        return deleted

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("terminal protocol clock must return an aware datetime")
        return value.astimezone(UTC)

    def _inject(self, point: TerminalProtocolFaultPoint) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    async def read_protocol_summary(self, context: TerminalReadContext) -> TerminalProtocolSummary:
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT
                  t.id AS turn_id,
                  t.terminal_protocol_version,
                  t.terminal_ingress_surface,
                  t.terminal_admission_cohort,
                  t.protocol_admitted_at,
                  t.current_generation,
                  t.generation_transition_epoch,
                  t.last_terminal_generation,
                  t.current_terminal_receipt_id,
                  t.event_sequence_next,
                  t.delivery_fence_epoch,
                  t.presentation_locale,
                  t.message_catalog_version,
                  g.protocol_version AS generation_protocol_version,
                  g.kind AS generation_kind,
                  g.state AS generation_state,
                  g.closing_reason AS generation_closing_reason,
                  g.recovery_resolution AS generation_recovery_resolution,
                  g.terminal_receipt_id AS generation_receipt_id,
                  r.receipt_id,
                  r.candidate_id AS receipt_candidate_id,
                  r.turn_id AS receipt_turn_id,
                  r.generation AS receipt_generation,
                  r.protocol_version AS receipt_protocol_version,
                  r.status AS receipt_status,
                  r.operation_kind AS receipt_operation_kind,
                  t.operation_kind AS turn_operation_kind,
                  r.reason_code AS receipt_reason_code,
                  r.message_key AS receipt_message_key,
                  r.final_answer_mode AS receipt_final_answer_mode,
                  r.final_answer_event_id AS receipt_final_answer_event_id,
                  r.selected_variant_key AS receipt_selected_variant_key,
                  cv.required_manifest_template_digest AS receipt_required_manifest_template_digest,
                  r.terminal_event_id,
                  r.first_event_seq,
                  r.last_event_seq,
                  r.persisted_event_highwater,
                  r.committed_at AS receipt_committed_at,
                  rr.recovery_id,
                  rr.kind AS recovery_kind,
                  rr.phase AS recovery_phase,
                  rr.state AS recovery_state,
                  rr.last_code AS recovery_last_code,
                  rr.next_attempt_at AS recovery_next_attempt_at
                FROM turns t
                JOIN sessions s ON s.id = t.session_id
                JOIN workspaces w ON w.id = s.workspace_id
                LEFT JOIN turn_generations g
                  ON g.turn_id = t.id AND g.generation = t.current_generation
                LEFT JOIN turn_terminal_receipts r
                  ON r.receipt_id = t.current_terminal_receipt_id
                LEFT JOIN turn_terminal_candidate_variants cv
                  ON cv.candidate_id=r.candidate_id AND cv.variant_key=r.selected_variant_key
                LEFT JOIN turn_recovery_records rr
                  ON rr.turn_id = t.id
                 AND rr.generation = t.current_generation
                 AND rr.scope = 'terminal'
                 AND rr.state = 'active'
                WHERE t.id = ? AND t.session_id = ? AND w.team_id = ?
                """,
                (context.turn_id, context.session_id, context.team_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise LookupError("terminal protocol turn not found")
        return _summary_from_row(row)

    async def read_checkpoint(self, context: TerminalReadContext) -> TerminalCheckpoint:
        """在一个 SQLite snapshot 中读取 pointer、receipt、manifest 与 event highwater。"""

        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN")
            try:
                row = await _read_checkpoint_row(connection, context)
                if row is None:
                    await connection.rollback()
                    raise LookupError("terminal protocol turn not found")
                async with connection.execute(
                    """
                    SELECT COUNT(*),COALESCE(MAX(event_seq),0)
                    FROM turn_events WHERE turn_id=? AND protocol_version=1
                    """,
                    (context.turn_id,),
                ) as cursor:
                    highwater_row = await cursor.fetchone()
                event_count = int(highwater_row[0]) if highwater_row is not None else 0
                persistent_event_highwater = int(highwater_row[1]) if highwater_row is not None else 0
                checkpoint = _checkpoint_from_row(
                    row,
                    context=context,
                    event_count=event_count,
                    persistent_event_highwater=persistent_event_highwater,
                )
                await _validate_checkpoint_dependencies(connection, checkpoint)
                await connection.commit()
                return checkpoint
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def read_generation_pointers(
        self,
        contexts: tuple[TerminalReadContext, ...],
    ) -> dict[str, GenerationPointer]:
        """TransitionWatchMultiplexer 的单批 durable pointer read（最多500 Turn）。"""

        if not contexts:
            return {}
        if len(contexts) > 500:
            raise ValueError("generation pointer batch exceeds 500 turns")
        by_turn: dict[str, TerminalReadContext] = {}
        for context in contexts:
            prior = by_turn.setdefault(context.turn_id, context)
            if prior.team_id != context.team_id or prior.session_id != context.session_id:
                raise ValueError("generation pointer batch contains conflicting turn identity")
        placeholders = ",".join("?" for _ in by_turn)
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                f"""
                SELECT t.id,t.session_id,t.terminal_protocol_version,t.current_generation,
                       t.generation_transition_epoch,t.delivery_fence_epoch,w.team_id,
                       g.protocol_version AS generation_protocol_version
                FROM turns t
                JOIN sessions s ON s.id=t.session_id
                JOIN workspaces w ON w.id=s.workspace_id
                JOIN turn_generations g
                  ON g.turn_id=t.id AND g.generation=t.current_generation
                WHERE t.id IN ({placeholders})
                """,
                tuple(by_turn),
            ) as cursor:
                rows = await cursor.fetchall()
        result: dict[str, GenerationPointer] = {}
        for row in rows:
            expected = by_turn.get(row["id"])
            if expected is None or row["session_id"] != expected.session_id or row["team_id"] != expected.team_id:
                continue
            if row["terminal_protocol_version"] != 1 or row["generation_protocol_version"] != 1:
                raise TerminalProtocolMalformedError("transition watch encountered a non-v1 turn")
            result[row["id"]] = GenerationPointer(
                generation=_positive_int(row["current_generation"], "turns.current_generation"),
                generation_transition_epoch=_positive_int(
                    row["generation_transition_epoch"],
                    "turns.generation_transition_epoch",
                ),
                delivery_fence_epoch=_nonnegative_int(
                    row["delivery_fence_epoch"],
                    "turns.delivery_fence_epoch",
                ),
            )
        if len(result) != len(by_turn):
            raise LookupError("one or more terminal protocol turns were not found")
        self._inject(TerminalProtocolFaultPoint.TRANSITION_WATCH_AFTER_POINTER_READ)
        return result

    async def linearize_subscription_promotion(
        self,
        context: TerminalReadContext,
        *,
        expected_pointer: GenerationPointer,
        resolve_context: AuthFenceResolveContext,
        expected_auth_fence_vector: AuthFenceVector,
        promote: Callable[[TerminalCheckpoint], Callable[[], None]],
    ) -> TerminalCheckpoint | None:
        """持有 SQLite write fence 时验证 C3 的 G/E/F/A 并安装 live watcher。"""

        if context.team_id != resolve_context.team_id or context.session_id != resolve_context.session_id:
            raise ValueError("subscription promotion auth identity mismatch")
        if context.viewer != resolve_context.viewer or context.turn_id != resolve_context.resource_ref:
            raise ValueError("subscription promotion viewer mismatch")
        if context.auth_fence_vector_digest != expected_auth_fence_vector.auth_fence_vector_digest:
            raise ValueError("subscription promotion auth digest mismatch")

        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await _read_checkpoint_row(connection, context)
                if row is None:
                    await connection.rollback()
                    raise LookupError("terminal protocol turn not found")
                async with connection.execute(
                    """
                    SELECT COUNT(*),COALESCE(MAX(event_seq),0)
                    FROM turn_events WHERE turn_id=? AND protocol_version=1
                    """,
                    (context.turn_id,),
                ) as cursor:
                    highwater_row = await cursor.fetchone()
                event_count = int(highwater_row[0]) if highwater_row is not None else 0
                highwater = int(highwater_row[1]) if highwater_row is not None else 0
                checkpoint = _checkpoint_from_row(
                    row,
                    context=context,
                    event_count=event_count,
                    persistent_event_highwater=highwater,
                )
                await _validate_checkpoint_dependencies(connection, checkpoint)
                current_auth_fence_vector = await _read_auth_fence_vector(
                    connection,
                    resolve_context.required_scopes,
                )
                if checkpoint.generation_pointer != expected_pointer or current_auth_fence_vector != expected_auth_fence_vector:
                    await connection.rollback()
                    return None
                undo = promote(checkpoint)
                try:
                    await connection.commit()
                except BaseException:
                    undo()
                    raise
                return checkpoint
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def linearize_frame_enqueue(
        self,
        context: TerminalReadContext,
        *,
        resolve_context: AuthFenceResolveContext,
        expected_pointer: GenerationPointer,
        expected_auth_fence_vector: AuthFenceVector,
        enqueue: Callable[[], bool],
    ) -> bool:
        """在 SQLite write fence 内重验 G/E/F/A，再执行无 await 的 bounded enqueue。"""

        if context.team_id != resolve_context.team_id or context.session_id != resolve_context.session_id:
            raise ValueError("frame enqueue auth identity mismatch")
        if context.viewer != resolve_context.viewer or context.turn_id != resolve_context.resource_ref:
            raise ValueError("frame enqueue viewer mismatch")
        if context.auth_fence_vector_digest != expected_auth_fence_vector.auth_fence_vector_digest:
            raise ValueError("frame enqueue auth digest mismatch")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("BEGIN IMMEDIATE")
            try:
                async with connection.execute(
                    """
                    SELECT t.current_generation,t.generation_transition_epoch,
                           t.delivery_fence_epoch,g.protocol_version AS generation_protocol_version
                    FROM turns t
                    JOIN sessions s ON s.id=t.session_id
                    JOIN workspaces w ON w.id=s.workspace_id
                    JOIN turn_generations g
                      ON g.turn_id=t.id AND g.generation=t.current_generation
                    WHERE t.id=? AND t.session_id=? AND w.team_id=?
                      AND t.terminal_protocol_version=1
                    """,
                    (context.turn_id, context.session_id, context.team_id),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None or row["generation_protocol_version"] != 1:
                    await connection.rollback()
                    return False
                pointer = GenerationPointer(
                    generation=_positive_int(row["current_generation"], "turns.current_generation"),
                    generation_transition_epoch=_positive_int(
                        row["generation_transition_epoch"],
                        "turns.generation_transition_epoch",
                    ),
                    delivery_fence_epoch=_nonnegative_int(
                        row["delivery_fence_epoch"],
                        "turns.delivery_fence_epoch",
                    ),
                )
                vector = await _read_auth_fence_vector(connection, resolve_context.required_scopes)
                if pointer != expected_pointer or vector != expected_auth_fence_vector:
                    await connection.rollback()
                    return False
                enqueued = enqueue()
                # Transaction is DB-writer fence only; enqueue linearizes, no DB writes.
                await connection.rollback()
                return enqueued
            except BaseException:
                if connection.in_transaction:
                    await connection.rollback()
                raise

    async def list_protocol_events(
        self,
        context: TerminalReadContext,
        *,
        after_event_seq: int = 0,
        up_to_event_seq: int | None = None,
    ) -> list[TerminalProtocolEventRow]:
        if after_event_seq < 0:
            raise ValueError("after_event_seq must be non-negative")
        if up_to_event_seq is not None and up_to_event_seq < after_event_seq:
            raise ValueError("up_to_event_seq must not precede after_event_seq")
        summary = await self.read_protocol_summary(context)
        if summary.version is None:
            return []
        checkpoint = await self.read_checkpoint(context)
        persistent_highwater = checkpoint.persistent_event_highwater
        if after_event_seq > persistent_highwater:
            raise ValueError("after_event_seq exceeds persistent highwater")
        upper_bound = persistent_highwater if up_to_event_seq is None else up_to_event_seq
        if upper_bound > persistent_highwater:
            raise ValueError("up_to_event_seq exceeds persistent highwater")
        async with open_terminal_protocol_connection(self._db_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute(
                """
                SELECT e.id, e.turn_id, e.generation, e.event_seq, e.event_type, e.actor_scope,
                       e.phase_scope, e.terminal_receipt_id, e.payload_json, e.payload_digest,
                       e.occurred_at, r.turn_id AS receipt_turn_id,
                       r.generation AS receipt_generation, r.protocol_version AS receipt_protocol_version
                FROM turn_events e
                LEFT JOIN turn_terminal_receipts r ON r.receipt_id = e.terminal_receipt_id
                WHERE e.turn_id = ? AND e.protocol_version = 1
                  AND e.event_seq > ? AND e.event_seq <= ?
                ORDER BY event_seq
                """,
                (context.turn_id, after_event_seq, upper_bound),
            ) as cursor:
                rows = await cursor.fetchall()
        events: list[TerminalProtocolEventRow] = []
        expected = after_event_seq + 1
        for row in rows:
            event_seq = _positive_int(row["event_seq"], "turn_events.event_seq")
            if event_seq != expected:
                raise TerminalProtocolMalformedError("terminal protocol event sequence contains a gap")
            payload = _parse_payload(row["payload_json"])
            payload_digest = _nonempty(row["payload_digest"], "turn_events.payload_digest")
            if canonical_digest_v1(payload) != payload_digest:
                raise TerminalProtocolMalformedError("turn_events.payload_digest does not match payload")
            actor_scope = _enum_value(row["actor_scope"], "actor_scope", "turn_events.actor_scope")
            phase_scope = _enum_value(row["phase_scope"], "phase_scope", "turn_events.phase_scope")
            terminal_receipt_id = row["terminal_receipt_id"]
            if terminal_receipt_id is not None:
                terminal_receipt_id = _nonempty(terminal_receipt_id, "turn_events.terminal_receipt_id")
                if (
                    row["receipt_turn_id"] != context.turn_id
                    or row["receipt_generation"] != row["generation"]
                    or row["receipt_protocol_version"] != 1
                ):
                    raise TerminalProtocolMalformedError("terminal event receipt identity is malformed")
            generation = _positive_int(row["generation"], "turn_events.generation")
            if generation > checkpoint.current_generation:
                raise TerminalProtocolMalformedError("terminal event generation exceeds current generation")
            events.append(
                TerminalProtocolEventRow(
                    event_id=_nonempty(row["id"], "turn_events.id"),
                    turn_id=_nonempty(row["turn_id"], "turn_events.turn_id"),
                    generation=generation,
                    event_seq=event_seq,
                    event_type=_nonempty(row["event_type"], "turn_events.event_type"),
                    actor_scope=actor_scope,
                    phase_scope=phase_scope,
                    terminal_receipt_id=terminal_receipt_id,
                    payload=payload,
                    payload_digest=payload_digest,
                    occurred_at=_nonempty(row["occurred_at"], "turn_events.occurred_at"),
                )
            )
            expected += 1
        if checkpoint.current_generation_resolution != "fenced_deleted" and expected != upper_bound + 1:
            raise TerminalProtocolMalformedError("terminal protocol event sequence is incomplete")
        return events


async def _read_auth_fence_vector(
    connection: aiosqlite.Connection,
    scopes: tuple[AuthFenceScope, ...],
) -> AuthFenceVector:
    entries: list[AuthFenceEpoch] = []
    for scope in sorted(scopes, key=AuthFenceScope.sort_key):
        async with connection.execute(
            """
            SELECT epoch FROM terminal_auth_scope_epochs
            WHERE scope_kind=? AND team_id=? AND scope_ref=?
            """,
            (scope.scope_kind.value, scope.team_id, scope.scope_ref),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise TerminalProtocolMalformedError("required auth fence scope is missing")
        entries.append(
            AuthFenceEpoch(
                scope=scope,
                epoch=_positive_int(row["epoch"], "terminal_auth_scope_epochs.epoch"),
            )
        )
    return AuthFenceVector.build(tuple(entries))


async def _read_checkpoint_row(
    connection: aiosqlite.Connection,
    context: TerminalReadContext,
) -> aiosqlite.Row | None:
    async with connection.execute(
        """
        SELECT
          t.id AS turn_id,t.terminal_protocol_version,t.terminal_ingress_surface,
          t.terminal_admission_cohort,t.protocol_admitted_at,t.current_generation,
          t.generation_transition_epoch,t.last_terminal_generation,t.current_terminal_receipt_id,
          t.event_sequence_next,t.delivery_fence_epoch,t.presentation_locale,t.message_catalog_version,
          g.protocol_version AS generation_protocol_version,g.kind AS generation_kind,
          g.state AS generation_state,
          g.closing_reason AS generation_closing_reason,
          g.recovery_resolution AS generation_recovery_resolution,
          g.terminal_receipt_id AS generation_receipt_id,
          r.receipt_id,r.turn_id AS receipt_turn_id,r.generation AS receipt_generation,
          r.candidate_id AS receipt_candidate_id,
          r.protocol_version AS receipt_protocol_version,r.status AS receipt_status,
                  r.operation_kind AS receipt_operation_kind,
                  t.operation_kind AS turn_operation_kind,
          r.reason_code AS receipt_reason_code,r.message_key AS receipt_message_key,
          r.final_answer_mode AS receipt_final_answer_mode,
          r.final_answer_event_id AS receipt_final_answer_event_id,
          r.selected_variant_key AS receipt_selected_variant_key,
          cv.required_manifest_template_digest AS receipt_required_manifest_template_digest,
          r.terminal_event_id,r.first_event_seq,
          r.last_event_seq,r.persisted_event_highwater,r.required_event_manifest_json,
          r.committed_at AS receipt_committed_at,
          rr.recovery_id,rr.kind AS recovery_kind,rr.phase AS recovery_phase,
          rr.state AS recovery_state,rr.last_code AS recovery_last_code,
          rr.next_attempt_at AS recovery_next_attempt_at,
          s.active_turn_id,s.active_turn_protocol_version,s.active_turn_generation,
          s.active_turn_claim_epoch
        FROM turns t
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        LEFT JOIN turn_generations g ON g.turn_id=t.id AND g.generation=t.current_generation
        LEFT JOIN turn_terminal_receipts r ON r.receipt_id=t.current_terminal_receipt_id
        LEFT JOIN turn_terminal_candidate_variants cv
          ON cv.candidate_id=r.candidate_id AND cv.variant_key=r.selected_variant_key
        LEFT JOIN turn_recovery_records rr
          ON rr.turn_id=t.id AND rr.generation=t.current_generation
         AND rr.scope='terminal' AND rr.state='active'
        WHERE t.id=? AND t.session_id=? AND w.team_id=?
        """,
        (context.turn_id, context.session_id, context.team_id),
    ) as cursor:
        return await cursor.fetchone()


def _checkpoint_from_row(
    row: aiosqlite.Row,
    *,
    context: TerminalReadContext,
    event_count: int,
    persistent_event_highwater: int,
) -> TerminalCheckpoint:
    summary = _summary_from_row(row)
    if summary.version != 1:
        raise TerminalProtocolMalformedError("coherent checkpoint requires a v1 turn")
    assert summary.current_generation is not None
    assert summary.generation_transition_epoch is not None
    assert summary.current_generation_state is not None
    assert summary.event_sequence_next is not None
    assert summary.delivery_fence_epoch is not None
    if summary.current_generation_resolution != "fenced_deleted" and summary.event_sequence_next != persistent_event_highwater + 1:
        raise TerminalProtocolMalformedError("checkpoint event highwater does not match allocator")
    if summary.current_generation_resolution != "fenced_deleted" and event_count != persistent_event_highwater:
        raise TerminalProtocolMalformedError("checkpoint event log contains a sequence gap")
    manifest = _required_event_manifest_from_row(row, receipt=summary.current_receipt)
    session_claim = _session_active_claim_from_row(row)
    token_projection = {
        "auth_fence_vector_digest": context.auth_fence_vector_digest,
        "current_generation": summary.current_generation,
        "current_generation_resolution": summary.current_generation_resolution,
        "current_generation_state": summary.current_generation_state.value,
        "current_receipt_id": summary.current_receipt.receipt_id if summary.current_receipt is not None else None,
        "delivery_fence_epoch": summary.delivery_fence_epoch,
        "generation_transition_epoch": summary.generation_transition_epoch,
        "last_terminal_generation": summary.last_terminal_generation,
        "persistent_event_highwater": persistent_event_highwater,
        "recovery_id": summary.recovery.recovery_id if summary.recovery is not None else None,
        "session_active_claim": (
            {
                "claim_epoch": session_claim.claim_epoch,
                "generation": session_claim.generation,
                "protocol_version": session_claim.protocol_version,
                "turn_id": session_claim.turn_id,
            }
            if session_claim is not None
            else None
        ),
        "turn_id": context.turn_id,
    }
    return TerminalCheckpoint(
        protocol_version=1,
        turn_id=context.turn_id,
        current_generation=summary.current_generation,
        generation_transition_epoch=summary.generation_transition_epoch,
        current_generation_state=summary.current_generation_state,
        current_generation_resolution=summary.current_generation_resolution,
        last_terminal_generation=summary.last_terminal_generation,
        persistent_event_highwater=persistent_event_highwater,
        current_receipt_summary=summary.current_receipt,
        required_event_manifest=manifest,
        recovery_summary=summary.recovery,
        session_active_claim=session_claim,
        delivery_fence_epoch=summary.delivery_fence_epoch,
        auth_fence_vector_digest=context.auth_fence_vector_digest,
        checkpoint_token=f"tcp_{canonical_digest_v1(token_projection)}",
    )


def _required_event_manifest_from_row(
    row: aiosqlite.Row,
    *,
    receipt: TerminalReceiptSummary | None,
) -> tuple[RequiredEventSummary, ...]:
    value = row["required_event_manifest_json"]
    if receipt is None:
        if value is not None:
            raise TerminalProtocolMalformedError("open checkpoint unexpectedly carries a required event manifest")
        return ()
    if not isinstance(value, str):
        raise TerminalProtocolMalformedError("receipt required event manifest is malformed")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise TerminalProtocolMalformedError("receipt required event manifest is malformed") from exc
    if not isinstance(parsed, list):
        raise TerminalProtocolMalformedError("receipt required event manifest is malformed")
    result: list[RequiredEventSummary] = []
    prior_seq = 0
    for item in parsed:
        if not isinstance(item, dict):
            raise TerminalProtocolMalformedError("receipt required event manifest is malformed")
        event_seq = _positive_int(item.get("event_seq"), "required_event_manifest.event_seq")
        if event_seq <= prior_seq:
            raise TerminalProtocolMalformedError("receipt required event manifest order is malformed")
        prior_seq = event_seq
        result.append(
            RequiredEventSummary(
                event_id=_required_text(item.get("event_id"), "required_event_manifest.event_id"),
                event_seq=event_seq,
                event_type=_required_text(item.get("event_type"), "required_event_manifest.event_type"),
                actor_scope=_enum_value(
                    item.get("actor_scope"),
                    "actor_scope",
                    "required_event_manifest.actor_scope",
                ),
                phase_scope=_enum_value(
                    item.get("phase_scope"),
                    "phase_scope",
                    "required_event_manifest.phase_scope",
                ),
                canonical_row_digest=_required_text(
                    item.get("canonical_row_digest"),
                    "required_event_manifest.canonical_row_digest",
                ),
            )
        )
    return tuple(result)


async def _validate_checkpoint_dependencies(
    connection: aiosqlite.Connection,
    checkpoint: TerminalCheckpoint,
) -> None:
    receipt = checkpoint.current_receipt_summary
    if receipt is None:
        if checkpoint.required_event_manifest:
            raise TerminalProtocolMalformedError("checkpoint without receipt carries required events")
        return
    if checkpoint.persistent_event_highwater < receipt.last_event_seq:
        raise TerminalProtocolMalformedError("checkpoint terminal receipt exceeds event highwater")
    async with connection.execute(
        """
        SELECT id,event_type,generation,event_seq,actor_scope,phase_scope,
               terminal_receipt_id,payload_json,payload_digest
        FROM turn_events WHERE turn_id=? AND id=? AND protocol_version=1
        """,
        (checkpoint.turn_id, receipt.terminal_event_id),
    ) as cursor:
        terminal = await cursor.fetchone()
    if (
        terminal is None
        or terminal["event_type"] != "done"
        or terminal["generation"] != receipt.generation
        or terminal["event_seq"] != receipt.last_event_seq
        or terminal["terminal_receipt_id"] != receipt.receipt_id
    ):
        raise TerminalProtocolMalformedError("checkpoint terminal event dependency is missing")
    terminal_payload = _parse_payload(terminal["payload_json"])
    if canonical_digest_v1(terminal_payload) != terminal["payload_digest"]:
        raise TerminalProtocolMalformedError("checkpoint terminal event payload digest is malformed")
    terminal_event = TerminalEvent(
        event_type=terminal["event_type"],
        event_id=terminal["id"],
        generation=terminal["generation"],
        actor_scope=terminal["actor_scope"],
        phase_scope=terminal["phase_scope"],
        terminal_receipt_id=terminal["terminal_receipt_id"],
        payload=terminal_payload,
    )
    expected_done_payload = {
        "candidate_id": receipt.candidate_id,
        **({"operation_kind": receipt.operation_kind} if receipt.operation_kind is not None else {}),
        "generation": receipt.generation,
        "message_key": receipt.message_key,
        "protocol": 1,
        "reason": receipt.reason_code,
        "receipt_id": receipt.receipt_id,
        "required_manifest_template_digest": receipt.required_manifest_template_digest,
        "selected_variant_key": receipt.selected_variant_key,
        "status": receipt.status.value,
    }
    if (
        classify_terminal(1, ConsumerProfile.CORE_BUS, terminal_event) is not TerminalClassification.ROOT_COMMITTED
        or terminal_payload != expected_done_payload
    ):
        raise TerminalProtocolMalformedError("checkpoint terminal event envelope is malformed")
    if any(item.event_id == receipt.terminal_event_id for item in checkpoint.required_event_manifest):
        raise TerminalProtocolMalformedError("checkpoint required manifest contains terminal done")
    expected = {item.event_id: item for item in checkpoint.required_event_manifest}
    if len(expected) != len(checkpoint.required_event_manifest):
        raise TerminalProtocolMalformedError("checkpoint required manifest contains duplicate event identity")
    if receipt.final_answer_event_id is not None and receipt.final_answer_event_id not in expected:
        raise TerminalProtocolMalformedError("checkpoint final answer dependency is missing from manifest")
    found: dict[str, aiosqlite.Row] = {}
    event_ids = tuple(expected)
    for start in range(0, len(event_ids), 500):
        chunk = event_ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        async with connection.execute(
            f"""
            SELECT id,generation,event_seq,event_type,actor_scope,phase_scope,payload_json,payload_digest
            FROM turn_events
            WHERE turn_id=? AND protocol_version=1 AND id IN ({placeholders})
            """,
            (checkpoint.turn_id, *chunk),
        ) as cursor:
            for row in await cursor.fetchall():
                found[row["id"]] = row
    if set(found) != set(expected):
        raise TerminalProtocolMalformedError("checkpoint required event dependency is missing")
    for event_id, item in expected.items():
        event = found[event_id]
        if event["generation"] != receipt.generation or event["event_seq"] >= receipt.last_event_seq:
            raise TerminalProtocolMalformedError("checkpoint required event generation or order is malformed")
        payload = _parse_payload(event["payload_json"])
        if canonical_digest_v1(payload) != event["payload_digest"]:
            raise TerminalProtocolMalformedError("checkpoint required event payload digest is malformed")
        row_digest = canonical_digest_v1(
            {
                "actor_scope": event["actor_scope"],
                "event_id": event["id"],
                "event_seq": event["event_seq"],
                "event_type": event["event_type"],
                "payload_digest": event["payload_digest"],
                "phase_scope": event["phase_scope"],
            }
        )
        if (
            event["event_seq"] != item.event_seq
            or event["event_type"] != item.event_type
            or event["actor_scope"] != item.actor_scope
            or event["phase_scope"] != item.phase_scope
            or row_digest != item.canonical_row_digest
        ):
            raise TerminalProtocolMalformedError("checkpoint required event dependency is malformed")


async def read_canonical_closed_generation_outcome(
    connection: aiosqlite.Connection,
    *,
    turn_id: str,
    closed_before: datetime,
) -> tuple[TerminalStatus | Literal["fenced_deleted"], bool] | None:
    """Reuse the Terminal reader to prove that a stale Plan fence is releasable.

    ``None`` means open/recent and every malformed, partial, missing or future
    shape raises. Callers that reconcile cross-store ownership must treat both
    outcomes as still live unless this function returns a canonical outcome.
    """

    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("terminal Plan proof turn identity is invalid")
    if closed_before.tzinfo is None or closed_before.utcoffset() is None:
        raise ValueError("terminal Plan proof cutoff must be timezone-aware")
    async with connection.execute(
        """
        SELECT t.session_id,w.team_id
        FROM turns t
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        WHERE t.id=?
        """,
        (turn_id,),
    ) as cursor:
        identity = await cursor.fetchone()
    if identity is None:
        raise TerminalProtocolMalformedError(
            "terminal Plan proof Turn is missing",
        )
    context = TerminalReadContext(
        team_id=_required_text(identity["team_id"], "plan_proof.team_id"),
        session_id=_required_text(
            identity["session_id"],
            "plan_proof.session_id",
        ),
        turn_id=turn_id,
        viewer="plan_orphan_maintenance",
        auth_fence_vector_digest="plan_orphan_maintenance",
    )
    row = await _read_checkpoint_row(connection, context)
    if row is None:
        raise TerminalProtocolMalformedError(
            "terminal Plan proof checkpoint is missing",
        )
    async with connection.execute(
        """
        SELECT COUNT(*),COALESCE(MAX(event_seq),0)
        FROM turn_events WHERE turn_id=? AND protocol_version=1
        """,
        (turn_id,),
    ) as cursor:
        highwater_row = await cursor.fetchone()
    event_count = int(highwater_row[0]) if highwater_row is not None else 0
    persistent_event_highwater = int(highwater_row[1]) if highwater_row is not None else 0
    checkpoint = _checkpoint_from_row(
        row,
        context=context,
        event_count=event_count,
        persistent_event_highwater=persistent_event_highwater,
    )
    await _validate_checkpoint_dependencies(connection, checkpoint)
    if checkpoint.current_generation_state is not GenerationState.CLOSED:
        return None
    async with connection.execute(
        """
        SELECT kind,closed_at,closing_reason,terminal_candidate_id,
               terminal_receipt_id
        FROM turn_generations
        WHERE turn_id=? AND generation=? AND protocol_version=1
        """,
        (turn_id, checkpoint.current_generation),
    ) as cursor:
        generation = await cursor.fetchone()
    if generation is None:
        raise TerminalProtocolMalformedError(
            "terminal Plan proof generation is missing",
        )
    closed_at = _aware_timestamp(generation["closed_at"])
    if closed_at is None:
        raise TerminalProtocolMalformedError(
            "terminal Plan proof closed time is malformed",
        )
    if closed_at > closed_before.astimezone(UTC):
        return None

    if checkpoint.current_generation_resolution == "fenced_deleted":
        async with connection.execute(
            """
            SELECT * FROM turn_terminal_deletion_ledgers
            WHERE turn_id=? AND generation=?
            """,
            (turn_id, checkpoint.current_generation),
        ) as cursor:
            deletion = await cursor.fetchone()
        if deletion is None:
            raise TerminalProtocolMalformedError(
                "terminal Plan proof deletion ledger is missing",
            )
        result = await _read_protocol_delete_result(
            connection,
            TerminalDeleteContext(
                scope=TerminalScope(
                    team_id=context.team_id,
                    session_id=context.session_id,
                    turn_id=turn_id,
                    generation=checkpoint.current_generation,
                ),
                deletion_id=_required_text(
                    deletion["deletion_id"],
                    "plan_proof.deletion_id",
                ),
                expected_generation_row_version=_positive_int(
                    deletion["prior_row_version"],
                    "plan_proof.prior_row_version",
                ),
                expected_candidate_id=deletion["candidate_id"],
                expected_delivery_fence_epoch=(
                    _positive_int(
                        deletion["new_delivery_fence_epoch"],
                        "plan_proof.new_delivery_fence_epoch",
                    )
                    - 1
                ),
                actor_audit_ref=_required_text(
                    deletion["actor_audit_ref"],
                    "plan_proof.actor_audit_ref",
                ),
                authz_snapshot_digest=_required_text(
                    deletion["authz_snapshot_digest"],
                    "plan_proof.authz_snapshot_digest",
                ),
                policy_code=_required_text(
                    deletion["policy_code"],
                    "plan_proof.policy_code",
                ),
                effect_identity_digest=_required_text(
                    deletion["effect_identity_digest"],
                    "plan_proof.effect_identity_digest",
                ),
                effect_bundle_digest=_required_text(
                    deletion["effect_bundle_digest"],
                    "plan_proof.effect_bundle_digest",
                ),
                effect_audit_key_version=_required_text(
                    deletion["effect_audit_key_version"],
                    "plan_proof.effect_audit_key_version",
                ),
                effect_key_required_until=_required_text(
                    deletion["effect_key_required_until"],
                    "plan_proof.effect_key_required_until",
                ),
            ),
        )
        return (
            (
                "fenced_deleted",
                False,
            )
            if result is not None
            else None
        )

    receipt = checkpoint.current_receipt_summary
    candidate_id = generation["terminal_candidate_id"]
    if (
        receipt is None
        or not isinstance(candidate_id, str)
        or not candidate_id
        or receipt.candidate_id != candidate_id
        or receipt.receipt_id != generation["terminal_receipt_id"]
    ):
        raise TerminalProtocolMalformedError(
            "terminal Plan proof receipt identity changed",
        )
    async with connection.execute(
        """
        SELECT candidate_projection_digest
        FROM turn_terminal_candidates WHERE candidate_id=?
        """,
        (candidate_id,),
    ) as cursor:
        candidate = await cursor.fetchone()
    if candidate is None:
        raise TerminalProtocolMalformedError(
            "terminal Plan proof candidate is missing",
        )
    committed = await _read_committed_receipt(
        connection,
        candidate_id=candidate_id,
        candidate_projection_digest=_required_text(
            candidate["candidate_projection_digest"],
            "plan_proof.candidate_projection_digest",
        ),
    )
    if committed is None or committed.receipt_id != receipt.receipt_id:
        raise TerminalProtocolMalformedError(
            "terminal Plan proof committed receipt is missing",
        )
    return (
        committed.status,
        is_explicit_user_cancel_receipt(
            status=committed.status,
            reason_code=receipt.reason_code,
            generation_kind=generation["kind"],
            closing_reason=generation["closing_reason"],
        ),
    )


def _session_active_claim_from_row(row: aiosqlite.Row) -> SessionActiveClaimSummary | None:
    values = (
        row["active_turn_id"],
        row["active_turn_protocol_version"],
        row["active_turn_generation"],
        row["active_turn_claim_epoch"],
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise TerminalProtocolMalformedError("Session active claim tuple is malformed")
    protocol_version = row["active_turn_protocol_version"]
    if protocol_version != 1:
        raise TerminalProtocolMalformedError("Session active claim uses a non-v1 protocol")
    return SessionActiveClaimSummary(
        turn_id=_required_text(row["active_turn_id"], "sessions.active_turn_id"),
        protocol_version=1,
        generation=_positive_int(row["active_turn_generation"], "sessions.active_turn_generation"),
        claim_epoch=_nonnegative_int(row["active_turn_claim_epoch"], "sessions.active_turn_claim_epoch"),
    )


def _validate_event_accept_input(
    *,
    event_type: str,
    actor_scope: ActorScope,
    phase_scope: PhaseScope,
    publish_mode: EventPublishMode,
    root_durability: RootDurability,
    payload: Mapping[str, object],
) -> None:
    if not isinstance(event_type, str) or not event_type or len(event_type) > 128:
        raise ValueError("event_type must be a non-empty bounded string")
    if event_type == "done":
        raise ValueError("done is finalizer-only terminal batch content")
    if not isinstance(actor_scope, ActorScope) or not isinstance(phase_scope, PhaseScope):
        raise TypeError("event scopes must use terminal protocol enums")
    if phase_scope is not PhaseScope.IN_GENERATION:
        raise ValueError("execution accept_event only supports in_generation")
    if not isinstance(publish_mode, EventPublishMode):
        raise TypeError("publish_mode must use EventPublishMode")
    if not isinstance(root_durability, RootDurability):
        raise TypeError("root_durability must use RootDurability")
    if not isinstance(payload, Mapping):
        raise TypeError("event payload must be a mapping")
    if event_type == "error" and actor_scope in {ActorScope.ROOT, ActorScope.TRANSPORT}:
        raise ValueError("root non-durable errors are transport-only and cannot be accepted")
    if root_durability is not RootDurability.COMMITTED:
        raise ValueError("durable event proposals must use committed root durability")


def _idempotent_event_identity(
    *,
    turn_id: str,
    generation: int,
    idempotency_key: str,
) -> tuple[str, str]:
    """Derive the one proposal/event primary-key pair used by accept and lookup."""

    if any(not isinstance(value, str) or not value for value in (turn_id, idempotency_key)):
        raise ValueError("idempotent event identity is invalid")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("idempotent event generation is invalid")
    stable_suffix = canonical_digest_v1(
        {
            "generation": generation,
            "idempotency_key": idempotency_key,
            "turn_id": turn_id,
        }
    )[:32]
    return f"tep_{stable_suffix}", f"evt_{stable_suffix}"


async def _insert_or_replay_event_proposal_in_tx(
    connection: aiosqlite.Connection,
    *,
    context: ExecutionGenerationContext,
    generation_row: aiosqlite.Row,
    event_type: str,
    actor_scope: ActorScope,
    phase_scope: PhaseScope,
    payload_json: str,
    payload_digest: str,
    publish_mode: EventPublishMode,
    root_durability: RootDurability,
    idempotency_key: str | None,
    accepted_at: str,
) -> tuple[AcceptedEventProposal, bool]:
    """Insert or replay one proposal inside the caller's fenced transaction."""

    if idempotency_key is None:
        proposal_id = f"tep_{uuid4().hex}"
        event_id = f"evt_{uuid4().hex}"
    else:
        proposal_id, event_id = _idempotent_event_identity(
            turn_id=context.scope.turn_id,
            generation=context.scope.generation,
            idempotency_key=idempotency_key,
        )
        async with connection.execute(
            """
            SELECT p.*,e.id AS persisted_id,e.turn_id AS persisted_turn_id,
                   e.generation AS persisted_generation,e.seq AS persisted_legacy_seq,
                   e.event_seq AS persisted_event_seq_row,
                   e.event_type AS persisted_event_type,
                   e.actor_scope AS persisted_actor_scope,
                   e.phase_scope AS persisted_phase_scope,
                   e.payload_json AS persisted_payload_json,
                   e.payload_digest AS persisted_payload_digest,
                   e.source_proposal_id AS persisted_source_proposal_id,
                   e.accept_ordinal AS persisted_accept_ordinal,
                   e.occurred_at AS persisted_occurred_at,
                   e.protocol_version AS persisted_protocol_version,
                   e.terminal_receipt_id AS persisted_terminal_receipt_id
            FROM turn_event_proposals p
            LEFT JOIN turn_events e ON e.id=p.event_id
            WHERE p.proposal_id=?
            """,
            (proposal_id,),
        ) as cursor:
            replay = await cursor.fetchone()
        if replay is not None:
            expected = {
                "event_id": event_id,
                "turn_id": context.scope.turn_id,
                "generation": context.scope.generation,
                "accepted_claim_epoch": context.claim_epoch,
                "event_type": event_type,
                "actor_scope": actor_scope.value,
                "phase_scope": phase_scope.value,
                "payload_json": payload_json,
                "payload_digest": payload_digest,
                "durability": root_durability.value,
            }
            if any(replay[field] != value for field, value in expected.items()):
                raise TerminalProtocolMalformedError(
                    "idempotent event proposal identity conflicts",
                )
            accept_ordinal = _positive_int(
                replay["accept_ordinal"],
                "proposal.accept_ordinal",
            )
            accepted_next = _positive_int(
                generation_row["accepted_proposal_next"],
                "generation.accepted_proposal_next",
            )
            if accept_ordinal >= accepted_next:
                raise TerminalProtocolMalformedError(
                    "idempotent event proposal allocator was not committed",
                )
            if replay["state"] == "accepted":
                if replay["persisted_id"] is not None or replay["persisted_at"] is not None or replay["persisted_event_seq"] is not None:
                    raise TerminalProtocolMalformedError(
                        "accepted event proposal has persisted state",
                    )
            elif replay["state"] == "persisted":
                if (
                    replay["persisted_protocol_version"] != 1
                    or replay["persisted_terminal_receipt_id"] is not None
                    or replay["persisted_occurred_at"] != replay["accepted_at"]
                ):
                    raise TerminalProtocolMalformedError(
                        "persisted event proposal envelope is malformed",
                    )
                _required_text(replay["persisted_at"], "proposal.persisted_at")
                _validate_persisted_proposal_projection(replay)
                persisted_event_seq = _positive_int(
                    replay["persisted_event_seq_row"],
                    "proposal.persisted_event_seq",
                )
                if persisted_event_seq >= _positive_int(
                    generation_row["event_sequence_next"],
                    "turns.event_sequence_next",
                ):
                    raise TerminalProtocolMalformedError(
                        "idempotent event allocator was not committed",
                    )
            else:
                raise TerminalProtocolMalformedError(
                    "idempotent event proposal state is malformed",
                )
            return (
                AcceptedEventProposal(
                    scope=context.scope,
                    proposal_id=proposal_id,
                    event_id=event_id,
                    accept_ordinal=accept_ordinal,
                    accepted_claim_epoch=context.claim_epoch,
                    event_type=event_type,
                    actor_scope=actor_scope,
                    phase_scope=phase_scope,
                    payload_json=payload_json,
                    payload_digest=payload_digest,
                    root_durability=root_durability,
                    publish_mode=publish_mode,
                    accepted_at=_required_text(
                        replay["accepted_at"],
                        "proposal.accepted_at",
                    ),
                ),
                False,
            )

    accept_ordinal = _positive_int(
        generation_row["accepted_proposal_next"],
        "generation.accepted_proposal_next",
    )
    await connection.execute(
        """
        INSERT INTO turn_event_proposals(
          proposal_id,event_id,turn_id,generation,accept_ordinal,
          accepted_claim_epoch,event_type,actor_scope,phase_scope,payload_json,
          payload_digest,durability,state,accepted_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'accepted',?)
        """,
        (
            proposal_id,
            event_id,
            context.scope.turn_id,
            context.scope.generation,
            accept_ordinal,
            context.claim_epoch,
            event_type,
            actor_scope.value,
            phase_scope.value,
            payload_json,
            payload_digest,
            root_durability.value,
            accepted_at,
        ),
    )
    update = await connection.execute(
        """
        UPDATE turn_generations
        SET accepted_proposal_next=accepted_proposal_next+1
        WHERE turn_id=? AND generation=? AND protocol_version=1 AND state='running'
          AND owner_instance_id=? AND claim_epoch=? AND accepted_proposal_next=?
          AND terminal_candidate_id IS NULL AND terminal_receipt_id IS NULL
        """,
        (
            context.scope.turn_id,
            context.scope.generation,
            context.owner_instance_id,
            context.claim_epoch,
            accept_ordinal,
        ),
    )
    if update.rowcount != 1:
        raise TerminalProtocolMalformedError(
            "execution generation event allocator changed",
        )
    return (
        AcceptedEventProposal(
            scope=context.scope,
            proposal_id=proposal_id,
            event_id=event_id,
            accept_ordinal=accept_ordinal,
            accepted_claim_epoch=context.claim_epoch,
            event_type=event_type,
            actor_scope=actor_scope,
            phase_scope=phase_scope,
            payload_json=payload_json,
            payload_digest=payload_digest,
            root_durability=root_durability,
            publish_mode=publish_mode,
            accepted_at=accepted_at,
        ),
        True,
    )


async def _read_event_proposal_prefix(
    connection: aiosqlite.Connection,
    *,
    scope: TerminalScope,
    accepted_through: int,
) -> list[aiosqlite.Row]:
    async with connection.execute(
        """
        SELECT p.*,e.id AS persisted_id,e.turn_id AS persisted_turn_id,
               e.generation AS persisted_generation,e.seq AS persisted_legacy_seq,
               e.event_seq AS persisted_event_seq_row,e.event_type AS persisted_event_type,
               e.actor_scope AS persisted_actor_scope,e.phase_scope AS persisted_phase_scope,
               e.payload_json AS persisted_payload_json,e.payload_digest AS persisted_payload_digest,
               e.source_proposal_id AS persisted_source_proposal_id,
               e.accept_ordinal AS persisted_accept_ordinal,e.occurred_at AS persisted_occurred_at
        FROM turn_event_proposals p
        LEFT JOIN turn_events e ON e.id=p.event_id AND e.protocol_version=1
        WHERE p.turn_id=? AND p.generation=? AND p.accept_ordinal<=?
        ORDER BY p.accept_ordinal
        """,
        (scope.turn_id, scope.generation, accepted_through),
    ) as cursor:
        return list(await cursor.fetchall())


def _validate_event_proposal_prefix(
    proposals: list[aiosqlite.Row],
    *,
    context: ExecutionGenerationContext | FinalizationContext,
    accepted_through: int,
) -> None:
    if len(proposals) != accepted_through:
        raise TerminalProtocolMalformedError("accepted event proposal prefix contains an ordinal gap")
    accepted_seen = False
    prior_event_seq = 0
    for expected_ordinal, proposal in enumerate(proposals, start=1):
        if proposal["accept_ordinal"] != expected_ordinal:
            raise TerminalProtocolMalformedError("accepted event proposal prefix contains an ordinal gap")
        expected_claim_epoch = context.claim_epoch if isinstance(context, ExecutionGenerationContext) else context.execution_claim_epoch
        if proposal["accepted_claim_epoch"] != expected_claim_epoch:
            raise TerminalProtocolMalformedError("accepted event proposal claim epoch changed")
        payload = _parse_payload(proposal["payload_json"])
        if canonical_json_v1(payload) != proposal["payload_json"] or canonical_digest_v1(payload) != proposal["payload_digest"]:
            raise TerminalProtocolMalformedError("accepted event proposal payload digest mismatch")
        _enum_value(proposal["actor_scope"], "actor_scope", "proposal.actor_scope")
        phase_scope = _enum_value(proposal["phase_scope"], "phase_scope", "proposal.phase_scope")
        if phase_scope != PhaseScope.IN_GENERATION:
            raise TerminalProtocolMalformedError("execution proposal phase scope is invalid")
        _enum_value(proposal["durability"], "root_durability", "proposal.durability")
        state = proposal["state"]
        if state == "accepted":
            accepted_seen = True
            if proposal["persisted_id"] is not None:
                raise TerminalProtocolMalformedError("accepted proposal already has a persisted event")
            continue
        if state != "persisted":
            raise TerminalProtocolMalformedError("event proposal state is invalid")
        if accepted_seen:
            raise TerminalProtocolMalformedError("event proposal persistence is not a prefix")
        _validate_persisted_proposal_projection(proposal)
        event_seq = _positive_int(proposal["persisted_event_seq_row"], "turn_events.event_seq")
        if prior_event_seq and event_seq <= prior_event_seq:
            raise TerminalProtocolMalformedError("persisted proposal event sequence moved backwards")
        prior_event_seq = event_seq


def _validate_persisted_proposal_projection(proposal: aiosqlite.Row) -> None:
    expected_pairs = (
        ("persisted_id", "event_id"),
        ("persisted_turn_id", "turn_id"),
        ("persisted_generation", "generation"),
        ("persisted_event_seq_row", "persisted_event_seq"),
        ("persisted_event_type", "event_type"),
        ("persisted_actor_scope", "actor_scope"),
        ("persisted_phase_scope", "phase_scope"),
        ("persisted_payload_json", "payload_json"),
        ("persisted_payload_digest", "payload_digest"),
        ("persisted_source_proposal_id", "proposal_id"),
        ("persisted_accept_ordinal", "accept_ordinal"),
    )
    if any(proposal[persisted] != proposal[accepted] for persisted, accepted in expected_pairs):
        raise TerminalProtocolMalformedError("persisted event does not match durable proposal")
    _positive_int(proposal["persisted_legacy_seq"], "turn_events.seq")
    _required_text(proposal["persisted_occurred_at"], "turn_events.occurred_at")


async def _materialize_event_proposal_prefix(
    connection: aiosqlite.Connection,
    *,
    context: ExecutionGenerationContext | FinalizationContext,
    proposals: list[aiosqlite.Row],
    event_sequence_next: int,
    persisted_at: str,
) -> tuple[EventTicket, ...]:
    async with connection.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM turn_events WHERE turn_id=?",
        (context.scope.turn_id,),
    ) as cursor:
        legacy_row = await cursor.fetchone()
    legacy_next = int(legacy_row[0]) if legacy_row is not None else 1
    tickets: list[EventTicket] = []
    newly_persisted = 0
    for proposal in proposals:
        if proposal["state"] == "persisted":
            tickets.append(_event_ticket_from_persisted_row(context.scope, proposal))
            continue
        event_seq = event_sequence_next + newly_persisted
        legacy_seq = legacy_next + newly_persisted
        update = await connection.execute(
            """
            INSERT INTO turn_events(
              id,turn_id,seq,event_type,payload_json,occurred_at,protocol_version,
              generation,event_seq,actor_scope,phase_scope,payload_digest,
              terminal_receipt_id,source_proposal_id,accept_ordinal
            ) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,NULL,?,?)
            """,
            (
                proposal["event_id"],
                proposal["turn_id"],
                legacy_seq,
                proposal["event_type"],
                proposal["payload_json"],
                proposal["accepted_at"],
                proposal["generation"],
                event_seq,
                proposal["actor_scope"],
                proposal["phase_scope"],
                proposal["payload_digest"],
                proposal["proposal_id"],
                proposal["accept_ordinal"],
            ),
        )
        update = await connection.execute(
            """
            UPDATE turn_event_proposals
            SET state='persisted',persisted_at=?,persisted_event_seq=?
            WHERE proposal_id=? AND state='accepted' AND persisted_at IS NULL
              AND persisted_event_seq IS NULL
            """,
            (persisted_at, event_seq, proposal["proposal_id"]),
        )
        if update.rowcount != 1:
            raise TerminalProtocolMalformedError("accepted event proposal persistence CAS changed")
        tickets.append(
            EventTicket(
                scope=context.scope,
                proposal_id=_required_text(proposal["proposal_id"], "proposal.proposal_id"),
                event_id=_required_text(proposal["event_id"], "proposal.event_id"),
                accept_ordinal=_positive_int(proposal["accept_ordinal"], "proposal.accept_ordinal"),
                event_seq=event_seq,
                legacy_seq=legacy_seq,
                event_type=_required_text(proposal["event_type"], "proposal.event_type"),
                actor_scope=ActorScope(proposal["actor_scope"]),
                phase_scope=PhaseScope(proposal["phase_scope"]),
                payload_json=_required_text(proposal["payload_json"], "proposal.payload_json"),
                payload_digest=_required_text(proposal["payload_digest"], "proposal.payload_digest"),
                root_durability=RootDurability(proposal["durability"]),
                occurred_at=_required_text(proposal["accepted_at"], "proposal.accepted_at"),
            )
        )
        newly_persisted += 1
    if newly_persisted:
        update = await connection.execute(
            """
            UPDATE turns SET event_sequence_next=event_sequence_next+?
            WHERE id=? AND terminal_protocol_version=1 AND event_sequence_next=?
            """,
            (newly_persisted, context.scope.turn_id, event_sequence_next),
        )
        if update.rowcount != 1:
            raise TerminalProtocolMalformedError("turn event sequence allocator changed")
    ordered = sorted(tickets, key=lambda ticket: ticket.accept_ordinal)
    for prior, current in zip(ordered, ordered[1:], strict=False):
        if current.event_seq <= prior.event_seq:
            raise TerminalProtocolMalformedError("materialized proposal event sequence moved backwards")
    return tuple(ordered)


def _event_ticket_from_persisted_row(scope: TerminalScope, proposal: aiosqlite.Row) -> EventTicket:
    return EventTicket(
        scope=scope,
        proposal_id=_required_text(proposal["proposal_id"], "proposal.proposal_id"),
        event_id=_required_text(proposal["event_id"], "proposal.event_id"),
        accept_ordinal=_positive_int(proposal["accept_ordinal"], "proposal.accept_ordinal"),
        event_seq=_positive_int(proposal["persisted_event_seq_row"], "turn_events.event_seq"),
        legacy_seq=_positive_int(proposal["persisted_legacy_seq"], "turn_events.seq"),
        event_type=_required_text(proposal["event_type"], "proposal.event_type"),
        actor_scope=ActorScope(proposal["actor_scope"]),
        phase_scope=PhaseScope(proposal["phase_scope"]),
        payload_json=_required_text(proposal["payload_json"], "proposal.payload_json"),
        payload_digest=_required_text(proposal["payload_digest"], "proposal.payload_digest"),
        root_durability=RootDurability(proposal["durability"]),
        occurred_at=_required_text(proposal["persisted_occurred_at"], "turn_events.occurred_at"),
    )


async def _append_post_terminal_auxiliary_in_tx(
    connection: aiosqlite.Connection,
    *,
    context: PostTerminalAuxiliaryContext,
    event_type: str,
    actor_scope: ActorScope,
    payload: Mapping[str, object],
    occurred_at: str,
) -> TerminalProtocolEventRow:
    payload_copy = _canonical_mapping_copy(payload)
    payload_json = canonical_json_v1(payload_copy)
    payload_digest = canonical_digest_v1(payload_copy)
    async with connection.execute(
        """
        SELECT r.receipt_id,r.protocol_version,r.turn_id,r.generation,
               g.protocol_version AS generation_protocol_version,
               g.state AS generation_state,g.terminal_receipt_id,
               t.terminal_protocol_version,t.delivery_fence_epoch,w.team_id,t.session_id
        FROM turn_terminal_receipts r
        JOIN turn_generations g
          ON g.turn_id=r.turn_id AND g.generation=r.generation
        JOIN turns t ON t.id=r.turn_id
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        WHERE r.receipt_id=? AND r.turn_id=? AND r.generation=?
        """,
        (context.receipt_id, context.scope.turn_id, context.scope.generation),
    ) as cursor:
        root = await cursor.fetchone()
    if (
        root is None
        or root["protocol_version"] != 1
        or root["generation_protocol_version"] != 1
        or root["terminal_protocol_version"] != 1
        or root["generation_state"] != GenerationState.CLOSED
        or root["terminal_receipt_id"] != context.receipt_id
        or root["team_id"] != context.scope.team_id
        or root["session_id"] != context.scope.session_id
        or root["delivery_fence_epoch"] != context.expected_delivery_fence_epoch
    ):
        raise TerminalProtocolMalformedError("post-terminal auxiliary receipt fence changed")
    async with connection.execute(
        """
        SELECT id,turn_id,seq,event_type,payload_json,occurred_at,protocol_version,
               generation,event_seq,actor_scope,phase_scope,payload_digest,
               terminal_receipt_id
        FROM turn_events WHERE id=?
        """,
        (context.event_id,),
    ) as cursor:
        existing = await cursor.fetchone()
    if existing is not None:
        return _post_terminal_auxiliary_from_row(
            existing,
            context=context,
            event_type=event_type,
            actor_scope=actor_scope,
            payload_json=payload_json,
            payload_digest=payload_digest,
        )
    async with connection.execute(
        "SELECT event_sequence_next FROM turns WHERE id=? AND terminal_protocol_version=1",
        (context.scope.turn_id,),
    ) as cursor:
        allocator = await cursor.fetchone()
    if allocator is None:
        raise TerminalProtocolMalformedError("post-terminal auxiliary turn is missing")
    event_seq = _positive_int(
        allocator["event_sequence_next"],
        "turns.event_sequence_next",
    )
    async with connection.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM turn_events WHERE turn_id=?",
        (context.scope.turn_id,),
    ) as cursor:
        legacy_row = await cursor.fetchone()
    legacy_seq = int(legacy_row[0]) if legacy_row is not None else 1
    await connection.execute(
        """
        INSERT INTO turn_events(
          id,turn_id,seq,event_type,payload_json,occurred_at,protocol_version,
          generation,event_seq,actor_scope,phase_scope,payload_digest,
          terminal_receipt_id,source_proposal_id,accept_ordinal
        ) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,NULL,NULL)
        """,
        (
            context.event_id,
            context.scope.turn_id,
            legacy_seq,
            event_type,
            payload_json,
            occurred_at,
            context.scope.generation,
            event_seq,
            actor_scope.value,
            PhaseScope.POST_TERMINAL_AUXILIARY.value,
            payload_digest,
            context.receipt_id,
        ),
    )
    update = await connection.execute(
        """
        UPDATE turns SET event_sequence_next=event_sequence_next+1
        WHERE id=? AND terminal_protocol_version=1 AND event_sequence_next=?
          AND delivery_fence_epoch=?
        """,
        (
            context.scope.turn_id,
            event_seq,
            context.expected_delivery_fence_epoch,
        ),
    )
    if update.rowcount != 1:
        raise TerminalProtocolMalformedError("post-terminal auxiliary allocator changed")
    return TerminalProtocolEventRow(
        event_id=context.event_id,
        turn_id=context.scope.turn_id,
        generation=context.scope.generation,
        event_seq=event_seq,
        event_type=event_type,
        actor_scope=actor_scope.value,
        phase_scope=PhaseScope.POST_TERMINAL_AUXILIARY.value,
        terminal_receipt_id=context.receipt_id,
        payload=payload_copy,
        payload_digest=payload_digest,
        occurred_at=occurred_at,
    )


def _post_terminal_auxiliary_from_row(
    row: aiosqlite.Row,
    *,
    context: PostTerminalAuxiliaryContext,
    event_type: str,
    actor_scope: ActorScope,
    payload_json: str,
    payload_digest: str,
) -> TerminalProtocolEventRow:
    expected = {
        "actor_scope": actor_scope.value,
        "id": context.event_id,
        "event_type": event_type,
        "generation": context.scope.generation,
        "payload_digest": payload_digest,
        "payload_json": payload_json,
        "phase_scope": PhaseScope.POST_TERMINAL_AUXILIARY.value,
        "protocol_version": 1,
        "terminal_receipt_id": context.receipt_id,
        "turn_id": context.scope.turn_id,
    }
    if any(row[field] != value for field, value in expected.items()):
        raise TerminalProtocolMalformedError("post-terminal auxiliary event identity conflict")
    payload = _parse_payload(row["payload_json"])
    if canonical_digest_v1(payload) != row["payload_digest"]:
        raise TerminalProtocolMalformedError("post-terminal auxiliary payload digest is malformed")
    _positive_int(row["seq"], "turn_events.seq")
    return TerminalProtocolEventRow(
        event_id=_required_text(row["id"], "turn_events.id"),
        turn_id=context.scope.turn_id,
        generation=context.scope.generation,
        event_seq=_positive_int(row["event_seq"], "turn_events.event_seq"),
        event_type=event_type,
        actor_scope=actor_scope.value,
        phase_scope=PhaseScope.POST_TERMINAL_AUXILIARY.value,
        terminal_receipt_id=context.receipt_id,
        payload=payload,
        payload_digest=payload_digest,
        occurred_at=_required_text(row["occurred_at"], "turn_events.occurred_at"),
    )


def _effect_attempt_from_row(scope: TerminalScope, row: aiosqlite.Row) -> EffectAttemptContext:
    return _effect_attempt_from_mapping(scope, dict(row))


def _effect_attempt_from_mapping(
    scope: TerminalScope,
    row: Mapping[str, object],
) -> EffectAttemptContext:
    if row.get("turn_id") != scope.turn_id or row.get("generation") != scope.generation:
        raise TerminalProtocolMalformedError("effect attempt scope is malformed")
    try:
        effect_class = EffectClass(row["effect_class"])
        state = EffectState(row["state"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TerminalProtocolMalformedError("effect attempt enum is malformed") from exc
    non_retriable = row["non_retriable"]
    if non_retriable not in {0, 1}:
        raise TerminalProtocolMalformedError("effect attempt retry shape is malformed")
    return EffectAttemptContext(
        scope=scope,
        attempt_id=_required_text(row.get("attempt_id"), "effect_attempt.attempt_id"),
        action_ref=_required_text(row.get("action_ref"), "effect_attempt.action_ref"),
        provider=_required_text(row.get("provider"), "effect_attempt.provider"),
        effect_class=effect_class,
        state=state,
        claim_epoch=_positive_int(row.get("claim_epoch"), "effect_attempt.claim_epoch"),
        row_version=_positive_int(row.get("row_version"), "effect_attempt.row_version"),
        idempotency_or_fence_token=_optional_nonempty(
            row.get("idempotency_or_fence_token"),
            "effect_attempt.idempotency_or_fence_token",
        ),
        non_retriable=bool(non_retriable),
    )


def _same_effect_attempt_identity(
    current: EffectAttemptContext,
    expected: EffectAttemptContext,
) -> bool:
    return bool(
        current.scope == expected.scope
        and current.attempt_id == expected.attempt_id
        and current.action_ref == expected.action_ref
        and current.provider == expected.provider
        and current.effect_class is expected.effect_class
        and current.claim_epoch == expected.claim_epoch
        and current.idempotency_or_fence_token == expected.idempotency_or_fence_token
        and current.non_retriable is expected.non_retriable
    )


def _require_effect_dispatch_before_usage_freeze(
    generation: Mapping[str, object],
) -> None:
    contract = resolve_effect_execution_contract(
        _parse_payload(generation["execution_intent_json"]),
    )
    if contract.disposition is EffectIntentDisposition.RECOVERY_ONLY and generation["usage_digest"] is not None:
        raise TerminalProtocolMalformedError(
            "effect dispatch is closed after usage freeze",
        )


def _local_billing_fact_from_generation(
    scope: TerminalScope,
    generation: Mapping[str, object],
) -> _LocalBillingFact:
    contract = resolve_effect_execution_contract(
        _parse_payload(generation["execution_intent_json"]),
    )
    if contract.disposition is not EffectIntentDisposition.RECOVERY_ONLY or contract.billing_rate_card_version is None:
        raise TerminalProtocolMalformedError(
            "local billing requires contract-v2",
        )
    user_id = _required_text(
        generation["session_user_id"],
        "local billing Session owner",
    )
    workspace_id = _required_text(
        generation["session_workspace_id"],
        "local billing workspace",
    )
    team_id = _required_text(
        generation["team_id"],
        "local billing team",
    )
    if team_id != scope.team_id:
        raise TerminalProtocolMalformedError("local billing team scope changed")
    input_tokens = _nonnegative_int(
        generation["input_tokens"],
        "local billing input tokens",
    )
    output_tokens = _nonnegative_int(
        generation["output_tokens"],
        "local billing output tokens",
    )
    cost_amount = f"{_nonnegative_float(generation['cost_amount'], 'local billing cost amount'):.12f}"
    rate_card_version = contract.billing_rate_card_version
    model_usage: tuple[ModelUsageLine, ...] = ()
    if rate_card_version == "v2":
        raw_usage = generation["billing_usage_json"]
        if raw_usage is None and input_tokens == 0 and output_tokens == 0:
            # No usage was ever recorded (for example Stop before the first
            # model call): there are no model lines to bill.
            raw_usage = "[]"
        if not isinstance(raw_usage, str):
            raise TerminalProtocolMalformedError("v2 local billing model usage is unavailable")
        parsed_usage = json.loads(raw_usage)
        if not isinstance(parsed_usage, list):
            raise TerminalProtocolMalformedError("v2 local billing model usage is invalid")
        try:
            model_usage = tuple(ModelUsageLine.from_json(line) for line in parsed_usage)
        except ValueError as exc:
            raise TerminalProtocolMalformedError("v2 local billing model usage is invalid") from exc
    try:
        cost_credits = credits_for_tokens_at_version(
            rate_card_version,
            input_tokens,
            output_tokens,
            model_usage=[line.usage for line in model_usage] if rate_card_version == "v2" else None,
        )
    except ValueError as exc:
        raise TerminalProtocolMalformedError(
            "local billing rate card is unavailable",
        ) from exc
    provider = f"credit_ledger/{rate_card_version}"
    provider_contract = get_effect_provider_contract(provider)
    if (
        provider_contract.disclosure_kind is not EffectDisclosureKind.ACCOUNTING
        or provider_contract.usage_evidence is not EffectUsageEvidence.DETERMINISTIC_LOCAL
    ):
        raise TerminalProtocolMalformedError(
            "local billing provider contract is not deterministic accounting",
        )
    slot_digest = canonical_digest_v1(
        {
            "generation": scope.generation,
            "purpose": "chat-credit/slot/v1",
            "reason": "turn",
            "team_id": team_id,
            "turn_id": scope.turn_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
        }
    )
    fact_payload = {
        "billing_slot": slot_digest,
        "cost_credits": cost_credits,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "purpose": f"chat-credit/fact/{rate_card_version}",
        "rate_card_version": rate_card_version,
    }
    if rate_card_version == "v2":
        fact_payload["model_usage"] = [line.as_json() for line in model_usage]
    fact_digest = canonical_digest_v1(fact_payload)
    return _LocalBillingFact(
        scope=scope,
        user_id=user_id,
        workspace_id=workspace_id,
        rate_card_version=rate_card_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_amount=cost_amount,
        cost_credits=cost_credits,
        slot_digest=slot_digest,
        fact_digest=fact_digest,
        usage_digest=canonical_digest_v1(
            {
                "billing_fact": fact_digest,
                "cost_amount": cost_amount,
                "purpose": "chat-usage/freeze/v1",
            }
        ),
        ledger_id=f"led_tv1_{slot_digest[:32]}",
        action_ref=f"billing:{slot_digest}",
        attempt_id=f"tea_bill_{fact_digest[:32]}",
        provider=provider,
        model_usage=model_usage,
    )


async def _require_local_billing_capability_in_tx(
    connection: aiosqlite.Connection,
    *,
    context: ExecutionGenerationContext | RecoveryProcessingContext,
    generation: aiosqlite.Row | None,
    now: datetime,
) -> None:
    if generation is None:
        raise TerminalProtocolMalformedError("local billing generation is missing")
    if generation["terminal_candidate_id"] is not None or generation["terminal_receipt_id"] is not None:
        raise TerminalProtocolMalformedError(
            "local billing is frozen after terminal candidate prepare",
        )
    if isinstance(context, ExecutionGenerationContext):
        if not _execution_context_matches(generation, context, now):
            raise TerminalProtocolMalformedError(
                "local billing execution capability changed",
            )
        if not await _writer_lease_matches_execution(
            connection,
            context=context,
            now=now,
        ):
            raise TerminalProtocolMalformedError(
                "local billing writer lease is unavailable",
            )
    else:
        recovery = await _read_active_recovery(
            connection,
            context.recovery_id,
        )
        if not _recovery_processing_matches(generation, recovery, context, now):
            raise TerminalProtocolMalformedError(
                "local billing recovery capability changed",
            )
    fact = _local_billing_fact_from_generation(context.scope, generation)
    await _read_effect_disclosure_snapshot_v2(connection, context.scope)
    async with connection.execute(
        """
        SELECT attempt_id FROM turn_effect_attempts
        WHERE turn_id=? AND generation=? AND action_ref<>? AND state<>'settled'
        LIMIT 1
        """,
        (context.scope.turn_id, context.scope.generation, fact.action_ref),
    ) as cursor:
        unsettled = await cursor.fetchone()
    if unsettled is not None:
        raise TerminalProtocolMalformedError(
            "local billing is blocked by unsettled side effects",
        )


async def _freeze_usage_and_claim_billing_in_tx(
    connection: aiosqlite.Connection,
    *,
    generation: aiosqlite.Row,
    scope: TerminalScope,
    now: datetime,
    inject: Callable[[TerminalProtocolFaultPoint], None],
) -> EffectAttemptContext | None:
    fact = _local_billing_fact_from_generation(scope, generation)
    frozen_digest = generation["usage_digest"]
    if frozen_digest is None:
        update = await connection.execute(
            """
            UPDATE turn_generations SET usage_digest=?
            WHERE turn_id=? AND generation=? AND protocol_version=1
              AND usage_digest IS NULL
              AND terminal_candidate_id IS NULL AND terminal_receipt_id IS NULL
            """,
            (fact.usage_digest, scope.turn_id, scope.generation),
        )
        if update.rowcount != 1:
            raise TerminalProtocolMalformedError(
                "local billing usage freeze CAS changed",
            )
        inject(TerminalProtocolFaultPoint.LOCAL_BILLING_AFTER_USAGE_FREEZE)
    elif frozen_digest != fact.usage_digest:
        raise TerminalProtocolMalformedError("local billing frozen usage changed")
    if fact.cost_credits == 0:
        return None
    async with connection.execute(
        """
        SELECT * FROM turn_effect_attempts
        WHERE turn_id=? AND generation=? AND action_ref=?
        """,
        (scope.turn_id, scope.generation, fact.action_ref),
    ) as cursor:
        row = await cursor.fetchone()
    claim_epoch = _positive_int(
        generation["claim_epoch"],
        "local billing execution claim epoch",
    )
    if row is None:
        await connection.execute(
            """
            INSERT INTO turn_effect_attempts(
              attempt_id,turn_id,generation,claim_epoch,effect_class,provider,
              action_ref,idempotency_or_fence_token,non_retriable,state,row_version,started_at
            ) VALUES (?,?,?,?,?,?,?,?,0,'intent',1,?)
            """,
            (
                fact.attempt_id,
                scope.turn_id,
                scope.generation,
                claim_epoch,
                EffectClass.SIDE_EFFECT_POSSIBLE.value,
                fact.provider,
                fact.action_ref,
                fact.ledger_id,
                _iso_utc(now),
            ),
        )
        row_version = 1
    else:
        current = _effect_attempt_from_row(scope, row)
        if (
            current.attempt_id != fact.attempt_id
            or current.provider != fact.provider
            or current.effect_class is not EffectClass.SIDE_EFFECT_POSSIBLE
            or current.claim_epoch != claim_epoch
            or current.idempotency_or_fence_token != fact.ledger_id
            or current.non_retriable
        ):
            raise TerminalProtocolMalformedError(
                "local billing attempt identity conflict",
            )
        if current.state is not EffectState.INTENT:
            return current
        row_version = current.row_version
    update = await connection.execute(
        """
        UPDATE turn_effect_attempts SET state='dispatched',row_version=row_version+1
        WHERE attempt_id=? AND turn_id=? AND generation=? AND claim_epoch=?
          AND state='intent' AND row_version=?
        """,
        (
            fact.attempt_id,
            scope.turn_id,
            scope.generation,
            claim_epoch,
            row_version,
        ),
    )
    if update.rowcount != 1:
        raise TerminalProtocolMalformedError(
            "local billing dispatch claim changed",
        )
    return EffectAttemptContext(
        scope=scope,
        attempt_id=fact.attempt_id,
        action_ref=fact.action_ref,
        provider=fact.provider,
        effect_class=EffectClass.SIDE_EFFECT_POSSIBLE,
        state=EffectState.DISPATCHED,
        claim_epoch=claim_epoch,
        row_version=row_version + 1,
        idempotency_or_fence_token=fact.ledger_id,
        non_retriable=False,
    )


async def _read_local_billing_attempt_in_tx(
    connection: aiosqlite.Connection,
    *,
    fact: _LocalBillingFact,
    generation: aiosqlite.Row,
) -> EffectAttemptContext:
    async with connection.execute(
        """
        SELECT * FROM turn_effect_attempts
        WHERE turn_id=? AND generation=? AND action_ref=?
        """,
        (fact.scope.turn_id, fact.scope.generation, fact.action_ref),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise TerminalProtocolMalformedError("local billing attempt is missing")
    current = _effect_attempt_from_row(fact.scope, row)
    if (
        current.attempt_id != fact.attempt_id
        or current.provider != fact.provider
        or current.effect_class is not EffectClass.SIDE_EFFECT_POSSIBLE
        or current.claim_epoch
        != _positive_int(
            generation["claim_epoch"],
            "local billing execution claim epoch",
        )
        or current.idempotency_or_fence_token != fact.ledger_id
        or current.non_retriable
    ):
        raise TerminalProtocolMalformedError(
            "local billing attempt identity conflict",
        )
    return current


async def _read_local_billing_settlement_outcome_in_tx(
    connection: aiosqlite.Connection,
    attempt: EffectAttemptContext,
) -> tuple[EffectSettlementOutcome, str] | None:
    async with connection.execute(
        """
        SELECT outcome,settlement_code FROM turn_effect_settlements
        WHERE attempt_id=?
        """,
        (attempt.attempt_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        if attempt.state is EffectState.SETTLED:
            raise TerminalProtocolMalformedError(
                "local billing settlement is missing",
            )
        return None
    if attempt.state is not EffectState.SETTLED:
        raise TerminalProtocolMalformedError(
            "local billing settlement precedes attempt state",
        )
    try:
        return EffectSettlementOutcome(row["outcome"]), _required_text(
            row["settlement_code"],
            "local billing settlement code",
        )
    except ValueError as exc:
        raise TerminalProtocolMalformedError(
            "local billing settlement outcome is malformed",
        ) from exc


async def _local_billing_ledger_exists_in_tx(
    connection: aiosqlite.Connection,
    fact: _LocalBillingFact,
) -> bool:
    try:
        return await _stable_credit_ledger_row_exists_in_tx(
            connection,
            ledger_id=fact.ledger_id,
            user_id=fact.user_id,
            team_id=fact.scope.team_id,
            cost_credits=fact.cost_credits,
            turn_id=fact.scope.turn_id,
            workspace_id=fact.workspace_id,
            tokens_input=fact.input_tokens,
            tokens_output=fact.output_tokens,
            reason="turn",
            rate_card_version=fact.rate_card_version,
            model_usage=fact.model_usage,
        )
    except ValueError as exc:
        raise TerminalProtocolMalformedError(
            "local billing ledger identity changed",
        ) from exc


async def _settle_local_billing_in_tx(
    connection: aiosqlite.Connection,
    *,
    generation: aiosqlite.Row,
    attempt: EffectAttemptContext,
    now: datetime,
    inject: Callable[[TerminalProtocolFaultPoint], None],
) -> EffectAttemptContext:
    """Settle one deterministic local charge inside the caller transaction."""

    fact = _local_billing_fact_from_generation(attempt.scope, generation)
    if fact.cost_credits < 1:
        raise TerminalProtocolMalformedError(
            "zero-usage generation has no billing attempt",
        )
    if generation["usage_digest"] != fact.usage_digest:
        raise TerminalProtocolMalformedError(
            "local billing frozen usage changed",
        )
    current = await _read_local_billing_attempt_in_tx(
        connection,
        fact=fact,
        generation=generation,
    )
    if not _same_effect_attempt_identity(current, attempt):
        raise TerminalProtocolMalformedError(
            "local billing attempt identity changed",
        )
    settlement = await _read_local_billing_settlement_outcome_in_tx(
        connection,
        current,
    )
    if settlement is None:
        if current.state not in {
            EffectState.DISPATCHED,
            EffectState.UNKNOWN,
        }:
            raise TerminalProtocolMalformedError(
                "local billing attempt is not dispatch-complete",
            )
        try:
            outcome_value, settlement_code = await _deduct_credit_with_stable_id_in_tx(
                connection,
                ledger_id=fact.ledger_id,
                user_id=fact.user_id,
                team_id=fact.scope.team_id,
                cost_credits=fact.cost_credits,
                turn_id=fact.scope.turn_id,
                workspace_id=fact.workspace_id,
                tokens_input=fact.input_tokens,
                tokens_output=fact.output_tokens,
                reason="turn",
                rate_card_version=fact.rate_card_version,
                model_usage=fact.model_usage,
            )
        except ValueError as exc:
            raise TerminalProtocolMalformedError(
                "local billing ledger identity changed",
            ) from exc
        inject(TerminalProtocolFaultPoint.LOCAL_BILLING_AFTER_LEDGER)
        outcome = EffectSettlementOutcome(outcome_value)
        settlement_code = settlement_code or "credit_ledger_applied"
        expected_row_version = current.row_version
    else:
        outcome, settlement_code = settlement
        ledger_exists = await _local_billing_ledger_exists_in_tx(
            connection,
            fact,
        )
        if ledger_exists is not (outcome is EffectSettlementOutcome.CONFIRMED_APPLIED):
            raise TerminalProtocolMalformedError(
                "local billing settlement disagrees with ledger truth",
            )
        expected_row_version = current.row_version - 1
    settlement_context = _local_billing_settlement_context(
        fact=fact,
        generation=generation,
        expected_attempt_row_version=expected_row_version,
        outcome=outcome,
        settlement_code=settlement_code,
    )
    return await _record_effect_settlement_in_tx(
        connection,
        context=settlement_context,
        now=now,
        inject=inject,
        allow_v2_manual=False,
        allow_v2_provider_control=False,
        allow_v2_approval_dispatched=False,
        allow_v2_local_billing=True,
        allow_v2_llm_completion=False,
    )


async def _require_local_billing_settled_truth_in_tx(
    connection: aiosqlite.Connection,
    *,
    scope: TerminalScope,
    generation: aiosqlite.Row,
) -> EffectAttemptContext | None:
    """Verify frozen usage and exact local ledger/settlement truth."""

    fact = _local_billing_fact_from_generation(scope, generation)
    if generation["usage_digest"] != fact.usage_digest:
        raise TerminalProtocolMalformedError(
            "contract-v2 terminal usage is not frozen",
        )
    if fact.cost_credits == 0:
        async with connection.execute(
            """
            SELECT 1 FROM turn_effect_attempts
            WHERE turn_id=? AND generation=? AND action_ref=?
            """,
            (scope.turn_id, scope.generation, fact.action_ref),
        ) as cursor:
            if await cursor.fetchone() is not None:
                raise TerminalProtocolMalformedError(
                    "zero-usage generation carries a billing attempt",
                )
        return None
    attempt = await _read_local_billing_attempt_in_tx(
        connection,
        fact=fact,
        generation=generation,
    )
    settlement = await _read_local_billing_settlement_outcome_in_tx(
        connection,
        attempt,
    )
    if settlement is None:
        raise TerminalProtocolMalformedError(
            "contract-v2 local billing is not settled",
        )
    outcome, _ = settlement
    ledger_exists = await _local_billing_ledger_exists_in_tx(connection, fact)
    if ledger_exists is not (outcome is EffectSettlementOutcome.CONFIRMED_APPLIED):
        raise TerminalProtocolMalformedError(
            "local billing settlement disagrees with ledger truth",
        )
    return attempt


def _local_billing_settlement_context(
    *,
    fact: _LocalBillingFact,
    generation: Mapping[str, object],
    expected_attempt_row_version: int,
    outcome: EffectSettlementOutcome,
    settlement_code: str,
) -> EffectSettlementContext:
    external_ref_digest = canonical_digest_v1({"ledger_id": fact.ledger_id}) if outcome is EffectSettlementOutcome.CONFIRMED_APPLIED else None
    external_ref_key_version = "credit-ledger-id/v1" if external_ref_digest is not None else None
    evidence_digest = canonical_digest_v1(
        {
            "billing_fact": fact.fact_digest,
            "ledger_id": fact.ledger_id,
            "outcome": outcome.value,
            "settlement_code": settlement_code,
        }
    )
    principal_ref = "adapter:credit_ledger_row/v1"
    audit_object_ref = f"credit-ledger:{fact.ledger_id}"
    identity = effect_control_identity_digest(
        attempt_id=fact.attempt_id,
        provider_contract_key=fact.provider,
        source=EffectSettlementSource.PROVIDER.value,
        outcome=outcome.value,
        settlement_code=settlement_code,
        evidence_digest=evidence_digest,
        object_version_digest=fact.fact_digest,
        actor_audit_ref=principal_ref,
        audit_object_ref=audit_object_ref,
        provider_external_ref_digest=external_ref_digest,
        provider_external_ref_key_version=external_ref_key_version,
    )[:32]
    return EffectSettlementContext(
        scope=fact.scope,
        attempt_id=fact.attempt_id,
        expected_attempt_row_version=expected_attempt_row_version,
        settlement_id=f"set_{identity}",
        source=EffectSettlementSource.PROVIDER,
        outcome=outcome,
        settlement_code=settlement_code,
        principal_ref=principal_ref,
        principal_role="provider_adapter",
        authz_snapshot_digest=_required_text(
            generation["execution_authz_snapshot_digest"],
            "local billing execution auth digest",
        ),
        evidence_link_id=f"evidence_{identity}",
        evidence_digest=evidence_digest,
        object_version_digest=fact.fact_digest,
        purge_after=None,
        audit_object_ref=audit_object_ref,
        provider_external_ref_digest=external_ref_digest,
        provider_external_ref_key_version=external_ref_key_version,
    )


async def _read_effect_disclosure_snapshot(
    connection: aiosqlite.Connection,
    scope: TerminalScope,
) -> EffectDisclosureSnapshot | None:
    async with connection.execute(
        """
        SELECT execution_intent_json FROM turn_generations
        WHERE turn_id=? AND generation=? AND protocol_version=1
        """,
        (scope.turn_id, scope.generation),
    ) as cursor:
        generation = await cursor.fetchone()
    if generation is None:
        raise TerminalProtocolMalformedError("effect disclosure generation is missing")
    intent = _parse_payload(generation["execution_intent_json"])
    contract = resolve_effect_execution_contract(intent)
    if contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
        return await _read_effect_disclosure_snapshot_v2(connection, scope)
    async with connection.execute(
        """
        SELECT a.attempt_id,a.state,a.result_digest,a.settlement_id,
               s.settlement_id AS persisted_settlement_id,
               s.outcome,s.settlement_code,s.evidence_digest
        FROM turn_effect_attempts a
        LEFT JOIN turn_effect_settlements s ON s.attempt_id=a.attempt_id
        WHERE a.turn_id=? AND a.generation=? AND a.effect_class='side_effect_possible'
        ORDER BY a.attempt_id
        """,
        (scope.turn_id, scope.generation),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return None
    projection: list[dict[str, object]] = []
    confirmed_applied_count = 0
    intent_count = 0
    unsettled_count = 0
    for row in rows:
        try:
            state = EffectState(row["state"])
        except ValueError as exc:
            raise TerminalProtocolMalformedError("effect disclosure state is malformed") from exc
        outcome = row["outcome"]
        if state is EffectState.SETTLED:
            if outcome not in {value.value for value in EffectSettlementOutcome}:
                raise TerminalProtocolMalformedError("effect disclosure settlement is missing")
            _validate_settled_effect_projection(row)
            if outcome == EffectSettlementOutcome.CONFIRMED_APPLIED.value:
                confirmed_applied_count += 1
        elif outcome is not None:
            raise TerminalProtocolMalformedError("unsettled effect carries a settlement outcome")
        if state in {
            EffectState.INTENT,
            EffectState.DISPATCHED,
            EffectState.UNKNOWN,
        }:
            unsettled_count += 1
        if state is EffectState.INTENT:
            intent_count += 1
        projection.append(
            {
                "attempt_id": _required_text(row["attempt_id"], "effect_disclosure.attempt_id"),
                "outcome": outcome,
                "state": state.value,
            }
        )
    return EffectDisclosureSnapshot(
        scope=scope,
        attempt_count=len(rows),
        confirmed_applied_count=confirmed_applied_count,
        intent_count=intent_count,
        unsettled_count=unsettled_count,
        state="confirmed_applied" if confirmed_applied_count else "possibly_applied",
        snapshot_digest=canonical_digest_v1(projection),
    )


async def _read_effect_disclosure_snapshot_v2(
    connection: aiosqlite.Connection,
    scope: TerminalScope,
) -> EffectDisclosureSnapshot:
    async with connection.execute(
        """
        SELECT a.attempt_id,a.effect_class,a.state,a.provider,a.result_digest,a.settlement_id,
               s.settlement_id AS persisted_settlement_id,
               s.source,s.outcome,s.settlement_code,s.evidence_digest,
               s.provider_external_ref_digest
        FROM turn_effect_attempts a
        LEFT JOIN turn_effect_settlements s ON s.attempt_id=a.attempt_id
        WHERE a.turn_id=? AND a.generation=?
        ORDER BY a.attempt_id
        """,
        (scope.turn_id, scope.generation),
    ) as cursor:
        rows = await cursor.fetchall()
    attempts: list[EffectDisclosureAttempt] = []
    for row in rows:
        if row["effect_class"] != EffectClass.SIDE_EFFECT_POSSIBLE.value:
            raise TerminalProtocolMalformedError("contract-v2 effect ledger class is malformed")
        provider = _required_text(row["provider"], "effect_disclosure.provider")
        provider_contract = get_effect_provider_contract(provider)
        try:
            state = EffectState(row["state"])
            outcome = EffectSettlementOutcome(row["outcome"]) if row["outcome"] is not None else None
        except ValueError as exc:
            raise TerminalProtocolMalformedError("effect disclosure enum is malformed") from exc
        if state is EffectState.SETTLED:
            try:
                source = EffectSettlementSource(row["source"])
            except ValueError as exc:
                raise TerminalProtocolMalformedError("effect disclosure settlement source is malformed") from exc
            if source not in provider_contract.allowed_settlement_sources or outcome not in provider_contract.allowed_outcomes:
                raise TerminalProtocolMalformedError("effect disclosure violates provider contract")
            _validate_settled_effect_projection(row)
        elif row["source"] is not None:
            raise TerminalProtocolMalformedError("unsettled effect carries a settlement source")
        try:
            attempts.append(
                EffectDisclosureAttempt(
                    attempt_id=_required_text(
                        row["attempt_id"],
                        "effect_disclosure.attempt_id",
                    ),
                    state=state,
                    outcome=outcome,
                    provider_contract_key=provider_contract.key,
                    disclosure_kind=provider_contract.disclosure_kind.value,
                    registry_contract_version=provider_contract.contract_version,
                    settlement_id=row["settlement_id"],
                    result_digest=row["result_digest"],
                    evidence_digest=row["evidence_digest"],
                    provider_external_ref_digest=row["provider_external_ref_digest"],
                )
            )
        except (TypeError, ValueError) as exc:
            raise TerminalProtocolMalformedError("effect disclosure proof is malformed") from exc
    return EffectDisclosureSnapshot.v2(scope=scope, attempts=tuple(attempts))


def _effect_disclosure_snapshot_from_extensions(
    scope: TerminalScope,
    raw_extensions: object,
) -> EffectDisclosureSnapshot | None:
    if not isinstance(raw_extensions, str):
        raise TerminalProtocolMalformedError("effect disclosure projection extensions are malformed")
    try:
        extensions = json.loads(raw_extensions)
    except json.JSONDecodeError as exc:
        raise TerminalProtocolMalformedError("effect disclosure projection extensions are malformed") from exc
    if not isinstance(extensions, list) or any(not isinstance(item, dict) for item in extensions):
        raise TerminalProtocolMalformedError("effect disclosure projection extensions are malformed")
    selected = [item for item in extensions if str(item.get("kind", "")).startswith("effect_disclosure_v2")]
    if not selected:
        return None
    headers = [item for item in selected if item.get("kind") == "effect_disclosure_v2"]
    members = [item for item in selected if item.get("kind") != "effect_disclosure_v2"]
    if len(headers) != 1:
        raise TerminalProtocolMalformedError("effect disclosure projection header is malformed")
    header = headers[0]
    attempt_count = header.get("attempt_count")
    if (
        header.get("protocol_version") != "2"
        or not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count < 0
        or attempt_count != len(members)
    ):
        raise TerminalProtocolMalformedError("effect disclosure projection header changed")
    attempts: list[EffectDisclosureAttempt] = []
    for member in members:
        try:
            registry_version = int(member.get("registry_contract_version"))
            outcome_value = member.get("outcome_code")
            attempt = EffectDisclosureAttempt(
                attempt_id=_required_text(
                    member.get("attempt_id"),
                    "effect disclosure frozen attempt",
                ),
                state=EffectState(
                    _required_text(
                        member.get("attempt_state"),
                        "effect disclosure frozen state",
                    )
                ),
                outcome=(EffectSettlementOutcome(outcome_value) if outcome_value is not None else None),
                provider_contract_key=_required_text(
                    member.get("provider_contract_id"),
                    "effect disclosure frozen provider",
                ),
                disclosure_kind=_required_text(
                    member.get("disclosure_kind_code"),
                    "effect disclosure frozen kind",
                ),
                registry_contract_version=registry_version,
                settlement_id=member.get("settlement_id"),
                result_digest=member.get("result_digest"),
                evidence_digest=member.get("evidence_digest"),
                provider_external_ref_digest=member.get("provider_external_ref_digest"),
            )
        except (TypeError, ValueError) as exc:
            raise TerminalProtocolMalformedError("effect disclosure frozen member is malformed") from exc
        contract = get_effect_provider_contract(attempt.provider_contract_key)
        if contract.contract_version != attempt.registry_contract_version or contract.disclosure_kind.value != attempt.disclosure_kind:
            raise TerminalProtocolMalformedError("effect disclosure frozen provider classification changed")
        attempts.append(attempt)
    snapshot = EffectDisclosureSnapshot.v2(scope=scope, attempts=tuple(attempts))
    if header.get("snapshot_digest") != snapshot.snapshot_digest:
        raise TerminalProtocolMalformedError("effect disclosure frozen snapshot digest changed")
    if tuple(sorted(selected, key=lambda item: str(item.get("kind")))) != (snapshot.projection_extensions):
        raise TerminalProtocolMalformedError("effect disclosure frozen projection is not canonical")
    return snapshot


def _validate_effect_disclosure_monotonic(
    frozen: EffectDisclosureSnapshot,
    current: EffectDisclosureSnapshot,
) -> None:
    frozen_by_id = {item.attempt_id: item for item in frozen.attempts}
    current_by_id = {item.attempt_id: item for item in current.attempts}
    if set(frozen_by_id) != set(current_by_id):
        raise TerminalProtocolMalformedError("effect disclosure attempt set changed after receipt")
    for attempt_id, before in frozen_by_id.items():
        after = current_by_id[attempt_id]
        immutable_before = (
            before.attempt_id,
            before.provider_contract_key,
            before.disclosure_kind,
            before.registry_contract_version,
        )
        immutable_after = (
            after.attempt_id,
            after.provider_contract_key,
            after.disclosure_kind,
            after.registry_contract_version,
        )
        if immutable_before != immutable_after:
            raise TerminalProtocolMalformedError("effect disclosure immutable fact changed after receipt")
        if before.state is EffectState.SETTLED:
            if before != after:
                raise TerminalProtocolMalformedError("settled effect disclosure changed after receipt")
        elif before.state is not EffectState.UNKNOWN or after.state not in {
            EffectState.UNKNOWN,
            EffectState.SETTLED,
        }:
            raise TerminalProtocolMalformedError("effect disclosure made a non-monotonic transition")


async def _effect_overlay_from_snapshot(
    connection: aiosqlite.Connection,
    *,
    snapshot: EffectDisclosureSnapshot,
    receipt_id: str | None,
    finalized: bool,
    now: datetime,
) -> EffectDisclosureOverlay:
    # INTENT has not crossed the provider boundary and is safely discardable.
    # It remains in the canonical recovery snapshot, but must not be exposed as
    # “possibly happened” in the user-facing overlay.
    visible_snapshot = EffectDisclosureSnapshot.v2(
        scope=snapshot.scope,
        attempts=tuple(attempt for attempt in snapshot.attempts if attempt.state is not EffectState.INTENT),
    )
    async with connection.execute(
        """
        SELECT MIN(started_at)
        FROM turn_effect_attempts
        WHERE turn_id=? AND generation=? AND state='unknown'
        """,
        (snapshot.scope.turn_id, snapshot.scope.generation),
    ) as cursor:
        row = await cursor.fetchone()
    oldest_unknown_attempt_started_at = row[0] if row is not None else None
    oldest_unknown_age_upper_bound_seconds = None
    if oldest_unknown_attempt_started_at is not None:
        try:
            oldest = datetime.fromisoformat(oldest_unknown_attempt_started_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise TerminalProtocolMalformedError("effect disclosure unsettled attempt time is malformed") from exc
        if oldest.tzinfo is None or oldest.utcoffset() is None:
            raise TerminalProtocolMalformedError("effect disclosure unsettled attempt time is malformed")
        oldest = oldest.astimezone(UTC)
        oldest_unknown_age_upper_bound_seconds = max(
            0,
            int((now - oldest).total_seconds()),
        )
    warnings = visible_snapshot.event_payload["warning_message_keys"]
    if not isinstance(warnings, list) or any(not isinstance(value, str) for value in warnings):
        raise TerminalProtocolMalformedError("effect disclosure warning projection is malformed")
    return EffectDisclosureOverlay(
        scope=snapshot.scope,
        receipt_id=receipt_id,
        protocol=2,
        attempt_count=visible_snapshot.attempt_count,
        counts=visible_snapshot.kind_outcome_counts,
        proof_digest=visible_snapshot.snapshot_digest,
        unknown_count=sum(item.state is EffectState.UNKNOWN for item in visible_snapshot.attempts),
        oldest_unknown_attempt_started_at=(oldest_unknown_attempt_started_at),
        oldest_unknown_age_upper_bound_seconds=(oldest_unknown_age_upper_bound_seconds),
        warning_message_keys=tuple(warnings),
        finalized=finalized,
    )


async def _read_final_effect_overlay_event(
    connection: aiosqlite.Connection,
    *,
    scope: TerminalScope,
    receipt_id: str,
    frozen: EffectDisclosureSnapshot,
) -> EffectDisclosureOverlay:
    async with connection.execute(
        """
        SELECT * FROM turn_events
        WHERE turn_id=? AND generation=? AND terminal_receipt_id=?
          AND event_type='turn.effect_disclosure_settled'
          AND protocol_version=1
        """,
        (scope.turn_id, scope.generation, receipt_id),
    ) as cursor:
        rows = await cursor.fetchall()
    if len(rows) != 1:
        raise TerminalProtocolMalformedError("effect disclosure final overlay event is missing")
    row = rows[0]
    expected_event_id = (
        "evt_"
        + canonical_digest_v1(
            {
                "event_type": "turn.effect_disclosure_settled",
                "receipt_id": receipt_id,
            }
        )[:32]
    )
    if (
        row["id"] != expected_event_id
        or row["phase_scope"] != PhaseScope.POST_TERMINAL_AUXILIARY.value
        or row["actor_scope"] != ActorScope.ROOT.value
    ):
        raise TerminalProtocolMalformedError("effect disclosure final overlay envelope is malformed")
    payload = _parse_payload(row["payload_json"])
    if canonical_digest_v1(payload) != row["payload_digest"]:
        raise TerminalProtocolMalformedError("effect disclosure final overlay digest is malformed")
    expected_keys = {
        "attempt_count",
        "counts",
        "proof_digest",
        "protocol",
        "receipt_id",
    }
    if (
        set(payload) != expected_keys
        or payload.get("protocol") != 2
        or payload.get("receipt_id") != receipt_id
        or payload.get("attempt_count") != frozen.attempt_count
        or not isinstance(payload.get("counts"), dict)
        or not isinstance(payload.get("proof_digest"), str)
    ):
        raise TerminalProtocolMalformedError("effect disclosure final overlay payload is malformed")
    counts = payload["counts"]
    if set(counts) != {"accounting", "inference_internal", "user_action"}:
        raise TerminalProtocolMalformedError("effect disclosure final overlay counts are malformed")
    try:
        return EffectDisclosureOverlay(
            scope=scope,
            receipt_id=receipt_id,
            protocol=2,
            attempt_count=frozen.attempt_count,
            counts=counts,
            proof_digest=payload["proof_digest"],
            unknown_count=0,
            oldest_unknown_attempt_started_at=None,
            oldest_unknown_age_upper_bound_seconds=None,
            warning_message_keys=(),
            finalized=True,
        )
    except (TypeError, ValueError) as exc:
        raise TerminalProtocolMalformedError("effect disclosure final overlay counts are malformed") from exc


def _validate_final_effect_overlay_snapshot(
    overlay: EffectDisclosureOverlay,
    snapshot: EffectDisclosureSnapshot,
    *,
    error: str,
) -> None:
    if (
        overlay.proof_digest != snapshot.snapshot_digest
        or overlay.attempt_count != snapshot.attempt_count
        or {kind: dict(outcomes) for kind, outcomes in overlay.counts.items()} != snapshot.kind_outcome_counts
    ):
        raise TerminalProtocolMalformedError(error)


async def _require_aggregated_effect_tombstone(
    connection: aiosqlite.Connection,
    frozen: EffectDisclosureSnapshot,
) -> None:
    async with connection.execute(
        """
        SELECT attempt_count,state
        FROM turn_effect_retention_tombstones
        WHERE team_id=? AND turn_id=? AND generation=?
        """,
        (
            frozen.scope.team_id,
            frozen.scope.turn_id,
            frozen.scope.generation,
        ),
    ) as cursor:
        rows = await cursor.fetchall()
    if len(rows) != 1 or rows[0]["state"] != "aggregated" or rows[0]["attempt_count"] != frozen.attempt_count:
        raise TerminalProtocolMalformedError("effect disclosure retained detail proof is missing")


async def _insert_or_validate_evidence_link(
    connection: aiosqlite.Connection,
    *,
    context: EffectSettlementContext,
    created_at: str,
) -> None:
    async with connection.execute(
        "SELECT * FROM turn_effect_evidence_links WHERE evidence_link_id=?",
        (context.evidence_link_id,),
    ) as cursor:
        existing = await cursor.fetchone()
    expected = {
        "team_id": context.scope.team_id,
        "audit_object_ref": context.audit_object_ref,
        "evidence_digest": context.evidence_digest,
        "object_version_digest": context.object_version_digest,
        "purge_after": context.purge_after,
    }
    if existing is not None:
        if existing["retention_state"] != "active" or any(existing[name] != value for name, value in expected.items()):
            raise TerminalProtocolMalformedError("effect evidence link identity conflict")
        return
    await connection.execute(
        """
        INSERT INTO turn_effect_evidence_links(
          evidence_link_id,team_id,audit_object_ref,evidence_digest,
          object_version_digest,retention_state,purge_after,created_at
        ) VALUES (?,?,?,?,?,'active',?,?)
        """,
        (
            context.evidence_link_id,
            context.scope.team_id,
            context.audit_object_ref,
            context.evidence_digest,
            context.object_version_digest,
            context.purge_after,
            created_at,
        ),
    )


async def _record_effect_settlement_in_tx(
    connection: aiosqlite.Connection,
    *,
    context: EffectSettlementContext,
    now: datetime,
    inject: Callable[[TerminalProtocolFaultPoint], None],
    allow_v2_manual: bool,
    allow_v2_provider_control: bool,
    allow_v2_approval_dispatched: bool,
    allow_v2_local_billing: bool,
    allow_v2_llm_completion: bool,
    allow_v2_tool_completion: bool = False,
) -> EffectAttemptContext:
    async with connection.execute(
        """
        SELECT a.*,t.session_id,w.team_id,g.execution_intent_json,
               g.state AS generation_state,g.closing_reason,
               g.terminal_receipt_id AS generation_terminal_receipt_id
        FROM turn_effect_attempts a
        JOIN turns t ON t.id=a.turn_id
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        JOIN turn_generations g
          ON g.turn_id=a.turn_id AND g.generation=a.generation
        WHERE a.attempt_id=?
        """,
        (context.attempt_id,),
    ) as cursor:
        attempt_row = await cursor.fetchone()
    if (
        attempt_row is None
        or attempt_row["turn_id"] != context.scope.turn_id
        or attempt_row["generation"] != context.scope.generation
        or attempt_row["session_id"] != context.scope.session_id
        or attempt_row["team_id"] != context.scope.team_id
    ):
        raise TerminalProtocolMalformedError("effect settlement scope mismatch")
    execution_contract = resolve_effect_execution_contract(_parse_payload(attempt_row["execution_intent_json"]))
    if allow_v2_manual and execution_contract.disposition is EffectIntentDisposition.LEGACY:
        raise TerminalProtocolMalformedError("manual effect settlement requires contract-v2")
    if execution_contract.disposition is EffectIntentDisposition.LEGACY and context.purge_after is None:
        raise TerminalProtocolMalformedError("legacy effect settlement retention is required")
    if execution_contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
        provider_key = _required_text(
            attempt_row["provider"],
            "effect settlement provider",
        )
        provider_contract = get_effect_provider_contract(provider_key)
        is_local_billing = bool(
            allow_v2_local_billing
            and provider_key.startswith("credit_ledger/")
            and provider_contract.disclosure_kind is EffectDisclosureKind.ACCOUNTING
            and provider_contract.usage_evidence is EffectUsageEvidence.DETERMINISTIC_LOCAL
            and context.principal_ref == f"adapter:{provider_contract.probe_adapter_key}"
            and context.principal_role == "provider_adapter"
        )
        is_llm_completion = bool(
            allow_v2_llm_completion
            and provider_key == "llm_provider/v1"
            and provider_contract.usage_evidence is EffectUsageEvidence.AUTHORITATIVE
            and context.principal_ref == f"adapter:{provider_contract.probe_adapter_key}"
            and context.principal_role == "provider_adapter"
        )
        is_tool_completion = bool(
            allow_v2_tool_completion
            and provider_key == "tool_external/v1"
            and provider_contract.disclosure_kind is EffectDisclosureKind.USER_ACTION
            and provider_contract.usage_evidence is EffectUsageEvidence.UNSUPPORTED
            and context.principal_ref == f"adapter:{provider_contract.outcome_classifier_key}"
            and context.principal_role == "provider_adapter"
        )
        if (
            attempt_row["state"] == EffectState.UNKNOWN.value
            and attempt_row["closing_reason"] == _CANCELLED_FINALIZATION_REASON
            and attempt_row["generation_terminal_receipt_id"] is None
            and not is_local_billing
        ):
            raise TerminalProtocolMalformedError(
                "cancelled effect settlement waits for terminal receipt",
            )
        if (
            context.source not in provider_contract.allowed_settlement_sources
            or context.outcome not in provider_contract.allowed_outcomes
            or (
                context.source is EffectSettlementSource.PROVIDER
                and (
                    not is_local_billing
                    and not is_llm_completion
                    and not is_tool_completion
                    and (
                        not allow_v2_provider_control
                        or (
                            not provider_contract.allows_control_probe
                            and not (allow_v2_approval_dispatched and provider_key == "approval_decision/v1")
                        )
                        or context.principal_ref != f"adapter:{provider_contract.probe_adapter_key}"
                        or context.principal_role != "provider_adapter"
                    )
                )
            )
            or (context.source is EffectSettlementSource.MANUAL and (not allow_v2_manual or not provider_contract.allows_manual_settlement))
        ):
            raise TerminalProtocolMalformedError("effect settlement is not allowed by provider contract")
        if context.purge_after is not None:
            raise TerminalProtocolMalformedError("effect control retention must be store-owned")
        _validate_v2_effect_control_context_identity(
            context,
            provider_contract.key,
        )
    async with connection.execute(
        "SELECT * FROM turn_effect_settlements WHERE attempt_id=?",
        (context.attempt_id,),
    ) as cursor:
        existing = await cursor.fetchone()
    if existing is not None:
        _validate_effect_settlement_replay(existing, context)
        async with connection.execute(
            """
            SELECT s.settlement_id AS persisted_settlement_id,
                   s.attempt_id,
                   s.source AS settlement_source,s.outcome,s.settlement_code,
                   s.actor_audit_ref,s.provider_external_ref_digest,
                   s.provider_external_ref_key_version,
                   s.created_at AS settlement_created_at,
                   e.evidence_link_id,e.evidence_digest,
                   e.object_version_digest,e.audit_object_ref,
                   e.retention_state AS evidence_retention_state,
                   e.purge_after AS evidence_purge_after,
                   e.purge_request_id,e.created_at AS evidence_created_at
            FROM turn_effect_settlements s
            JOIN turn_effect_evidence_links e
              ON e.evidence_link_id=s.evidence_link_id
            WHERE s.attempt_id=?
            """,
            (context.attempt_id,),
        ) as cursor:
            evidence = await cursor.fetchone()
        if evidence is None:
            raise TerminalProtocolMalformedError("effect settlement evidence is missing")
        if execution_contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
            context = replace(
                context,
                purge_after=evidence["evidence_purge_after"],
            )
            _validate_v2_effect_control_evidence(
                evidence,
                provider_contract.key,
            )
        await _validate_effect_evidence_replay(connection, context)
        result = _effect_attempt_from_row(context.scope, attempt_row)
        if result.state is not EffectState.SETTLED or result.row_version != context.expected_attempt_row_version + 1:
            raise TerminalProtocolMalformedError("effect settlement winner is not bound to attempt")
        _validate_settled_effect_projection(
            attempt_row,
            persisted_settlement_id=context.settlement_id,
            settlement_code=context.settlement_code,
            outcome=context.outcome.value,
            evidence_digest=context.evidence_digest,
        )
        await _persist_final_effect_overlay_if_ready(
            connection,
            scope=context.scope,
            now=now,
            inject=inject,
        )
        return result
    if execution_contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
        if context.source is EffectSettlementSource.PROVIDER and attempt_row["state"] not in (
            {EffectState.UNKNOWN.value, EffectState.DISPATCHED.value}
            if is_local_billing
            or is_llm_completion
            or is_tool_completion
            or (allow_v2_approval_dispatched and provider_key == "approval_decision/v1")
            else {EffectState.UNKNOWN.value}
        ):
            raise TerminalProtocolMalformedError("provider control settlement requires unknown attempt")
        if context.source is EffectSettlementSource.MANUAL and attempt_row["state"] != EffectState.UNKNOWN.value:
            raise TerminalProtocolMalformedError("manual effect settlement requires unknown attempt")
    if (
        attempt_row["state"] not in {EffectState.DISPATCHED, EffectState.UNKNOWN}
        or attempt_row["row_version"] != context.expected_attempt_row_version
    ):
        raise TerminalProtocolMalformedError("effect settlement attempt fence changed")
    if execution_contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
        context = replace(
            context,
            purge_after=_iso_utc(now + timedelta(days=30)),
        )
    now_text = _iso_utc(now)
    await _insert_or_validate_evidence_link(
        connection,
        context=context,
        created_at=now_text,
    )
    inject(TerminalProtocolFaultPoint.EFFECT_SETTLEMENT_AFTER_EVIDENCE_LINK)
    settlement_insert = await connection.execute(
        """
        INSERT INTO turn_effect_settlements(
          settlement_id,attempt_id,source,outcome,settlement_code,
          actor_audit_ref,authz_snapshot_digest,provider_external_ref_digest,
          provider_external_ref_key_version,evidence_link_id,evidence_digest,
          evidence_recorded_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            context.settlement_id,
            context.attempt_id,
            context.source.value,
            context.outcome.value,
            context.settlement_code,
            context.principal_ref,
            context.authz_snapshot_digest,
            context.provider_external_ref_digest,
            context.provider_external_ref_key_version,
            context.evidence_link_id,
            context.evidence_digest,
            now_text,
            now_text,
        ),
    )
    if settlement_insert.rowcount != 1:
        raise TerminalProtocolMalformedError("effect settlement insert failed")
    inject(TerminalProtocolFaultPoint.EFFECT_SETTLEMENT_AFTER_INSERT)
    result_digest = _effect_settlement_result_digest(
        attempt_id=context.attempt_id,
        evidence_digest=context.evidence_digest,
        outcome=context.outcome.value,
        settlement_code=context.settlement_code,
        settlement_id=context.settlement_id,
    )
    inject(TerminalProtocolFaultPoint.EFFECT_SETTLEMENT_BEFORE_ATTEMPT_CAS)
    update = await connection.execute(
        """
        UPDATE turn_effect_attempts
        SET state='settled',row_version=row_version+1,settled_at=?,
            result_digest=?,settlement_id=?
        WHERE attempt_id=? AND row_version=? AND state IN ('dispatched','unknown')
        """,
        (
            now_text,
            result_digest,
            context.settlement_id,
            context.attempt_id,
            context.expected_attempt_row_version,
        ),
    )
    if update.rowcount != 1:
        raise TerminalProtocolMalformedError("effect settlement attempt CAS changed")
    settled_row = dict(attempt_row)
    settled_row.update(
        {
            "state": EffectState.SETTLED.value,
            "row_version": context.expected_attempt_row_version + 1,
            "settlement_id": context.settlement_id,
        }
    )
    result = _effect_attempt_from_mapping(context.scope, settled_row)
    await _persist_final_effect_overlay_if_ready(
        connection,
        scope=context.scope,
        now=now,
        inject=inject,
    )
    return result


async def _persist_final_effect_overlay_if_ready(
    connection: aiosqlite.Connection,
    *,
    scope: TerminalScope,
    now: datetime,
    inject: Callable[[TerminalProtocolFaultPoint], None],
) -> TerminalProtocolEventRow | None:
    async with connection.execute(
        """
        SELECT r.receipt_id,r.status,c.projection_extensions_json,
               t.delivery_fence_epoch
        FROM turn_terminal_receipts r
        JOIN turn_terminal_candidates c ON c.candidate_id=r.candidate_id
        JOIN turns t ON t.id=r.turn_id
        WHERE r.turn_id=? AND r.generation=?
        """,
        (scope.turn_id, scope.generation),
    ) as cursor:
        receipt = await cursor.fetchone()
    if receipt is None or receipt["status"] != TerminalStatus.CANCELLED.value:
        return None
    frozen = _effect_disclosure_snapshot_from_extensions(
        scope,
        receipt["projection_extensions_json"],
    )
    if frozen is None or not frozen.unsettled_count:
        return None
    current = await _read_effect_disclosure_snapshot_v2(connection, scope)
    _validate_effect_disclosure_monotonic(frozen, current)
    if current.unsettled_count:
        return None
    receipt_id = _required_text(
        receipt["receipt_id"],
        "effect disclosure final receipt",
    )
    event_id = (
        "evt_"
        + canonical_digest_v1(
            {
                "event_type": "turn.effect_disclosure_settled",
                "receipt_id": receipt_id,
            }
        )[:32]
    )
    payload = {
        "attempt_count": current.attempt_count,
        "counts": current.kind_outcome_counts,
        "proof_digest": current.snapshot_digest,
        "protocol": 2,
        "receipt_id": receipt_id,
    }
    inject(TerminalProtocolFaultPoint.EFFECT_FINAL_OVERLAY_BEFORE_EVENT)
    return await _append_post_terminal_auxiliary_in_tx(
        connection,
        context=PostTerminalAuxiliaryContext(
            scope=scope,
            receipt_id=receipt_id,
            event_id=event_id,
            expected_delivery_fence_epoch=_nonnegative_int(
                receipt["delivery_fence_epoch"],
                "effect disclosure delivery fence",
            ),
            auth_fence_vector_digest="effect_settlement_capability",
        ),
        event_type="turn.effect_disclosure_settled",
        actor_scope=ActorScope.ROOT,
        payload=payload,
        occurred_at=_iso_utc(now),
    )


async def _validate_effect_evidence_replay(
    connection: aiosqlite.Connection,
    context: EffectSettlementContext,
) -> None:
    async with connection.execute(
        "SELECT * FROM turn_effect_evidence_links WHERE evidence_link_id=?",
        (context.evidence_link_id,),
    ) as cursor:
        evidence = await cursor.fetchone()
    if evidence is None:
        raise TerminalProtocolMalformedError("effect settlement evidence is missing")
    expected = {
        "team_id": context.scope.team_id,
        "evidence_digest": context.evidence_digest,
        "object_version_digest": context.object_version_digest,
        "purge_after": context.purge_after,
    }
    if any(evidence[name] != value for name, value in expected.items()):
        raise TerminalProtocolMalformedError("effect settlement evidence identity conflict")
    if evidence["retention_state"] == "purged":
        if evidence["audit_object_ref"] is not None or not _is_aware_timestamp(evidence["purged_at"]):
            raise TerminalProtocolMalformedError("purged effect settlement evidence is malformed")
    elif evidence["retention_state"] in {"active", "purge_pending"}:
        if evidence["audit_object_ref"] != context.audit_object_ref or evidence["purged_at"] is not None:
            raise TerminalProtocolMalformedError("effect settlement evidence reference conflict")
    else:
        raise TerminalProtocolMalformedError("effect settlement evidence retention state is malformed")


def _validate_effect_settlement_replay(
    row: aiosqlite.Row,
    context: EffectSettlementContext,
) -> None:
    expected = {
        "settlement_id": context.settlement_id,
        "attempt_id": context.attempt_id,
        "source": context.source.value,
        "outcome": context.outcome.value,
        "settlement_code": context.settlement_code,
        "actor_audit_ref": context.principal_ref,
        "authz_snapshot_digest": context.authz_snapshot_digest,
        "provider_external_ref_digest": context.provider_external_ref_digest,
        "provider_external_ref_key_version": context.provider_external_ref_key_version,
        "evidence_link_id": context.evidence_link_id,
        "evidence_digest": context.evidence_digest,
    }
    if any(row[name] != value for name, value in expected.items()):
        raise TerminalProtocolMalformedError("effect settlement winner conflict")


def _validate_v2_effect_control_context_identity(
    context: EffectSettlementContext,
    provider_contract_key: str,
) -> None:
    """Reject a control write the exact reader could not later authenticate."""

    identity_fields = {
        "attempt_id": context.attempt_id,
        "provider_contract_key": provider_contract_key,
        "source": context.source.value,
        "outcome": context.outcome.value,
        "settlement_code": context.settlement_code,
        "evidence_digest": context.evidence_digest,
        "object_version_digest": context.object_version_digest,
        "actor_audit_ref": context.principal_ref,
        "provider_external_ref_digest": context.provider_external_ref_digest,
        "provider_external_ref_key_version": (context.provider_external_ref_key_version),
    }
    try:
        audit_identity = (
            effect_control_identity_digest(
                **identity_fields,
                audit_object_ref=None,
            )
            if context.source is EffectSettlementSource.MANUAL
            else None
        )
        identity = effect_control_identity_digest(
            **identity_fields,
            audit_object_ref=context.audit_object_ref,
        )[:32]
    except ValueError as exc:
        raise TerminalProtocolMalformedError("effect settlement evidence control identity is malformed") from exc
    if audit_identity is not None and context.audit_object_ref != f"admin-attestation:{audit_identity}":
        raise TerminalProtocolMalformedError("manual effect settlement audit identity changed")
    if context.settlement_id != f"set_{identity}" or context.evidence_link_id != f"evidence_{identity}":
        raise TerminalProtocolMalformedError("effect settlement evidence control identity changed")


def _record_effect_resolution_metric(*, source: str, outcome: str) -> None:
    """Count only the transaction that changed UNKNOWN to SETTLED."""

    try:
        from bowhead_core import metrics

        metrics.inc(
            "bowhead_terminal_effect_resolution_total",
            labels={"source": source, "outcome": outcome},
            help="UNKNOWN effects resolved through provider probe or manual control",
        )
    except Exception:  # noqa: S110
        pass


def _validate_v2_effect_control_evidence(
    row: Mapping[str, object],
    provider_contract_key: str,
) -> None:
    """Cross-bind exact control metadata using existing stable identities."""

    settlement_created_at = _aware_timestamp(row["settlement_created_at"])
    evidence_created_at = _aware_timestamp(row["evidence_created_at"])
    purge_after = _aware_timestamp(row["evidence_purge_after"])
    if settlement_created_at is None or evidence_created_at != settlement_created_at or purge_after != settlement_created_at + timedelta(days=30):
        raise TerminalProtocolMalformedError("effect settlement evidence retention identity changed")
    # Purge intentionally erases the external locator. Before purge, bind all
    # exact fields (including that locator) to the immutable settlement/link IDs.
    if row["evidence_retention_state"] == "purged":
        if row["purge_request_id"] != (f"{row['evidence_link_id']}:{row['object_version_digest']}"):
            raise TerminalProtocolMalformedError("purged effect settlement evidence identity changed")
        return
    source = _required_text(
        row["settlement_source"],
        "effect settlement source",
    )
    actor_audit_ref = _required_text(
        row["actor_audit_ref"],
        "effect settlement actor",
    )
    audit_object_ref = row["audit_object_ref"]
    identity_fields = {
        "attempt_id": _required_text(row["attempt_id"], "effect attempt"),
        "provider_contract_key": provider_contract_key,
        "source": source,
        "outcome": _required_text(row["outcome"], "effect settlement outcome"),
        "settlement_code": _required_text(
            row["settlement_code"],
            "effect settlement code",
        ),
        "evidence_digest": _required_text(
            row["evidence_digest"],
            "effect settlement evidence",
        ),
        "object_version_digest": _required_text(
            row["object_version_digest"],
            "effect settlement object version",
        ),
        "actor_audit_ref": actor_audit_ref,
        "provider_external_ref_digest": row["provider_external_ref_digest"],
        "provider_external_ref_key_version": row["provider_external_ref_key_version"],
    }
    try:
        audit_identity = (
            effect_control_identity_digest(
                **identity_fields,
                audit_object_ref=None,
            )
            if source == EffectSettlementSource.MANUAL.value
            else None
        )
        identity = effect_control_identity_digest(
            **identity_fields,
            audit_object_ref=audit_object_ref,
        )[:32]
    except ValueError as exc:
        raise TerminalProtocolMalformedError("effect settlement evidence control identity is malformed") from exc
    if audit_identity is not None and audit_object_ref != f"admin-attestation:{audit_identity}":
        raise TerminalProtocolMalformedError("manual effect settlement audit identity changed")
    if row["persisted_settlement_id"] != f"set_{identity}" or row["evidence_link_id"] != f"evidence_{identity}":
        raise TerminalProtocolMalformedError("effect settlement evidence control identity changed")


def _effect_settlement_result_digest(
    *,
    attempt_id: str,
    evidence_digest: str,
    outcome: str,
    settlement_code: str,
    settlement_id: str,
) -> str:
    return canonical_digest_v1(
        {
            "attempt_id": attempt_id,
            "evidence_digest": evidence_digest,
            "outcome": outcome,
            "settlement_code": settlement_code,
            "settlement_id": settlement_id,
        }
    )


def _validate_settled_effect_projection(
    row: Mapping[str, object],
    *,
    persisted_settlement_id: object | None = None,
    settlement_code: object | None = None,
    outcome: object | None = None,
    evidence_digest: object | None = None,
) -> None:
    persisted = row["persisted_settlement_id"] if persisted_settlement_id is None else persisted_settlement_id
    code = row["settlement_code"] if settlement_code is None else settlement_code
    settled_outcome = row["outcome"] if outcome is None else outcome
    evidence = row["evidence_digest"] if evidence_digest is None else evidence_digest
    if any(not isinstance(value, str) or not value for value in (persisted, code, settled_outcome, evidence)):
        raise TerminalProtocolMalformedError("settled effect projection is incomplete")
    if row["settlement_id"] != persisted or row["result_digest"] != _effect_settlement_result_digest(
        attempt_id=_required_text(
            row["attempt_id"],
            "effect attempt identity",
        ),
        evidence_digest=evidence,
        outcome=settled_outcome,
        settlement_code=code,
        settlement_id=persisted,
    ):
        raise TerminalProtocolMalformedError("settled effect projection identity changed")


async def _recovery_kind_for_generation(
    connection: aiosqlite.Connection,
    row: aiosqlite.Row,
) -> RecoveryKind:
    async with connection.execute(
        """
        SELECT 1 FROM turn_effect_attempts
        WHERE turn_id=? AND generation=? AND effect_class='side_effect_possible'
          AND state IN ('dispatched','unknown')
        LIMIT 1
        """,
        (row["turn_id"], row["generation"]),
    ) as cursor:
        if await cursor.fetchone() is not None:
            return RecoveryKind.EFFECT_UNKNOWN
    state = _generation_state(row["state"])
    if state is GenerationState.OPENED:
        return RecoveryKind.OPENED_OWNER_LOST
    if state is GenerationState.RUNNING:
        return RecoveryKind.EXECUTING_OWNER_LOST
    if state is GenerationState.CLOSING:
        return RecoveryKind.FINALIZATION_UNKNOWN
    if state is GenerationState.RECOVERY_PENDING:
        try:
            return RecoveryKind(row["recovery_reason"])
        except (TypeError, ValueError) as exc:
            raise TerminalProtocolMalformedError("recovery generation reason is malformed") from exc
    raise TerminalProtocolMalformedError("closed generation cannot enter recovery")


async def _discover_recovery_in_tx(
    connection: aiosqlite.Connection,
    *,
    context: RecoveryDiscoveryContext,
    generation: aiosqlite.Row,
    now: datetime,
    inject: Callable[[TerminalProtocolFaultPoint], None],
) -> RecoveryRecord:
    """Shared connection-local recovery kernel; caller owns BEGIN/COMMIT."""

    if generation["terminal_receipt_id"] is not None or generation["recovery_resolution"] is not None:
        raise TerminalProtocolMalformedError("recovery generation already converged")
    active = await _read_active_generation_recovery(
        connection,
        context.scope,
    )
    if generation["state"] == GenerationState.RECOVERY_PENDING and active is not None:
        if active["kind"] == RecoveryKind.EFFECT_UNKNOWN.value:
            sealed = await _seal_contract_v2_dispatched_effects_unknown(
                connection,
                generation=generation,
                scope=context.scope,
            )
            if sealed:
                inject(TerminalProtocolFaultPoint.EFFECT_UNKNOWN_AFTER_ATTEMPT_CAS)
        return _recovery_record_from_row(
            context.scope,
            active,
            generation,
            created=False,
        )
    if (
        generation["state"] != context.observed_state
        or generation["row_version"] != context.observed_generation_row_version
        or generation["claim_epoch"] != context.observed_execution_claim_epoch
        or generation["finalization_epoch"] != context.observed_finalization_epoch
    ):
        if active is None:
            raise TerminalProtocolMalformedError("recovery discovery snapshot changed")
        return _recovery_record_from_row(
            context.scope,
            active,
            generation,
            created=False,
        )
    kind = await _recovery_kind_for_generation(connection, generation)
    if kind is RecoveryKind.EFFECT_UNKNOWN:
        sealed = await _seal_contract_v2_dispatched_effects_unknown(
            connection,
            generation=generation,
            scope=context.scope,
        )
        if sealed:
            inject(TerminalProtocolFaultPoint.EFFECT_UNKNOWN_AFTER_ATTEMPT_CAS)
    discovery_identity = canonical_digest_v1(
        {
            "claim_epoch": context.observed_execution_claim_epoch,
            "finalization_epoch": context.observed_finalization_epoch,
            "generation": context.scope.generation,
            "row_version": context.observed_generation_row_version,
            "state": context.observed_state.value,
            "turn_id": context.scope.turn_id,
        }
    )
    async with connection.execute(
        """
        SELECT * FROM turn_recovery_records
        WHERE scope='terminal' AND discovery_identity=?
        """,
        (discovery_identity,),
    ) as cursor:
        replay = await cursor.fetchone()
    if replay is not None:
        return _recovery_record_from_row(
            context.scope,
            replay,
            generation,
            created=False,
        )
    now_text = _iso_utc(now)
    recovery_id = f"rec_{uuid4().hex}"
    recovery_insert = await connection.execute(
        """
        INSERT INTO turn_recovery_records(
          recovery_id,turn_id,generation,scope,kind,phase,state,
          discovery_identity,discovered_generation_row_version,
          recovery_claim_epoch,attempt_count,next_attempt_at,last_code,created_at
        ) VALUES (?,?,?,'terminal',?,'discovered','active',?,?,0,0,?,NULL,?)
        """,
        (
            recovery_id,
            context.scope.turn_id,
            context.scope.generation,
            kind.value,
            discovery_identity,
            context.observed_generation_row_version,
            now_text,
            now_text,
        ),
    )
    if recovery_insert.rowcount != 1:
        raise TerminalProtocolMalformedError("recovery record insert failed")
    inject(TerminalProtocolFaultPoint.RECOVERY_DISCOVERY_AFTER_ROW_CREATE)
    accepted_through = (
        _nonnegative_int(
            generation["closing_accepted_through"],
            "generation.closing_accepted_through",
        )
        if generation["closing_accepted_through"] is not None
        else _positive_int(
            generation["accepted_proposal_next"],
            "generation.accepted_proposal_next",
        )
        - 1
    )
    next_finalization_epoch = context.observed_finalization_epoch + 1
    update = await connection.execute(
        """
        UPDATE turn_generations
        SET state='recovery_pending',row_version=row_version+1,
            closing_accepted_through=?,recovery_started_at=?,recovery_reason=?,
            finalization_owner_instance_id=NULL,
            finalization_epoch=?,finalization_lease_expires_at=NULL
        WHERE turn_id=? AND generation=? AND protocol_version=1
          AND state=? AND row_version=? AND claim_epoch=? AND finalization_epoch=?
          AND terminal_receipt_id IS NULL AND recovery_resolution IS NULL
        """,
        (
            accepted_through,
            now_text,
            kind.value,
            next_finalization_epoch,
            context.scope.turn_id,
            context.scope.generation,
            context.observed_state.value,
            context.observed_generation_row_version,
            context.observed_execution_claim_epoch,
            context.observed_finalization_epoch,
        ),
    )
    if update.rowcount != 1:
        raise TerminalProtocolMalformedError("recovery generation CAS changed")
    inject(TerminalProtocolFaultPoint.RECOVERY_AFTER_STATE_CAS)
    session_update = await connection.execute(
        """
        UPDATE sessions
        SET recovery_lock_turn_id=?,recovery_lock_generation=?,recovery_lock_reason=?,
            steer_buffer_state='frozen',steer_freeze_turn_id=?,
            steer_freeze_generation=?,steer_freeze_claim_epoch=?,
            steer_frozen_revision=steer_buffer_revision
        WHERE id=? AND recovery_lock_turn_id IS NULL AND (
            steer_buffer_state='collecting' OR (
              steer_buffer_state='frozen' AND steer_freeze_turn_id=?
              AND steer_freeze_generation=? AND steer_freeze_claim_epoch=?
              AND steer_frozen_revision=steer_buffer_revision
            )
          )
        """,
        (
            context.scope.turn_id,
            context.scope.generation,
            _recovery_lock_reason(kind),
            context.scope.turn_id,
            context.scope.generation,
            context.observed_execution_claim_epoch,
            context.scope.session_id,
            context.scope.turn_id,
            context.scope.generation,
            context.observed_execution_claim_epoch,
        ),
    )
    if session_update.rowcount != 1:
        raise TerminalProtocolMalformedError("recovery Session lock CAS changed")
    generation_projection = dict(generation)
    generation_projection.update(
        {
            "row_version": context.observed_generation_row_version + 1,
            "finalization_epoch": next_finalization_epoch,
            "closing_accepted_through": accepted_through,
        }
    )
    return _recovery_record_from_mapping(
        context.scope,
        {
            "recovery_id": recovery_id,
            "kind": kind.value,
            "discovery_identity": discovery_identity,
            "recovery_claim_epoch": 0,
            "recovery_owner_instance_id": None,
            "recovery_lease_expires_at": None,
        },
        generation_projection,
        created=True,
    )


async def _read_active_generation_recovery(
    connection: aiosqlite.Connection,
    scope: TerminalScope,
) -> aiosqlite.Row | None:
    async with connection.execute(
        """
        SELECT * FROM turn_recovery_records
        WHERE turn_id=? AND generation=? AND scope='terminal' AND state='active'
        """,
        (scope.turn_id, scope.generation),
    ) as cursor:
        return await cursor.fetchone()


async def _seal_contract_v2_dispatched_effects_unknown(
    connection: aiosqlite.Connection,
    *,
    generation: aiosqlite.Row,
    scope: TerminalScope,
) -> int:
    """Freeze every pre-recovery v2 dispatch; legacy rows keep old semantics."""

    contract = resolve_effect_execution_contract(
        _parse_payload(generation["execution_intent_json"]),
    )
    if contract.disposition is EffectIntentDisposition.LEGACY:
        return 0
    update = await connection.execute(
        """
        UPDATE turn_effect_attempts
        SET state='unknown',row_version=row_version+1
        WHERE turn_id=? AND generation=? AND state='dispatched'
        """,
        (scope.turn_id, scope.generation),
    )
    return update.rowcount


def _recovery_lock_reason(kind: RecoveryKind) -> str:
    if kind is RecoveryKind.EFFECT_UNKNOWN:
        return "effect_outcome_unknown"
    if kind is RecoveryKind.DEPENDENCY_MISSING:
        return "dependency_missing"
    if kind is RecoveryKind.NON_DURABLE:
        return "non_durable"
    if kind is RecoveryKind.FINALIZATION_UNKNOWN:
        return "commit_outcome_unknown"
    return "owner_lost"


def _recovery_record_from_row(
    scope: TerminalScope,
    recovery: aiosqlite.Row,
    generation: aiosqlite.Row,
    *,
    created: bool,
) -> RecoveryRecord:
    return _recovery_record_from_mapping(scope, dict(recovery), dict(generation), created=created)


def _recovery_record_from_mapping(
    scope: TerminalScope,
    recovery: Mapping[str, object],
    generation: Mapping[str, object],
    *,
    created: bool,
) -> RecoveryRecord:
    try:
        kind = RecoveryKind(recovery["kind"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TerminalProtocolMalformedError("recovery kind is malformed") from exc
    owner = _optional_nonempty(
        recovery.get("recovery_owner_instance_id"),
        "recovery.recovery_owner_instance_id",
    )
    lease = _optional_nonempty(
        recovery.get("recovery_lease_expires_at"),
        "recovery.recovery_lease_expires_at",
    )
    return RecoveryRecord(
        scope=scope,
        recovery_id=_required_text(recovery.get("recovery_id"), "recovery.recovery_id"),
        kind=kind,
        discovery_identity=_required_text(
            recovery.get("discovery_identity"),
            "recovery.discovery_identity",
        ),
        generation_row_version=_positive_int(
            generation.get("row_version"),
            "generation.row_version",
        ),
        recovery_claim_epoch=_nonnegative_int(
            recovery.get("recovery_claim_epoch"),
            "recovery.recovery_claim_epoch",
        ),
        owner_instance_id=owner,
        recovery_lease_expires_at=lease,
        finalization_epoch=_nonnegative_int(
            generation.get("finalization_epoch"),
            "generation.finalization_epoch",
        ),
        created=created,
    )


def _recovery_processing_from_rows(
    scope: TerminalScope,
    recovery: aiosqlite.Row,
    generation: aiosqlite.Row,
) -> RecoveryProcessingContext:
    owner = _required_text(
        recovery["recovery_owner_instance_id"],
        "recovery.recovery_owner_instance_id",
    )
    lease = _required_text(
        recovery["recovery_lease_expires_at"],
        "recovery.recovery_lease_expires_at",
    )
    if generation["finalization_owner_instance_id"] != owner:
        raise TerminalProtocolMalformedError("recovery finalization owner is malformed")
    return RecoveryProcessingContext(
        scope=scope,
        recovery_id=_required_text(recovery["recovery_id"], "recovery.recovery_id"),
        recovery_owner_instance_id=owner,
        recovery_claim_epoch=_positive_int(
            recovery["recovery_claim_epoch"],
            "recovery.recovery_claim_epoch",
        ),
        recovery_lease_expires_at=lease,
        finalization_epoch=_positive_int(
            generation["finalization_epoch"],
            "generation.finalization_epoch",
        ),
        finalization_lease_expires_at=_required_text(
            generation["finalization_lease_expires_at"],
            "generation.finalization_lease_expires_at",
        ),
        generation_row_version=_positive_int(generation["row_version"], "generation.row_version"),
        closing_accepted_through=_nonnegative_int(
            generation["closing_accepted_through"],
            "generation.closing_accepted_through",
        ),
        cancelled_finalization=(generation["closing_reason"] == _CANCELLED_FINALIZATION_REASON),
    )


async def _read_active_recovery(
    connection: aiosqlite.Connection,
    recovery_id: str,
) -> aiosqlite.Row | None:
    async with connection.execute(
        "SELECT * FROM turn_recovery_records WHERE recovery_id=? AND scope='terminal' AND state='active'",
        (recovery_id,),
    ) as cursor:
        return await cursor.fetchone()


def _recovery_processing_matches(
    generation: aiosqlite.Row | None,
    recovery: aiosqlite.Row | None,
    context: RecoveryProcessingContext,
    now: datetime,
) -> bool:
    if generation is None or recovery is None:
        return False
    return bool(
        generation["terminal_protocol_version"] == 1
        and generation["protocol_version"] == 1
        and generation["current_generation"] == context.scope.generation
        and generation["state"] == GenerationState.RECOVERY_PENDING
        and generation["row_version"] == context.generation_row_version
        and generation["finalization_owner_instance_id"] == context.recovery_owner_instance_id
        and generation["finalization_epoch"] == context.finalization_epoch
        and generation["closing_accepted_through"] == context.closing_accepted_through
        and (generation["closing_reason"] == _CANCELLED_FINALIZATION_REASON) is context.cancelled_finalization
        and generation["terminal_receipt_id"] is None
        and generation["team_id"] == context.scope.team_id
        and generation["session_id"] == context.scope.session_id
        and _recovery_session_lock_matches(generation, recovery, context.scope)
        and recovery["turn_id"] == context.scope.turn_id
        and recovery["generation"] == context.scope.generation
        and recovery["recovery_id"] == context.recovery_id
        and recovery["recovery_owner_instance_id"] == context.recovery_owner_instance_id
        and recovery["recovery_claim_epoch"] == context.recovery_claim_epoch
        and _timestamp_after(generation["finalization_lease_expires_at"], now)
        and _timestamp_after(recovery["recovery_lease_expires_at"], now)
    )


def _recovery_session_lock_matches(
    generation: aiosqlite.Row,
    recovery: aiosqlite.Row,
    scope: TerminalScope,
) -> bool:
    try:
        expected_reason = _recovery_lock_reason(RecoveryKind(recovery["kind"]))
    except (TypeError, ValueError):
        return False
    return bool(
        generation["recovery_lock_turn_id"] == scope.turn_id
        and generation["recovery_lock_generation"] == scope.generation
        and generation["recovery_lock_reason"] == expected_reason
    )


async def _require_effects_safe_for_recovery(
    connection: aiosqlite.Connection,
    scope: TerminalScope,
    *,
    allow_cancelled_unknown: bool = False,
) -> None:
    async with connection.execute(
        """
        SELECT state FROM turn_effect_attempts
        WHERE turn_id=? AND generation=? AND effect_class='side_effect_possible'
        ORDER BY attempt_id
        """,
        (scope.turn_id, scope.generation),
    ) as cursor:
        states = tuple(EffectState(row["state"]) for row in await cursor.fetchall())
    if allow_cancelled_unknown:
        if all(state in {EffectState.UNKNOWN, EffectState.SETTLED} for state in states):
            return
        raise TerminalProtocolMalformedError(
            "cancelled recovery has an unsealed side effect",
        )
    if not recovery_effects_safe(states):
        raise TerminalProtocolMalformedError("recovery is blocked by unsettled side effects")


def _finalization_from_recovery(
    context: RecoveryProcessingContext,
    generation: aiosqlite.Row,
) -> FinalizationContext:
    return FinalizationContext(
        scope=context.scope,
        execution_owner_instance_id=_required_text(
            generation["owner_instance_id"] or "recovery-ownerless",
            "generation.owner_instance_id",
        ),
        execution_claim_epoch=_nonnegative_int(generation["claim_epoch"], "generation.claim_epoch"),
        finalization_owner_instance_id=context.recovery_owner_instance_id,
        finalization_epoch=context.finalization_epoch,
        generation_row_version=context.generation_row_version,
        closing_accepted_through=context.closing_accepted_through,
        lease_expires_at=context.finalization_lease_expires_at,
    )


async def _validate_recovery_persisted_prefix(
    connection: aiosqlite.Connection,
    context: RecoveryProcessingContext,
    generation: aiosqlite.Row,
) -> None:
    proposals = await _read_event_proposal_prefix(
        connection,
        scope=context.scope,
        accepted_through=context.closing_accepted_through,
    )
    _validate_event_proposal_prefix(
        proposals,
        context=_finalization_from_recovery(context, generation),
        accepted_through=context.closing_accepted_through,
    )
    if any(proposal["state"] != "persisted" for proposal in proposals):
        raise TerminalProtocolMalformedError("recovery accepted fence is not persisted")


async def _load_terminal_candidate(
    connection: aiosqlite.Connection,
    scope: TerminalScope,
    candidate_id: str,
) -> TerminalCandidatePlan:
    async with connection.execute(
        "SELECT * FROM turn_terminal_candidates WHERE candidate_id=? AND turn_id=? AND generation=?",
        (candidate_id, scope.turn_id, scope.generation),
    ) as cursor:
        candidate = await cursor.fetchone()
    if candidate is None or candidate["state"] not in {"prepared", "committed"}:
        raise TerminalProtocolMalformedError("terminal candidate is missing or unusable")
    session_plan = _parse_payload(candidate["session_disposition_plan_json"])
    try:
        extensions_value = json.loads(candidate["projection_extensions_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise TerminalProtocolMalformedError("terminal candidate extensions are malformed") from exc
    if not isinstance(extensions_value, list) or any(not isinstance(value, dict) for value in extensions_value):
        raise TerminalProtocolMalformedError("terminal candidate extensions are malformed")
    async with connection.execute(
        "SELECT * FROM turn_terminal_candidate_variants WHERE candidate_id=? ORDER BY variant_key",
        (candidate_id,),
    ) as cursor:
        variant_rows = await cursor.fetchall()
    templates = session_plan.get("variant_templates")
    if not isinstance(templates, dict):
        raise TerminalProtocolMalformedError("terminal candidate variant templates are malformed")
    variants: list[TerminalCandidateVariant] = []
    for variant_row in variant_rows:
        key = _required_text(variant_row["variant_key"], "candidate_variant.variant_key")
        template = templates.get(key)
        if not isinstance(template, dict):
            raise TerminalProtocolMalformedError("terminal candidate variant template is missing")
        async with connection.execute(
            """
            SELECT * FROM turn_terminal_candidate_events
            WHERE candidate_id=? AND variant_key=? ORDER BY ordinal
            """,
            (candidate_id, key),
        ) as cursor:
            event_rows = await cursor.fetchall()
        events = tuple(
            StagedTerminalEvent(
                event_id=_required_text(event["event_id"], "candidate_event.event_id"),
                event_type=_required_text(event["event_type"], "candidate_event.event_type"),
                actor_scope=ActorScope(event["actor_scope"]),
                phase_scope=PhaseScope(event["phase_scope"]),
                payload_json=_required_text(event["payload_json"], "candidate_event.payload_json"),
                payload_digest=_required_text(event["payload_digest"], "candidate_event.payload_digest"),
            )
            for event in event_rows
        )
        variants.append(
            TerminalCandidateVariant(
                variant_key=key,
                outcome_variant=CandidateOutcome(variant_row["outcome_variant"]),
                session_variant=SessionVariant(variant_row["session_variant"]),
                status=TerminalStatus(variant_row["status"]),
                reason_code=variant_row["reason_code"],
                message_key=_required_text(variant_row["message_key"], "candidate_variant.message_key"),
                final_answer_mode=FinalAnswerMode(variant_row["final_answer_mode"]),
                final_answer_event_id=variant_row["final_answer_event_id"],
                final_answer_digest=variant_row["final_answer_digest"],
                operation_kind=variant_row["operation_kind"],
                session_mutation_template=template,
                events=events,
            )
        )
    plan = TerminalCandidatePlan(
        scope=scope,
        candidate_id=candidate_id,
        reserved_receipt_id=_required_text(candidate["reserved_receipt_id"], "candidate.reserved_receipt_id"),
        reserved_terminal_event_id=_required_text(
            candidate["reserved_terminal_event_id"],
            "candidate.reserved_terminal_event_id",
        ),
        origin=CandidateOrigin(candidate["candidate_origin"]),
        closing_accepted_through=_nonnegative_int(
            candidate["closing_accepted_through"],
            "candidate.closing_accepted_through",
        ),
        persisted_event_highwater=_nonnegative_int(
            candidate["persisted_event_highwater"],
            "candidate.persisted_event_highwater",
        ),
        variants=tuple(variants),
        session_disposition_plan=session_plan,
        projection_extensions=tuple(extensions_value),
        canonicalization_version=_positive_int(
            candidate["canonicalization_version"],
            "candidate.canonicalization_version",
        ),
    )
    source = await _read_context_maintenance_source(connection, turn_id=scope.turn_id)
    operation_kind = "context_maintenance" if source is not None else None
    if any(variant.operation_kind != operation_kind for variant in plan.variants):
        raise TerminalProtocolMalformedError("terminal candidate operation source changed")
    if plan.candidate_projection_digest != candidate["candidate_projection_digest"]:
        raise TerminalProtocolMalformedError("terminal candidate reconstruction digest mismatch")
    return plan


async def _read_protocol_delete_result(
    connection: aiosqlite.Connection,
    context: TerminalDeleteContext,
) -> ProtocolDeleteResult | None:
    async with connection.execute(
        """
        SELECT * FROM turn_terminal_deletion_ledgers
        WHERE turn_id=? AND generation=?
        """,
        (context.scope.turn_id, context.scope.generation),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    if (
        row["deletion_id"] != context.deletion_id
        or row["candidate_id"] != context.expected_candidate_id
        or row["prior_row_version"] != context.expected_generation_row_version
        or row["new_delivery_fence_epoch"] != context.expected_delivery_fence_epoch + 1
        or row["effect_identity_digest"] != context.effect_identity_digest
        or row["effect_bundle_digest"] != context.effect_bundle_digest
        or row["effect_audit_key_version"] != context.effect_audit_key_version
        or row["effect_key_required_until"] != context.effect_key_required_until
        or row["actor_audit_ref"] != context.actor_audit_ref
        or row["authz_snapshot_digest"] != context.authz_snapshot_digest
        or row["policy_code"] != context.policy_code
        or row["resolution"] != "fenced_deleted"
        or row["receipt_id"] is not None
    ):
        raise TerminalProtocolMalformedError("terminal delete replay identity conflict")
    try:
        old_active = json.loads(_required_text(row["old_session_active_tuple"], "deletion.old_session_active_tuple"))
        old_recovery = json.loads(_required_text(row["old_session_recovery_tuple"], "deletion.old_session_recovery_tuple"))
    except json.JSONDecodeError as exc:
        raise TerminalProtocolMalformedError("terminal delete replay Session projection is malformed") from exc
    expected_digest = canonical_digest_v1(
        {
            "actor_audit_ref": row["actor_audit_ref"],
            "authz_snapshot_digest": row["authz_snapshot_digest"],
            "candidate_id": row["candidate_id"],
            "deletion_id": row["deletion_id"],
            "effect_audit_key_version": row["effect_audit_key_version"],
            "effect_bundle_digest": row["effect_bundle_digest"],
            "effect_identity_digest": row["effect_identity_digest"],
            "effect_key_required_until": row["effect_key_required_until"],
            "generation": row["generation"],
            "new_delivery_fence_epoch": row["new_delivery_fence_epoch"],
            "old_session_active_tuple": old_active,
            "old_session_recovery_tuple": old_recovery,
            "policy_code": row["policy_code"],
            "prior_execution_claim_epoch": row["prior_execution_claim_epoch"],
            "prior_finalization_epoch": row["prior_finalization_epoch"],
            "prior_generation_state": row["prior_generation_state"],
            "prior_row_version": row["prior_row_version"],
            "resolution": row["resolution"],
            "turn_id": row["turn_id"],
            "unsettled_effect_count": row["unsettled_effect_count"],
        }
    )
    if row["deletion_digest"] != expected_digest:
        raise TerminalProtocolMalformedError("terminal delete replay digest mismatch")
    async with connection.execute(
        """
        SELECT g.state,g.recovery_resolution,g.terminal_candidate_id,
               g.terminal_receipt_id,
               t.delivery_fence_epoch,t.current_terminal_receipt_id,
               s.active_turn_id,s.recovery_lock_turn_id,s.steer_freeze_turn_id,
               c.state AS candidate_state,w.team_id,t.session_id,
               EXISTS(
                 SELECT 1 FROM turn_terminal_receipts r
                 WHERE r.turn_id=g.turn_id AND r.generation=g.generation
               ) AS receipt_exists
        FROM turn_generations g
        JOIN turns t ON t.id=g.turn_id
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        LEFT JOIN turn_terminal_candidates c ON c.candidate_id=?
        WHERE g.turn_id=? AND g.generation=?
        """,
        (context.expected_candidate_id, context.scope.turn_id, context.scope.generation),
    ) as cursor:
        sealed = await cursor.fetchone()
    if (
        sealed is None
        or sealed["team_id"] != context.scope.team_id
        or sealed["session_id"] != context.scope.session_id
        or sealed["state"] != GenerationState.CLOSED
        or sealed["recovery_resolution"] != "fenced_deleted"
        or sealed["terminal_candidate_id"] != context.expected_candidate_id
        or sealed["terminal_receipt_id"] is not None
        or sealed["current_terminal_receipt_id"] is not None
        or sealed["receipt_exists"] != 0
        or sealed["delivery_fence_epoch"] < row["new_delivery_fence_epoch"]
        or sealed["active_turn_id"] == context.scope.turn_id
        or sealed["recovery_lock_turn_id"] == context.scope.turn_id
        or sealed["steer_freeze_turn_id"] == context.scope.turn_id
        or (context.expected_candidate_id is not None and sealed["candidate_state"] not in {None, "fenced_deleted"})
    ):
        raise TerminalProtocolMalformedError("terminal delete replay seal changed")
    return ProtocolDeleteResult(
        scope=context.scope,
        deletion_id=context.deletion_id,
        deletion_digest=_required_text(row["deletion_digest"], "deletion.deletion_digest"),
        new_delivery_fence_epoch=_positive_int(
            row["new_delivery_fence_epoch"],
            "deletion.new_delivery_fence_epoch",
        ),
        unsettled_effect_count=_nonnegative_int(
            row["unsettled_effect_count"],
            "deletion.unsettled_effect_count",
        ),
        created=False,
    )


async def _read_effect_retention_result(
    connection: aiosqlite.Connection,
    context: EffectRetentionContext,
) -> EffectRetentionResult | None:
    async with connection.execute(
        """
        SELECT * FROM turn_effect_retention_tombstones
        WHERE team_id=? AND turn_id=? AND generation=? AND effect_bundle_digest=?
        """,
        (
            context.scope.team_id,
            context.scope.turn_id,
            context.scope.generation,
            context.effect_bundle_digest,
        ),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    if (
        row["tombstone_id"] != context.tombstone_id
        or row["audit_key_version"] != context.audit_key_version
        or row["provider_token_digests_digest"] != context.provider_token_digests_digest
        or row["settlement_outcomes_digest"] != context.settlement_outcomes_digest
        or row["state"] != "aggregated"
        or row["eligible_at"] != context.eligible_at
        or row["provider_dedup_horizon_at"] != context.provider_dedup_horizon_at
        or row["expires_at"] != context.expires_at
    ):
        raise TerminalProtocolMalformedError("effect retention replay identity conflict")
    async with connection.execute(
        """
        SELECT member_key_id,team_id,attempt_identity_digest,provider_token_digest,
               audit_key_version,expires_at
        FROM turn_effect_retention_member_keys
        WHERE tombstone_id=? ORDER BY member_key_id
        """,
        (context.tombstone_id,),
    ) as cursor:
        stored_members = list(await cursor.fetchall())
    expected_members = sorted(context.members, key=lambda member: member.member_key_id)
    if len(stored_members) != len(expected_members) or row["attempt_count"] != len(stored_members):
        raise TerminalProtocolMalformedError("effect retention replay members changed")
    for stored, expected in zip(stored_members, expected_members, strict=True):
        if (
            stored["member_key_id"] != expected.member_key_id
            or stored["team_id"] != context.scope.team_id
            or stored["attempt_identity_digest"] != expected.attempt_identity_digest
            or stored["provider_token_digest"] != expected.provider_token_digest
            or stored["audit_key_version"] != context.audit_key_version
            or stored["expires_at"] != context.expires_at
        ):
            raise TerminalProtocolMalformedError("effect retention replay member identity changed")
    return EffectRetentionResult(
        scope=context.scope,
        tombstone_id=context.tombstone_id,
        effect_bundle_digest=context.effect_bundle_digest,
        attempt_count=len(stored_members),
        created=False,
    )


def _finalization_context_matches(
    row: aiosqlite.Row,
    context: FinalizationContext,
    now: datetime,
) -> bool:
    return bool(
        row["terminal_protocol_version"] == 1
        and row["protocol_version"] == 1
        and row["current_generation"] == context.scope.generation
        and row["state"] == GenerationState.CLOSING
        and row["row_version"] == context.generation_row_version
        and row["owner_instance_id"] == context.execution_owner_instance_id
        and row["claim_epoch"] == context.execution_claim_epoch
        and row["finalization_owner_instance_id"] == context.finalization_owner_instance_id
        and row["finalization_epoch"] == context.finalization_epoch
        and row["closing_accepted_through"] == context.closing_accepted_through
        and row["team_id"] == context.scope.team_id
        and row["session_id"] == context.scope.session_id
        and _timestamp_after(row["finalization_lease_expires_at"], now)
    )


async def _begin_finalization_in_tx(
    connection: aiosqlite.Connection,
    *,
    context: ExecutionGenerationContext,
    finalization_owner_instance_id: str,
    closing_reason: str,
    now: datetime,
) -> tuple[FinalizationContext, bool]:
    """Reuse the one RUNNING-to-CLOSING and Session-freeze state machine."""

    row = await _read_generation_claim_row(connection, context.scope)
    if row is None:
        raise TerminalProtocolMalformedError("terminal generation is missing")
    state = _generation_state(row["state"])
    if state is GenerationState.CLOSING:
        replay = _finalization_context_from_replay(
            row,
            context=context,
            finalization_owner_instance_id=finalization_owner_instance_id,
            now=now,
        )
        if replay is None:
            raise TerminalProtocolMalformedError(
                "terminal generation has another finalization owner",
            )
        if row["closing_reason"] != closing_reason:
            raise TerminalProtocolMalformedError(
                "terminal finalization reason changed",
            )
        return replay, False
    if state is GenerationState.CLOSED:
        raise TerminalProtocolMalformedError("terminal generation is already closed")
    if not _execution_context_matches(row, context, now):
        raise TerminalProtocolMalformedError(
            "execution generation ownership fence changed",
        )
    if not await _writer_lease_matches_execution(
        connection,
        context=context,
        now=now,
    ):
        raise TerminalProtocolMalformedError(
            "execution writer lease is unavailable",
        )
    if row["terminal_candidate_id"] is not None or row["terminal_receipt_id"] is not None:
        raise TerminalProtocolMalformedError("terminal generation is already sealed")
    accepted_through = (
        _positive_int(
            row["accepted_proposal_next"],
            "generation.accepted_proposal_next",
        )
        - 1
    )
    finalization_epoch = (
        _nonnegative_int(
            row["finalization_epoch"],
            "generation.finalization_epoch",
        )
        + 1
    )
    lease_expires_at = _iso_utc(
        now + timedelta(seconds=_FINALIZATION_LEASE_SECONDS),
    )
    now_text = _iso_utc(now)
    update = await connection.execute(
        """
        UPDATE turn_generations
        SET state='closing',row_version=row_version+1,closing_reason=?,
            closing_accepted_through=?,finalization_owner_instance_id=?,
            finalization_epoch=?,finalization_lease_expires_at=?,
            finalization_started_at=?
        WHERE turn_id=? AND generation=? AND protocol_version=1 AND state='running'
          AND row_version=? AND owner_instance_id=? AND claim_epoch=?
          AND terminal_candidate_id IS NULL AND terminal_receipt_id IS NULL
          AND closing_accepted_through IS NULL
        """,
        (
            closing_reason,
            accepted_through,
            finalization_owner_instance_id,
            finalization_epoch,
            lease_expires_at,
            now_text,
            context.scope.turn_id,
            context.scope.generation,
            context.generation_row_version,
            context.owner_instance_id,
            context.claim_epoch,
        ),
    )
    if update.rowcount != 1:
        raise TerminalProtocolMalformedError(
            "terminal finalization ownership CAS changed",
        )
    session_update = await connection.execute(
        """
        UPDATE sessions
        SET steer_buffer_state='frozen',steer_freeze_turn_id=?,
            steer_freeze_generation=?,steer_freeze_claim_epoch=?,
            steer_frozen_revision=steer_buffer_revision
        WHERE id=? AND active_turn_id=? AND active_turn_protocol_version=1
          AND active_turn_generation=? AND active_turn_claim_epoch=?
          AND recovery_lock_turn_id IS NULL AND steer_buffer_state='collecting'
        """,
        (
            context.scope.turn_id,
            context.scope.generation,
            context.claim_epoch,
            context.scope.session_id,
            context.scope.turn_id,
            context.scope.generation,
            context.claim_epoch,
        ),
    )
    if session_update.rowcount != 1:
        raise TerminalProtocolMalformedError(
            "terminal finalization Session freeze CAS changed",
        )
    return (
        FinalizationContext(
            scope=context.scope,
            execution_owner_instance_id=context.owner_instance_id,
            execution_claim_epoch=context.claim_epoch,
            finalization_owner_instance_id=finalization_owner_instance_id,
            finalization_epoch=finalization_epoch,
            generation_row_version=context.generation_row_version + 1,
            closing_accepted_through=accepted_through,
            lease_expires_at=lease_expires_at,
        ),
        True,
    )


async def _require_v2_normal_finalization_ready_in_tx(
    connection: aiosqlite.Connection,
    *,
    scope: TerminalScope,
    generation: aiosqlite.Row,
) -> None:
    await _require_local_billing_settled_truth_in_tx(
        connection,
        scope=scope,
        generation=generation,
    )
    snapshot = await _read_effect_disclosure_snapshot_v2(connection, scope)
    if any(attempt.state is not EffectState.SETTLED for attempt in snapshot.attempts):
        raise TerminalProtocolMalformedError(
            "contract-v2 normal finalization has unsettled effects",
        )


async def _freeze_cancelled_effect_set_in_tx(
    connection: aiosqlite.Connection,
    *,
    scope: TerminalScope,
) -> None:
    async with connection.execute(
        """
        SELECT COUNT(*) AS n FROM turn_effect_attempts
        WHERE turn_id=? AND generation=? AND state='intent'
        """,
        (scope.turn_id, scope.generation),
    ) as cursor:
        row = await cursor.fetchone()
    intent_count = int(row["n"]) if row is not None else 0
    if intent_count:
        deleted = await connection.execute(
            """
            DELETE FROM turn_effect_attempts
            WHERE turn_id=? AND generation=? AND state='intent'
            """,
            (scope.turn_id, scope.generation),
        )
        if deleted.rowcount != intent_count:
            raise TerminalProtocolMalformedError(
                "cancelled effect intent set changed",
            )
    await connection.execute(
        """
        UPDATE turn_effect_attempts
        SET state='unknown',row_version=row_version+1
        WHERE turn_id=? AND generation=? AND state='dispatched'
        """,
        (scope.turn_id, scope.generation),
    )


async def _require_v2_cancelled_finalization_ready_in_tx(
    connection: aiosqlite.Connection,
    *,
    scope: TerminalScope,
    generation: aiosqlite.Row,
) -> None:
    if generation["closing_reason"] != _CANCELLED_FINALIZATION_REASON:
        raise TerminalProtocolMalformedError(
            "cancelled finalization marker changed",
        )
    await _require_local_billing_settled_truth_in_tx(
        connection,
        scope=scope,
        generation=generation,
    )
    snapshot = await _read_effect_disclosure_snapshot_v2(connection, scope)
    if any(attempt.state not in {EffectState.UNKNOWN, EffectState.SETTLED} for attempt in snapshot.attempts):
        raise TerminalProtocolMalformedError(
            "cancelled finalization has an unsealed effect",
        )


def _finalization_context_from_replay(
    row: aiosqlite.Row,
    *,
    context: ExecutionGenerationContext,
    finalization_owner_instance_id: str,
    now: datetime,
) -> FinalizationContext | None:
    if (
        row["terminal_protocol_version"] != 1
        or row["protocol_version"] != 1
        or row["current_generation"] != context.scope.generation
        or row["owner_instance_id"] != context.owner_instance_id
        or row["claim_epoch"] != context.claim_epoch
        or row["finalization_owner_instance_id"] != finalization_owner_instance_id
        or row["finalization_epoch"] is None
        or not _timestamp_after(row["finalization_lease_expires_at"], now)
    ):
        return None
    accepted_through = _nonnegative_int(row["closing_accepted_through"], "generation.closing_accepted_through")
    return FinalizationContext(
        scope=context.scope,
        execution_owner_instance_id=context.owner_instance_id,
        execution_claim_epoch=context.claim_epoch,
        finalization_owner_instance_id=finalization_owner_instance_id,
        finalization_epoch=_positive_int(row["finalization_epoch"], "generation.finalization_epoch"),
        generation_row_version=_positive_int(row["row_version"], "generation.row_version"),
        closing_accepted_through=accepted_through,
        lease_expires_at=_required_text(
            row["finalization_lease_expires_at"],
            "generation.finalization_lease_expires_at",
        ),
    )


def _validate_candidate_plan_envelopes(plan: TerminalCandidatePlan) -> None:
    for variant in plan.variants:
        terminal = variant.events[-1]
        if terminal.event_id != plan.reserved_terminal_event_id:
            raise TerminalProtocolMalformedError("terminal candidate done identity mismatch")
        if terminal.phase_scope is not PhaseScope.IN_GENERATION:
            raise TerminalProtocolMalformedError("normal terminal candidate done phase is invalid")
        expected_payload = {
            "candidate_id": plan.candidate_id,
            **({"operation_kind": variant.operation_kind} if variant.operation_kind is not None else {}),
            "generation": plan.scope.generation,
            "message_key": variant.message_key,
            "protocol": 1,
            "reason": variant.reason_code,
            "receipt_id": plan.reserved_receipt_id,
            "required_manifest_template_digest": variant.required_manifest_template_digest,
            "selected_variant_key": variant.variant_key,
            "status": variant.status.value,
        }
        if terminal.payload != expected_payload:
            raise TerminalProtocolMalformedError("terminal candidate done payload is not canonical")


async def _validate_candidate_effect_disclosure(
    connection: aiosqlite.Connection,
    plan: TerminalCandidatePlan,
) -> None:
    snapshot = await _read_effect_disclosure_snapshot(connection, plan.scope)
    persisted_v2_extensions = tuple(
        extension for extension in plan.projection_extensions if str(extension.get("kind", "")).startswith("effect_disclosure_v2")
    )
    if snapshot is not None and snapshot.protocol == 2:
        if persisted_v2_extensions != snapshot.projection_extensions:
            raise TerminalProtocolMalformedError("v2 effect disclosure manifest does not match frozen truth")
        await _validate_v2_candidate_effect_disclosure(
            connection,
            plan=plan,
            snapshot=snapshot,
        )
        return
    if persisted_v2_extensions:
        raise TerminalProtocolMalformedError("legacy effect disclosure carries a v2 manifest")
    for variant in plan.variants:
        disclosures = [event for event in variant.required_events if event.event_type == "effect_disclosure"]
        settlements = [event for event in variant.required_events if event.event_type == "effect_settlement"]
        if plan.origin is CandidateOrigin.RECOVERY and variant.outcome_variant is CandidateOutcome.PRIMARY:
            if (
                snapshot is None
                or snapshot.unsettled_count != 0
                or disclosures
                or len(settlements) != 1
                or settlements[0].actor_scope is not ActorScope.ROOT
                or settlements[0].phase_scope is not PhaseScope.IN_GENERATION
                or settlements[0].payload != snapshot.event_payload
                or not await _is_settled_approval_variant(
                    connection,
                    plan=plan,
                    variant=variant,
                    effect_settlement_payload=snapshot.event_payload,
                )
            ):
                raise TerminalProtocolMalformedError("settled effect recovery does not match frozen truth")
            continue
        if variant.outcome_variant is not CandidateOutcome.RECOVERY_INTERRUPT:
            if disclosures:
                raise TerminalProtocolMalformedError("primary variant carries recovery effect disclosure")
            if not settlements:
                continue
            if (
                snapshot is None
                or snapshot.unsettled_count != 0
                or len(settlements) != 1
                or settlements[0].actor_scope is not ActorScope.ROOT
                or settlements[0].phase_scope is not PhaseScope.IN_GENERATION
                or settlements[0].payload != snapshot.event_payload
                or not await _is_settled_approval_variant(
                    connection,
                    plan=plan,
                    variant=variant,
                    effect_settlement_payload=snapshot.event_payload,
                )
            ):
                raise TerminalProtocolMalformedError("settled primary effect does not match frozen truth")
            continue
        if snapshot is None:
            if disclosures:
                raise TerminalProtocolMalformedError("effect-free recovery variant carries a disclosure")
            continue
        if (
            len(disclosures) != 1
            or disclosures[0].actor_scope is not ActorScope.ROOT
            or disclosures[0].phase_scope is not PhaseScope.IN_GENERATION
            or disclosures[0].payload != snapshot.event_payload
        ):
            raise TerminalProtocolMalformedError("recovery effect disclosure does not match frozen truth")


async def _validate_v2_candidate_effect_disclosure(
    connection: aiosqlite.Connection,
    *,
    plan: TerminalCandidatePlan,
    snapshot: EffectDisclosureSnapshot,
) -> None:
    """Validate generic v2 truth before applying approval-specific checks."""

    generation = await _read_generation_claim_row(connection, plan.scope)
    if generation is None:
        raise TerminalProtocolMalformedError(
            "v2 candidate generation is missing",
        )
    if any(attempt.state not in {EffectState.UNKNOWN, EffectState.SETTLED} for attempt in snapshot.attempts):
        raise TerminalProtocolMalformedError("v2 candidate carries an unsealed effect state")
    cancelled_primary = any(
        variant.outcome_variant is CandidateOutcome.PRIMARY and variant.status is TerminalStatus.CANCELLED for variant in plan.variants
    )
    cancelled_marker = generation["closing_reason"] == _CANCELLED_FINALIZATION_REASON
    if cancelled_marker is not cancelled_primary:
        raise TerminalProtocolMalformedError(
            "v2 cancellation candidate does not match its closing marker",
        )
    if snapshot.unsettled_count and (not cancelled_primary or generation["closing_reason"] != _CANCELLED_FINALIZATION_REASON):
        raise TerminalProtocolMalformedError(
            "non-cancelled v2 candidate carries unsettled effects",
        )
    await _require_local_billing_settled_truth_in_tx(
        connection,
        scope=plan.scope,
        generation=generation,
    )
    has_approval = any(attempt.provider_contract_key == "approval_decision/v1" for attempt in snapshot.attempts)
    for variant in plan.variants:
        disclosures = tuple(event for event in variant.required_events if event.event_type == "effect_disclosure")
        settlements = tuple(event for event in variant.required_events if event.event_type == "effect_settlement")
        if len(disclosures) > 1 or len(settlements) > 1 or (disclosures and settlements):
            raise TerminalProtocolMalformedError("v2 candidate effect control event is not unique")
        expected_type: str | None
        if not snapshot.attempt_count:
            expected_type = None
        elif snapshot.unsettled_count:
            expected_type = "effect_disclosure"
        else:
            expected_type = "effect_settlement"
        actual = (*disclosures, *settlements)
        if expected_type is None:
            if actual:
                raise TerminalProtocolMalformedError("effect-free v2 candidate carries an effect event")
            continue
        if (
            len(actual) != 1
            or actual[0].event_type != expected_type
            or actual[0].actor_scope is not ActorScope.ROOT
            or actual[0].phase_scope is not PhaseScope.IN_GENERATION
            or actual[0].payload != snapshot.event_payload
        ):
            raise TerminalProtocolMalformedError("v2 candidate effect event does not match frozen truth")
        approval_terminal_variant = bool(
            has_approval
            and variant.outcome_variant is CandidateOutcome.PRIMARY
            and variant.status in {TerminalStatus.COMPLETED, TerminalStatus.FAILED}
        )
        if approval_terminal_variant and not await _is_settled_approval_variant(
            connection,
            plan=plan,
            variant=variant,
            effect_settlement_payload=snapshot.event_payload,
        ):
            raise TerminalProtocolMalformedError("v2 approval effect adapter does not match frozen truth")


async def _validate_selected_v2_effect_disclosure(
    connection: aiosqlite.Connection,
    *,
    plan: TerminalCandidatePlan,
    selected: TerminalCandidateVariant,
) -> None:
    snapshot = await _read_effect_disclosure_snapshot(
        connection,
        plan.scope,
    )
    if snapshot is None or snapshot.protocol != 2:
        return
    generation = await _read_generation_claim_row(connection, plan.scope)
    if generation is None:
        raise TerminalProtocolMalformedError(
            "v2 selected candidate generation is missing",
        )
    if (generation["closing_reason"] == _CANCELLED_FINALIZATION_REASON) is not (selected.status is TerminalStatus.CANCELLED):
        raise TerminalProtocolMalformedError(
            "v2 selected cancellation does not match its closing marker",
        )
    frozen = tuple(extension for extension in plan.projection_extensions if str(extension.get("kind", "")).startswith("effect_disclosure_v2"))
    if frozen != snapshot.projection_extensions:
        raise TerminalProtocolMalformedError(
            "v2 effect disclosure changed after candidate prepare",
        )
    if snapshot.unsettled_count and selected.status is not TerminalStatus.CANCELLED:
        raise TerminalProtocolMalformedError(
            "non-cancelled receipt cannot commit unsettled effects",
        )


async def _is_settled_approval_variant(
    connection: aiosqlite.Connection,
    *,
    plan: TerminalCandidatePlan,
    variant: TerminalCandidateVariant,
    effect_settlement_payload: dict[str, object],
) -> bool:
    """Bind normal/recovery approval events to projection and settlement truth."""

    if plan.origin not in {CandidateOrigin.NORMAL, CandidateOrigin.RECOVERY}:
        return False
    async with connection.execute(
        """
        SELECT d.decision_id,d.source_kind,d.source_id,d.action,
               g.execution_intent_json,
               p.arguments_digest,p.execution_ok,p.execution_result,
               a.attempt_id,a.provider,a.action_ref,s.outcome
        FROM turn_generations g
        JOIN turn_control_decisions d ON d.decision_id=g.decision_id
        JOIN approvals p ON p.id=d.source_id
        JOIN turn_effect_attempts a
          ON a.turn_id=g.turn_id AND a.generation=g.generation
        JOIN turn_effect_settlements s ON s.attempt_id=a.attempt_id
        WHERE g.turn_id=? AND g.generation=? AND g.kind='execution'
        """,
        (plan.scope.turn_id, plan.scope.generation),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return False
    execution_contract = resolve_effect_execution_contract(_parse_payload(rows[0]["execution_intent_json"]))
    if execution_contract.disposition is EffectIntentDisposition.RECOVERY_ONLY:
        approval_rows = tuple(row for row in rows if row["provider"] == "approval_decision/v1")
    else:
        approval_rows = tuple(row for row in rows if isinstance(row["provider"], str) and row["provider"].startswith("approval:"))
        if len(rows) != 1:
            return False
    if len(approval_rows) != 1:
        return False
    row = approval_rows[0]
    if (
        row["source_kind"] != DecisionSourceKind.APPROVAL.value
        or row["action"] != DecisionAction.APPROVE.value
        or row["execution_ok"] not in {0, 1}
        or row["action_ref"]
        != stable_approval_effect_action_ref(
            decision_id=_required_text(row["decision_id"], "approval decision"),
            approval_id=_required_text(row["source_id"], "approval source"),
            arguments_digest=_required_text(
                row["arguments_digest"],
                "approval arguments digest",
            ),
        )
    ):
        return False
    ok = bool(row["execution_ok"])
    if row["outcome"] != (EffectSettlementOutcome.CONFIRMED_APPLIED.value if ok else EffectSettlementOutcome.CONFIRMED_NOT_APPLIED.value):
        return False
    result_digest: str | None = None
    if ok:
        try:
            result_value = json.loads(row["execution_result"])
        except (TypeError, json.JSONDecodeError):
            return False
        result_digest = canonical_digest_v1(result_value)
    from bowhead_core.terminal_approval_contract import (
        build_approval_required_events,
    )

    expected = build_approval_required_events(
        decision_id=_required_text(row["decision_id"], "approval decision"),
        approval_id=_required_text(row["source_id"], "approval source"),
        ok=ok,
        result_digest=result_digest,
        effect_settlement_payload=effect_settlement_payload,
    )
    return variant.required_events == expected


async def _validate_precommitted_answers(
    connection: aiosqlite.Connection,
    plan: TerminalCandidatePlan,
) -> None:
    for variant in plan.variants:
        if variant.final_answer_mode is not FinalAnswerMode.PRECOMMITTED:
            continue
        assert variant.final_answer_event_id is not None
        assert variant.final_answer_digest is not None
        async with connection.execute(
            """
            SELECT id,turn_id,generation,event_seq,event_type,actor_scope,phase_scope,
                   payload_json,payload_digest,terminal_receipt_id
            FROM turn_events WHERE id=? AND protocol_version=1
            """,
            (variant.final_answer_event_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if (
            row is None
            or row["turn_id"] != plan.scope.turn_id
            or row["generation"] != plan.scope.generation
            or row["event_type"] != "final_answer"
            or row["actor_scope"] != ActorScope.ROOT
            or row["phase_scope"] != PhaseScope.IN_GENERATION
            or row["payload_digest"] != variant.final_answer_digest
            or row["terminal_receipt_id"] is not None
        ):
            raise TerminalProtocolMalformedError("precommitted final answer identity changed")
        payload = _parse_payload(row["payload_json"])
        if canonical_digest_v1(payload) != row["payload_digest"]:
            raise TerminalProtocolMalformedError("precommitted final answer payload digest mismatch")


async def _insert_terminal_candidate(
    connection: aiosqlite.Connection,
    *,
    context: FinalizationContext,
    plan: TerminalCandidatePlan,
    prepared_at: str,
) -> None:
    source = await _read_context_maintenance_source(connection, turn_id=plan.scope.turn_id)
    operation_kind = "context_maintenance" if source is not None else None
    if any(variant.operation_kind != operation_kind for variant in plan.variants):
        raise TerminalProtocolMalformedError("terminal candidate operation source mismatch")
    primary = plan.primary_variant
    counts = {variant.variant_key: len(variant.events) for variant in plan.variants}
    variant_digests = {variant.variant_key: variant.variant_digest for variant in plan.variants}
    followup_epoch = plan.session_disposition_plan.get("writer_admission_epoch")
    if followup_epoch is not None and (not isinstance(followup_epoch, int) or isinstance(followup_epoch, bool) or followup_epoch < 0):
        raise TerminalProtocolMalformedError("candidate followup admission epoch is invalid")
    await connection.execute(
        """
        INSERT INTO turn_terminal_candidates(
          candidate_id,turn_id,generation,candidate_origin,reserved_receipt_id,
          reserved_terminal_event_id,prepared_execution_claim_epoch,
          finalization_epoch_at_prepare,followup_admission_epoch_at_prepare,
          primary_status,primary_reason_code,primary_message_key,
          closing_accepted_through,persisted_event_highwater,
          terminal_event_counts_by_variant_json,candidate_projection_digest,
          canonicalization_version,state,session_disposition_plan_json,
          session_disposition_plan_digest,projection_variant_digests_json,
          projection_extensions_json,created_at,prepared_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'prepared',?,?,?,?,?,?)
        """,
        (
            plan.candidate_id,
            plan.scope.turn_id,
            plan.scope.generation,
            plan.origin.value,
            plan.reserved_receipt_id,
            plan.reserved_terminal_event_id,
            context.execution_claim_epoch,
            context.finalization_epoch,
            followup_epoch,
            primary.status.value if primary is not None else None,
            primary.reason_code if primary is not None else None,
            primary.message_key if primary is not None else None,
            plan.closing_accepted_through,
            plan.persisted_event_highwater,
            canonical_json_v1(counts),
            plan.candidate_projection_digest,
            canonical_json_v1(dict(plan.session_disposition_plan)),
            plan.session_disposition_plan_digest,
            canonical_json_v1(variant_digests),
            canonical_json_v1([dict(value) for value in plan.projection_extensions]),
            prepared_at,
            prepared_at,
        ),
    )
    for variant in plan.variants:
        await connection.execute(
            """
            INSERT INTO turn_terminal_candidate_variants(
              candidate_id,variant_key,outcome_variant,session_variant,status,
              reason_code,message_key,final_answer_mode,final_answer_event_id,
              final_answer_digest,required_manifest_template_digest,
              session_mutation_template_digest,variant_digest,operation_kind
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                plan.candidate_id,
                variant.variant_key,
                variant.outcome_variant.value,
                variant.session_variant.value,
                variant.status.value,
                variant.reason_code,
                variant.message_key,
                variant.final_answer_mode.value,
                variant.final_answer_event_id,
                variant.final_answer_digest,
                variant.required_manifest_template_digest,
                variant.session_mutation_template_digest,
                variant.variant_digest,
                variant.operation_kind,
            ),
        )
        for ordinal, event in enumerate(variant.events, start=1):
            await connection.execute(
                """
                INSERT INTO turn_terminal_candidate_events(
                  candidate_id,variant_key,ordinal,event_id,event_type,
                  actor_scope,phase_scope,payload_json,payload_digest
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    plan.candidate_id,
                    variant.variant_key,
                    ordinal,
                    event.event_id,
                    event.event_type,
                    event.actor_scope.value,
                    event.phase_scope.value,
                    event.payload_json,
                    event.payload_digest,
                ),
            )


async def _read_prepared_candidate_context(
    connection: aiosqlite.Connection,
    *,
    row: aiosqlite.Row,
    context: FinalizationContext,
    plan: TerminalCandidatePlan,
) -> PreparedFinalizationContext:
    if row["terminal_candidate_id"] != plan.candidate_id:
        raise TerminalProtocolMalformedError("terminal candidate identity conflict")
    async with connection.execute(
        """
        SELECT candidate_projection_digest,state FROM turn_terminal_candidates
        WHERE candidate_id=? AND turn_id=? AND generation=?
        """,
        (plan.candidate_id, plan.scope.turn_id, plan.scope.generation),
    ) as cursor:
        candidate = await cursor.fetchone()
    if (
        candidate is None
        or candidate["candidate_projection_digest"] != plan.candidate_projection_digest
        or candidate["state"] not in {"prepared", "committed"}
    ):
        raise TerminalProtocolMalformedError("terminal candidate digest conflict")
    return PreparedFinalizationContext(
        finalization=FinalizationContext(
            scope=context.scope,
            execution_owner_instance_id=context.execution_owner_instance_id,
            execution_claim_epoch=context.execution_claim_epoch,
            finalization_owner_instance_id=context.finalization_owner_instance_id,
            finalization_epoch=context.finalization_epoch,
            generation_row_version=_positive_int(row["row_version"], "generation.row_version"),
            closing_accepted_through=context.closing_accepted_through,
            lease_expires_at=context.lease_expires_at,
        ),
        candidate_id=plan.candidate_id,
        candidate_projection_digest=plan.candidate_projection_digest,
    )


async def _validate_stored_candidate(
    connection: aiosqlite.Connection,
    *,
    context: PreparedFinalizationContext,
    plan: TerminalCandidatePlan,
) -> None:
    async with connection.execute(
        "SELECT * FROM turn_terminal_candidates WHERE candidate_id=?",
        (plan.candidate_id,),
    ) as cursor:
        candidate = await cursor.fetchone()
    if candidate is None:
        raise TerminalProtocolMalformedError("terminal candidate is missing")
    primary = plan.primary_variant
    followup_epoch = plan.session_disposition_plan.get("writer_admission_epoch")
    expected_candidate = {
        "turn_id": plan.scope.turn_id,
        "generation": plan.scope.generation,
        "candidate_origin": plan.origin.value,
        "reserved_receipt_id": plan.reserved_receipt_id,
        "reserved_terminal_event_id": plan.reserved_terminal_event_id,
        "prepared_execution_claim_epoch": context.finalization.execution_claim_epoch,
        "finalization_epoch_at_prepare": context.finalization.finalization_epoch,
        "followup_admission_epoch_at_prepare": followup_epoch,
        "primary_status": primary.status.value if primary is not None else None,
        "primary_reason_code": primary.reason_code if primary is not None else None,
        "primary_message_key": primary.message_key if primary is not None else None,
        "closing_accepted_through": plan.closing_accepted_through,
        "persisted_event_highwater": plan.persisted_event_highwater,
        "terminal_event_counts_by_variant_json": canonical_json_v1({variant.variant_key: len(variant.events) for variant in plan.variants}),
        "candidate_projection_digest": plan.candidate_projection_digest,
        "canonicalization_version": 1,
        "state": "prepared",
        "session_disposition_plan_json": canonical_json_v1(dict(plan.session_disposition_plan)),
        "session_disposition_plan_digest": plan.session_disposition_plan_digest,
        "projection_variant_digests_json": canonical_json_v1({variant.variant_key: variant.variant_digest for variant in plan.variants}),
        "projection_extensions_json": canonical_json_v1([dict(value) for value in plan.projection_extensions]),
    }
    if any(candidate[name] != value for name, value in expected_candidate.items()):
        raise TerminalProtocolMalformedError("stored terminal candidate projection mismatch")
    async with connection.execute(
        "SELECT * FROM turn_terminal_candidate_variants WHERE candidate_id=? ORDER BY variant_key",
        (plan.candidate_id,),
    ) as cursor:
        variants = list(await cursor.fetchall())
    if len(variants) != len(plan.variants):
        raise TerminalProtocolMalformedError("stored terminal candidate variants are incomplete")
    plan_by_key = {variant.variant_key: variant for variant in plan.variants}
    for stored in variants:
        variant = plan_by_key.get(stored["variant_key"])
        expected_variant = (
            {
                "outcome_variant": variant.outcome_variant.value,
                "session_variant": variant.session_variant.value,
                "status": variant.status.value,
                "reason_code": variant.reason_code,
                "message_key": variant.message_key,
                "final_answer_mode": variant.final_answer_mode.value,
                "final_answer_event_id": variant.final_answer_event_id,
                "final_answer_digest": variant.final_answer_digest,
                "required_manifest_template_digest": variant.required_manifest_template_digest,
                "session_mutation_template_digest": variant.session_mutation_template_digest,
                "variant_digest": variant.variant_digest,
                "operation_kind": variant.operation_kind,
            }
            if variant is not None
            else {}
        )
        if variant is None or any(stored[name] != value for name, value in expected_variant.items()):
            raise TerminalProtocolMalformedError("stored terminal candidate variant mismatch")
        async with connection.execute(
            """
            SELECT * FROM turn_terminal_candidate_events
            WHERE candidate_id=? AND variant_key=? ORDER BY ordinal
            """,
            (plan.candidate_id, variant.variant_key),
        ) as cursor:
            events = list(await cursor.fetchall())
        if len(events) != len(variant.events):
            raise TerminalProtocolMalformedError("stored terminal candidate event set is incomplete")
        for ordinal, (stored_event, expected_event) in enumerate(zip(events, variant.events, strict=True), start=1):
            expected_projection = {
                "ordinal": ordinal,
                "event_id": expected_event.event_id,
                "event_type": expected_event.event_type,
                "actor_scope": expected_event.actor_scope.value,
                "phase_scope": expected_event.phase_scope.value,
                "payload_json": expected_event.payload_json,
                "payload_digest": expected_event.payload_digest,
            }
            if any(stored_event[name] != value for name, value in expected_projection.items()):
                raise TerminalProtocolMalformedError("stored terminal candidate event projection mismatch")


@dataclass(frozen=True, slots=True)
class _SessionMutationPlan:
    disposition: str
    old_active: dict[str, object] | None
    new_active: dict[str, object] | None
    freeze_revision: int
    buffer_identity_digest: str
    consumed_or_retained_count: int
    new_buffer_json: str
    new_buffer_state: str
    next_turn: Mapping[str, object] | None
    projection: dict[str, object]
    mutation_digest: str


def _select_normal_candidate_variant(
    row: aiosqlite.Row,
    plan: TerminalCandidatePlan,
) -> TerminalCandidateVariant:
    """Select the production Session branch from frozen durable facts."""

    if plan.origin is not CandidateOrigin.NORMAL:
        raise TerminalProtocolMalformedError("automatic branch selection requires a normal candidate")
    buffer = _parse_steer_buffer(row["steer_buffer_json"])
    if buffer:
        if (
            row["steer_buffer_state"] != "frozen"
            or row["steer_freeze_turn_id"] != plan.scope.turn_id
            or row["steer_freeze_generation"] != plan.scope.generation
            or row["steer_frozen_revision"] != row["steer_buffer_revision"]
        ):
            raise TerminalProtocolMalformedError("candidate selector lacks a frozen steer buffer")
        primary = plan.primary_variant
        if primary is None:
            raise TerminalProtocolMalformedError("normal candidate lacks a primary status")
        if primary.status is TerminalStatus.CANCELLED:
            expected_session = SessionVariant.DISCARD
        elif any(
            variant.outcome_variant is CandidateOutcome.PRIMARY and variant.session_variant is SessionVariant.FOLLOWUP for variant in plan.variants
        ):
            expected_session = SessionVariant.FOLLOWUP
        else:
            expected_session = SessionVariant.DEFERRED
    else:
        expected_session = SessionVariant.NONE
    selected = next(
        (variant for variant in plan.variants if variant.outcome_variant is CandidateOutcome.PRIMARY and variant.session_variant is expected_session),
        None,
    )
    if selected is None:
        raise TerminalProtocolMalformedError("candidate lacks the required durable Session branch")
    return selected


async def _read_terminal_commit_row(
    connection: aiosqlite.Connection,
    scope: TerminalScope,
) -> aiosqlite.Row | None:
    async with connection.execute(
        """
        SELECT g.*,t.session_id,t.current_generation,t.terminal_protocol_version,
               t.terminal_ingress_surface,t.terminal_admission_cohort,
               t.presentation_locale,t.message_catalog_version,t.event_sequence_next,
               t.current_terminal_receipt_id,
               t.delivery_fence_epoch,t.status AS turn_status,t.intent AS turn_intent,
               t.final_answer AS turn_final_answer,t.error AS turn_error,
               t.error_kind AS turn_error_kind,
               s.active_turn_id,s.active_turn_protocol_version,s.active_turn_generation,
               s.active_turn_claim_epoch,s.recovery_lock_turn_id,s.recovery_lock_generation,
               s.recovery_lock_reason,s.steer_buffer_revision,s.steer_buffer_json,
               s.steer_buffer_state,s.steer_freeze_turn_id,s.steer_freeze_generation,
               s.steer_freeze_claim_epoch,s.steer_frozen_revision,s.last_active_at,w.team_id
        FROM turn_generations g
        JOIN turns t ON t.id=g.turn_id
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        WHERE g.turn_id=? AND g.generation=? AND t.session_id=? AND w.team_id=?
        """,
        (scope.turn_id, scope.generation, scope.session_id, scope.team_id),
    ) as cursor:
        return await cursor.fetchone()


def _active_claim_projection(row: aiosqlite.Row) -> dict[str, object] | None:
    turn_id = row["active_turn_id"]
    if turn_id is None:
        if any(row[name] is not None for name in ("active_turn_protocol_version", "active_turn_generation", "active_turn_claim_epoch")):
            raise TerminalProtocolMalformedError("Session active tuple is malformed")
        return None
    turn_id = _required_text(turn_id, "Session active Turn")
    protocol_version, generation, claim_epoch = _active_protocol_tuple(row)
    return {
        "turn_id": turn_id,
        "protocol_version": protocol_version,
        "generation": generation,
        "claim_epoch": claim_epoch,
    }


def _prepare_session_mutation(
    row: aiosqlite.Row,
    *,
    context: PreparedFinalizationContext | PreparedRecoveryContext,
    candidate_origin: CandidateOrigin,
    selected: TerminalCandidateVariant,
    authz_snapshot_digest: str,
    writer_admission_epoch: int,
) -> _SessionMutationPlan:
    if isinstance(context, PreparedFinalizationContext):
        scope = context.finalization.scope
        execution_claim_epoch = context.finalization.execution_claim_epoch
        recovery_id: str | None = None
    else:
        scope = context.processing.scope
        execution_claim_epoch = _nonnegative_int(row["claim_epoch"], "generation.claim_epoch")
        recovery_id = context.processing.recovery_id
    old_active = _active_claim_projection(row)
    buffer = _parse_steer_buffer(row["steer_buffer_json"])
    buffer_json = canonical_json_v1(buffer)
    if buffer_json != row["steer_buffer_json"]:
        raise TerminalProtocolMalformedError("Session steer buffer is not canonical")
    buffer_identity_digest = canonical_digest_v1(
        [
            {
                "request_digest": item.get("request_digest"),
                "revision": item.get("revision"),
                "steer_id": item.get("steer_id"),
            }
            for item in buffer
        ]
    )
    if candidate_origin is CandidateOrigin.CONTROL:
        if selected.session_variant is not SessionVariant.NONE:
            raise TerminalProtocolMalformedError("control generation must preserve its Session")
        if old_active is not None and old_active["turn_id"] == scope.turn_id:
            raise TerminalProtocolMalformedError("control cannot preserve an active tuple for its own Turn")
        freeze_revision = _nonnegative_int(
            row["steer_buffer_revision"],
            "sessions.steer_buffer_revision",
        )
        recovery_tuple = {
            "generation": row["recovery_lock_generation"],
            "reason": row["recovery_lock_reason"],
            "turn_id": row["recovery_lock_turn_id"],
        }
        freeze_tuple = {
            "claim_epoch": row["steer_freeze_claim_epoch"],
            "frozen_revision": row["steer_frozen_revision"],
            "generation": row["steer_freeze_generation"],
            "turn_id": row["steer_freeze_turn_id"],
        }
        projection = {
            "authz_snapshot_digest_at_commit": authz_snapshot_digest,
            "buffer_identity_digest": buffer_identity_digest,
            "candidate_id": context.candidate_id,
            "consumed_or_retained_count": len(buffer),
            "disposition": "control_preserve_unrelated",
            "freeze_revision": freeze_revision,
            "new_active_claim_tuple": old_active,
            "new_buffer_state": row["steer_buffer_state"],
            "new_recovery_tuple": recovery_tuple,
            "new_steer_freeze_tuple": freeze_tuple,
            "next_turn_identity_digest": None,
            "old_active_claim_tuple": old_active,
            "old_recovery_tuple": recovery_tuple,
            "old_steer_freeze_tuple": freeze_tuple,
            "selected_session_mutation_template_digest": selected.session_mutation_template_digest,
            "selected_variant_key": selected.variant_key,
            "writer_admission_epoch_at_commit": writer_admission_epoch,
        }
        return _SessionMutationPlan(
            disposition="control_preserve_unrelated",
            old_active=old_active,
            new_active=old_active,
            freeze_revision=freeze_revision,
            buffer_identity_digest=buffer_identity_digest,
            consumed_or_retained_count=len(buffer),
            new_buffer_json=buffer_json,
            new_buffer_state=_required_text(
                row["steer_buffer_state"],
                "sessions.steer_buffer_state",
            ),
            next_turn=None,
            projection=projection,
            mutation_digest=canonical_digest_v1(projection),
        )
    expected_active = {
        "turn_id": scope.turn_id,
        "protocol_version": 1,
        "generation": scope.generation,
        "claim_epoch": execution_claim_epoch,
    }
    if old_active != expected_active:
        raise TerminalProtocolMalformedError("terminal Session active claim changed")
    if recovery_id is None:
        if row["recovery_lock_turn_id"] is not None:
            raise TerminalProtocolMalformedError("normal terminal commit cannot hold a recovery lock")
    elif row["recovery_lock_turn_id"] != scope.turn_id or row["recovery_lock_generation"] != scope.generation or row["recovery_lock_reason"] is None:
        raise TerminalProtocolMalformedError("recovery terminal commit lacks its Session lock")
    disposition = selected.session_variant.value
    next_turn: Mapping[str, object] | None = None
    if selected.session_variant is SessionVariant.NONE:
        if buffer or row["steer_buffer_state"] not in {"collecting", "frozen"}:
            raise TerminalProtocolMalformedError("none Session variant requires an empty collecting buffer")
        if row["steer_buffer_state"] == "frozen" and (
            row["steer_freeze_turn_id"] != scope.turn_id
            or row["steer_freeze_generation"] != scope.generation
            or row["steer_freeze_claim_epoch"] != execution_claim_epoch
        ):
            raise TerminalProtocolMalformedError("terminal Session empty freeze fence changed")
        new_buffer_json = buffer_json
        new_buffer_state = "collecting"
        freeze_revision = _nonnegative_int(row["steer_buffer_revision"], "sessions.steer_buffer_revision")
        count = 0
        new_active = None
    else:
        if (
            row["steer_buffer_state"] != "frozen"
            or row["steer_freeze_turn_id"] != scope.turn_id
            or row["steer_freeze_generation"] != scope.generation
            or row["steer_freeze_claim_epoch"] != execution_claim_epoch
            or row["steer_frozen_revision"] != row["steer_buffer_revision"]
        ):
            raise TerminalProtocolMalformedError("terminal Session freeze fence changed")
        freeze_revision = _nonnegative_int(row["steer_frozen_revision"], "sessions.steer_frozen_revision")
        count = len(buffer)
        if selected.session_variant is SessionVariant.DEFERRED:
            new_buffer_json = buffer_json
            new_buffer_state = "deferred"
            new_active = None
        elif selected.session_variant is SessionVariant.DISCARD:
            new_buffer_json = "[]"
            new_buffer_state = "collecting"
            new_active = None
        else:
            raw_next_turn = selected.session_mutation_template.get("next_turn")
            if not isinstance(raw_next_turn, Mapping):
                raise TerminalProtocolMalformedError("followup Session variant lacks frozen next Turn")
            next_turn = dict(raw_next_turn)
            frozen_writer_epoch = selected.session_mutation_template.get(
                "writer_admission_epoch",
                None,
            )
            if frozen_writer_epoch is not None and frozen_writer_epoch != writer_admission_epoch:
                raise TerminalProtocolMalformedError("followup writer admission epoch changed")
            next_turn_id = _required_text(next_turn.get("turn_id"), "session_mutation.next_turn.turn_id")
            new_active = {
                "turn_id": next_turn_id,
                "protocol_version": 1,
                "generation": 1,
                "claim_epoch": 0,
            }
            new_buffer_json = "[]"
            new_buffer_state = "collecting"
    projection = {
        "authz_snapshot_digest_at_commit": authz_snapshot_digest,
        "buffer_identity_digest": buffer_identity_digest,
        "candidate_id": context.candidate_id,
        "consumed_or_retained_count": count,
        "disposition": disposition,
        "freeze_revision": freeze_revision,
        "new_active_claim_tuple": new_active,
        "new_buffer_state": new_buffer_state,
        "next_turn_identity_digest": canonical_digest_v1(dict(next_turn)) if next_turn is not None else None,
        "old_active_claim_tuple": old_active,
        "selected_session_mutation_template_digest": selected.session_mutation_template_digest,
        "selected_variant_key": selected.variant_key,
        "writer_admission_epoch_at_commit": writer_admission_epoch,
    }
    return _SessionMutationPlan(
        disposition=disposition,
        old_active=old_active,
        new_active=new_active,
        freeze_revision=freeze_revision,
        buffer_identity_digest=buffer_identity_digest,
        consumed_or_retained_count=count,
        new_buffer_json=new_buffer_json,
        new_buffer_state=new_buffer_state,
        next_turn=next_turn,
        projection=projection,
        mutation_digest=canonical_digest_v1(projection),
    )


async def _insert_followup_turn(
    connection: aiosqlite.Connection,
    *,
    session_id: str,
    source_row: aiosqlite.Row,
    next_turn: Mapping[str, object],
    committed_at: str,
) -> None:
    turn_id = _required_text(next_turn.get("turn_id"), "session_mutation.next_turn.turn_id")
    request_id = _required_text(next_turn.get("request_id"), "session_mutation.next_turn.request_id")
    user_text = next_turn.get("user_text")
    if not isinstance(user_text, str):
        raise TerminalProtocolMalformedError("session_mutation.next_turn.user_text is malformed")
    execution_intent = next_turn.get("execution_intent")
    if not isinstance(execution_intent, Mapping):
        raise TerminalProtocolMalformedError("followup execution intent is invalid")
    if execution_intent.get("datarc_selection") is not None:
        from bowhead_core.datarc_selection import DatarcSelectionSnapshot, require_datarc_context_migration

        DatarcSelectionSnapshot.model_validate(execution_intent["datarc_selection"])
        await require_datarc_context_migration(connection)
    intent_copy = _canonical_mapping_copy(execution_intent)
    canonical_intent = {
        "execution_intent": intent_copy,
        "request_id": request_id,
        "session_id": session_id,
        "team_id": _required_text(source_row["team_id"], "session_mutation.team_id"),
        "turn_id": turn_id,
        "user_text": user_text,
    }
    intent_digest = canonical_digest_v1(canonical_intent)
    expected_digest = next_turn.get("execution_intent_digest")
    if expected_digest is not None and expected_digest != intent_digest:
        raise TerminalProtocolMalformedError("followup execution intent digest mismatch")
    async with connection.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?",
        (session_id,),
    ) as cursor:
        seq_row = await cursor.fetchone()
    turn_seq = int(seq_row[0]) if seq_row is not None else 1
    await connection.execute(
        """
        INSERT INTO turns(
          id,session_id,seq,user_text,status,started_at,
          terminal_protocol_version,terminal_ingress_surface,terminal_admission_cohort,
          protocol_admitted_at,current_generation,generation_transition_epoch,
          event_sequence_next,delivery_fence_epoch,presentation_locale,message_catalog_version
        ) VALUES (?,?,?,?, 'running',?,1,?,?,?,?,1,1,0,?,?)
        """,
        (
            turn_id,
            session_id,
            turn_seq,
            user_text,
            committed_at,
            source_row["terminal_ingress_surface"],
            source_row["terminal_admission_cohort"],
            committed_at,
            1,
            source_row["presentation_locale"],
            source_row["message_catalog_version"],
        ),
    )
    await connection.execute(
        """
        INSERT INTO turn_generations(
          turn_id,generation,protocol_version,kind,execution_intent_id,
          execution_intent_digest,admission_request_digest,execution_intent_json,
          execution_authz_snapshot_digest,opened_at,state
        ) VALUES (?,1,1,'execution',?,?,?,?,?,?,'opened')
        """,
        (
            turn_id,
            request_id,
            intent_digest,
            intent_digest,
            canonical_json_v1(intent_copy),
            _required_text(
                source_row["execution_authz_snapshot_digest"],
                "session_mutation.execution_authz_snapshot_digest",
            ),
            committed_at,
        ),
    )


async def _apply_session_mutation(
    connection: aiosqlite.Connection,
    *,
    scope: TerminalScope,
    source_row: aiosqlite.Row,
    mutation: _SessionMutationPlan,
    committed_at: str,
    update_last_active: bool,
    recovery_locked: bool,
) -> None:
    if mutation.disposition == "control_preserve_unrelated":
        return
    if mutation.next_turn is not None:
        await _insert_followup_turn(
            connection,
            session_id=scope.session_id,
            source_row=source_row,
            next_turn=mutation.next_turn,
            committed_at=committed_at,
        )
    new_active = mutation.new_active
    update = await connection.execute(
        """
        UPDATE sessions
        SET active_turn_id=?,active_turn_protocol_version=?,active_turn_generation=?,
            active_turn_claim_epoch=?,steer_buffer_json=?,steer_buffer_state=?,
            steer_freeze_turn_id=NULL,steer_freeze_generation=NULL,
            steer_freeze_claim_epoch=NULL,steer_frozen_revision=NULL,
            recovery_lock_turn_id=NULL,recovery_lock_generation=NULL,recovery_lock_reason=NULL,
            last_active_at=CASE WHEN ? THEN ? ELSE last_active_at END
        WHERE id=? AND active_turn_id=? AND active_turn_protocol_version=1
          AND active_turn_generation=? AND active_turn_claim_epoch=?
          AND recovery_lock_turn_id IS ? AND recovery_lock_generation IS ?
        """,
        (
            new_active["turn_id"] if new_active is not None else None,
            new_active["protocol_version"] if new_active is not None else None,
            new_active["generation"] if new_active is not None else None,
            new_active["claim_epoch"] if new_active is not None else None,
            mutation.new_buffer_json,
            mutation.new_buffer_state,
            1 if update_last_active else 0,
            committed_at,
            scope.session_id,
            scope.turn_id,
            scope.generation,
            mutation.old_active["claim_epoch"] if mutation.old_active is not None else None,
            scope.turn_id if recovery_locked else None,
            scope.generation if recovery_locked else None,
        ),
    )
    if update.rowcount != 1:
        raise TerminalProtocolMalformedError("terminal Session mutation CAS changed")


def _event_row_digest(event: Mapping[str, object]) -> str:
    return canonical_digest_v1(
        {
            "actor_scope": event["actor_scope"],
            "event_id": event["event_id"],
            "event_seq": event["event_seq"],
            "event_type": event["event_type"],
            "payload_digest": event["payload_digest"],
            "phase_scope": event["phase_scope"],
        }
    )


async def _precommitted_manifest_item(
    connection: aiosqlite.Connection,
    *,
    plan: TerminalCandidatePlan,
    selected: TerminalCandidateVariant,
) -> tuple[dict[str, object], str | None]:
    if selected.final_answer_mode is not FinalAnswerMode.PRECOMMITTED:
        return {}, None
    assert selected.final_answer_event_id is not None
    assert selected.final_answer_digest is not None
    async with connection.execute(
        """
        SELECT id,event_seq,event_type,actor_scope,phase_scope,payload_json,payload_digest,
               generation,terminal_receipt_id
        FROM turn_events WHERE turn_id=? AND id=? AND protocol_version=1
        """,
        (plan.scope.turn_id, selected.final_answer_event_id),
    ) as cursor:
        row = await cursor.fetchone()
    if (
        row is None
        or row["generation"] != plan.scope.generation
        or row["event_type"] != "final_answer"
        or row["payload_digest"] != selected.final_answer_digest
        or row["terminal_receipt_id"] is not None
    ):
        raise TerminalProtocolMalformedError("precommitted final answer identity changed")
    payload = _parse_payload(row["payload_json"])
    if canonical_digest_v1(payload) != row["payload_digest"]:
        raise TerminalProtocolMalformedError("precommitted final answer payload digest mismatch")
    projection = {
        "event_id": row["id"],
        "event_seq": _positive_int(row["event_seq"], "turn_events.event_seq"),
        "event_type": row["event_type"],
        "actor_scope": row["actor_scope"],
        "phase_scope": row["phase_scope"],
        "payload_digest": row["payload_digest"],
    }
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise TerminalProtocolMalformedError("precommitted final answer text is missing")
    return {
        **projection,
        "canonical_row_digest": _event_row_digest(projection),
    }, text


def _terminal_answer_text(selected: TerminalCandidateVariant) -> str | None:
    if selected.final_answer_mode is not FinalAnswerMode.IN_TERMINAL_BATCH:
        return None
    assert selected.final_answer_event_id is not None
    answer = next((event for event in selected.events[:-1] if event.event_id == selected.final_answer_event_id), None)
    if answer is None:
        raise TerminalProtocolMalformedError("terminal batch final answer is missing")
    text = answer.payload.get("text")
    if not isinstance(text, str) or not text:
        raise TerminalProtocolMalformedError("terminal batch final answer text is missing")
    return text


async def _update_turn_terminal_projection(
    connection: aiosqlite.Connection,
    *,
    row: aiosqlite.Row,
    selected: TerminalCandidateVariant,
    plan: TerminalCandidatePlan,
    final_answer: str | None,
    committed_at: str,
) -> None:
    projection = plan.session_disposition_plan.get("turn_projection", {})
    if not isinstance(projection, Mapping):
        raise TerminalProtocolMalformedError("terminal Turn projection is invalid")
    intent = projection.get("intent")
    if intent is not None and not isinstance(intent, str):
        raise TerminalProtocolMalformedError("terminal Turn intent is invalid")
    error = projection.get("error")
    if error is not None and not isinstance(error, str):
        raise TerminalProtocolMalformedError("terminal Turn error is invalid")
    error_kind = projection.get("error_kind", selected.reason_code)
    if error_kind is not None and not isinstance(error_kind, str):
        raise TerminalProtocolMalformedError("terminal Turn error kind is invalid")
    status = selected.status
    tokens_input = _nonnegative_int(row["input_tokens"], "generation.input_tokens")
    tokens_output = _nonnegative_int(row["output_tokens"], "generation.output_tokens")
    cost_amount = float(row["cost_amount"])
    if cost_amount < 0:
        raise TerminalProtocolMalformedError("generation cost is invalid")
    if status is TerminalStatus.COMPLETED:
        source = await _read_context_maintenance_source(connection, turn_id=plan.scope.turn_id)
        operation_kind = "context_maintenance" if source is not None else None
        if selected.operation_kind != operation_kind:
            raise TerminalProtocolMalformedError("terminal operation source changed")
        if final_answer is None and operation_kind is None:
            raise TerminalProtocolMalformedError("completed terminal projection is missing final answer")
        if operation_kind is not None and final_answer is not None:
            raise TerminalProtocolMalformedError("maintenance terminal projection cannot contain an answer")
        update = await connection.execute(
            """
            UPDATE turns SET status='completed',ended_at=?,intent=?,final_answer=?,
                tokens_input=?,tokens_output=?,cost_usd=?,error=?,error_kind=?
            WHERE id=? AND terminal_protocol_version=1
            """,
            (
                committed_at,
                intent,
                final_answer,
                tokens_input,
                tokens_output,
                cost_amount,
                error,
                error_kind,
                plan.scope.turn_id,
            ),
        )
    elif status is TerminalStatus.FAILED:
        update = await connection.execute(
            """
            UPDATE turns SET status='failed',ended_at=?,intent=COALESCE(?,intent),
                final_answer=COALESCE(?,final_answer),tokens_input=?,tokens_output=?,
                cost_usd=?,error=?,error_kind=?
            WHERE id=? AND terminal_protocol_version=1
            """,
            (
                committed_at,
                intent,
                final_answer,
                tokens_input,
                tokens_output,
                cost_amount,
                error or selected.message_key,
                error_kind,
                plan.scope.turn_id,
            ),
        )
    elif status is TerminalStatus.CANCELLED:
        update = await connection.execute(
            """
            UPDATE turns SET status='cancelled',ended_at=?,intent=COALESCE(?,intent),
                tokens_input=?,tokens_output=?,error_kind=COALESCE(?,error_kind)
            WHERE id=? AND terminal_protocol_version=1
            """,
            (committed_at, intent, tokens_input, tokens_output, error_kind, plan.scope.turn_id),
        )
    elif status is TerminalStatus.ABORTED:
        update = await connection.execute(
            """
            UPDATE turns SET status='aborted',ended_at=?,intent=COALESCE(?,intent),
                final_answer=COALESCE(?,final_answer),tokens_input=?,tokens_output=?,
                cost_usd=?,error_kind=COALESCE(?,error_kind)
            WHERE id=? AND terminal_protocol_version=1
            """,
            (
                committed_at,
                intent,
                final_answer,
                tokens_input,
                tokens_output,
                cost_amount,
                error_kind,
                plan.scope.turn_id,
            ),
        )
    elif status is TerminalStatus.CLARIFICATION_PENDING:
        update = await connection.execute(
            """
            UPDATE turns SET status='clarification_pending',ended_at=?,intent=?,
                final_answer=COALESCE(?,final_answer),tokens_input=?,tokens_output=?,
                cost_usd=?,error_kind=COALESCE(?,error_kind)
            WHERE id=? AND terminal_protocol_version=1
            """,
            (
                committed_at,
                intent,
                final_answer,
                tokens_input,
                tokens_output,
                cost_amount,
                error_kind,
                plan.scope.turn_id,
            ),
        )
    else:
        update = await connection.execute(
            """
            UPDATE turns SET status='awaiting_approval',ended_at=?,intent=?,
                tokens_input=?,tokens_output=?,cost_usd=?,error_kind=COALESCE(?,error_kind)
            WHERE id=? AND terminal_protocol_version=1
            """,
            (
                committed_at,
                intent,
                tokens_input,
                tokens_output,
                cost_amount,
                error_kind,
                plan.scope.turn_id,
            ),
        )
    if update.rowcount != 1:
        raise TerminalProtocolMalformedError("terminal Turn projection CAS changed")


async def _commit_terminal_candidate(
    connection: aiosqlite.Connection,
    *,
    context: PreparedFinalizationContext | PreparedRecoveryContext,
    plan: TerminalCandidatePlan,
    selected: TerminalCandidateVariant,
    authz_snapshot_digest: str,
    writer_admission_epoch: int,
    committed_at: str,
    fault_injector: Callable[[TerminalProtocolFaultPoint], None],
    context_publication: StagedTerminalContext | None = None,
) -> CommittedReceipt:
    if selected.session_variant is SessionVariant.FOLLOWUP and (
        plan.session_disposition_plan.get("writer_admission_epoch") != writer_admission_epoch
    ):
        raise TerminalProtocolMalformedError("followup writer admission epoch changed")
    row = await _read_terminal_commit_row(connection, plan.scope)
    if row is None:
        raise TerminalProtocolMalformedError("terminal commit generation is missing")
    if row["terminal_candidate_id"] != plan.candidate_id or row["terminal_receipt_id"] is not None:
        raise TerminalProtocolMalformedError("terminal commit generation seal changed")
    mutation = _prepare_session_mutation(
        row,
        context=context,
        candidate_origin=plan.origin,
        selected=selected,
        authz_snapshot_digest=authz_snapshot_digest,
        writer_admission_epoch=writer_admission_epoch,
    )
    precommitted_manifest, precommitted_text = await _precommitted_manifest_item(
        connection,
        plan=plan,
        selected=selected,
    )
    final_answer = precommitted_text or _terminal_answer_text(selected)
    context_extensions = [item for item in plan.projection_extensions if item.get("kind") == "model_context_publication_v1"]
    if context_extensions:
        from bowhead_core.context_publication import StagedTerminalContext, TerminalContextProjection

        if len(context_extensions) != 1:
            raise TerminalProtocolMalformedError("context publication projection is duplicated")
        TerminalContextProjection.model_validate(dict(context_extensions[0]))
        if selected.outcome_variant is CandidateOutcome.PRIMARY and selected.status is TerminalStatus.COMPLETED:
            if not isinstance(context_publication, StagedTerminalContext):
                raise TerminalProtocolMalformedError("context publication validation is unavailable")
            await context_publication.adopt(connection, context=context, plan=plan, selected=selected, final_answer=final_answer)
    elif context_publication is not None:
        raise TerminalProtocolMalformedError("context publication was not frozen in the candidate")

    event_sequence_next = _positive_int(row["event_sequence_next"], "turns.event_sequence_next")
    if event_sequence_next != plan.persisted_event_highwater + 1:
        raise TerminalProtocolMalformedError("terminal event allocator moved after candidate prepare")
    fault_injector(TerminalProtocolFaultPoint.TERMINAL_BEFORE_SEQ_ALLOC)
    async with connection.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM turn_events WHERE turn_id=?",
        (plan.scope.turn_id,),
    ) as cursor:
        legacy_row = await cursor.fetchone()
    legacy_next = int(legacy_row[0]) if legacy_row is not None else 1
    staged_rows: list[dict[str, object]] = []
    for offset, event in enumerate(selected.events):
        staged_rows.append(
            {
                "actor_scope": event.actor_scope.value,
                "event_id": event.event_id,
                "event_seq": event_sequence_next + offset,
                "event_type": event.event_type,
                "legacy_seq": legacy_next + offset,
                "payload_digest": event.payload_digest,
                "payload_json": event.payload_json,
                "phase_scope": event.phase_scope.value,
            }
        )
    fault_injector(TerminalProtocolFaultPoint.TERMINAL_AFTER_SEQ_ALLOC)
    required_manifest: list[dict[str, object]] = []
    if precommitted_manifest:
        required_manifest.append(precommitted_manifest)
    for event in staged_rows[:-1]:
        manifest_projection = {
            "event_id": event["event_id"],
            "event_seq": event["event_seq"],
            "event_type": event["event_type"],
            "actor_scope": event["actor_scope"],
            "phase_scope": event["phase_scope"],
            "payload_digest": event["payload_digest"],
        }
        required_manifest.append(
            {
                **manifest_projection,
                "canonical_row_digest": _event_row_digest(manifest_projection),
            }
        )
    required_manifest.sort(key=lambda item: int(item["event_seq"]))
    terminal_row = staged_rows[-1]
    selected_manifest_projection = {
        "required_event_manifest": required_manifest,
        "terminal_event": {
            "event_id": terminal_row["event_id"],
            "event_seq": terminal_row["event_seq"],
            "event_type": terminal_row["event_type"],
            "payload_digest": terminal_row["payload_digest"],
        },
    }
    selected_manifest_digest = canonical_digest_v1(selected_manifest_projection)
    receipt_projection = {
        **({"operation_kind": selected.operation_kind} if selected.operation_kind is not None else {}),
        "candidate_id": plan.candidate_id,
        "candidate_projection_digest": plan.candidate_projection_digest,
        "message_key": selected.message_key,
        "reason_code": selected.reason_code,
        "selected_manifest_digest": selected_manifest_digest,
        "selected_outcome_variant": selected.outcome_variant.value,
        "selected_session_mutation_digest": mutation.mutation_digest,
        "selected_session_variant": selected.session_variant.value,
        "selected_variant_key": selected.variant_key,
        "status": selected.status.value,
        "terminal_event_id": plan.reserved_terminal_event_id,
    }
    receipt_digest = canonical_digest_v1(receipt_projection)
    first_event_seq = int(staged_rows[0]["event_seq"])
    last_event_seq = int(terminal_row["event_seq"])
    await connection.execute(
        """
        INSERT INTO turn_terminal_receipts(
          receipt_id,turn_id,generation,candidate_id,protocol_version,status,
          reason_code,message_key,final_answer_mode,final_answer_event_id,
          selected_variant_key,selected_outcome_variant,selected_session_variant,
          terminal_event_id,first_event_seq,last_event_seq,persisted_event_highwater,
          required_event_manifest_json,candidate_projection_digest,
          selected_projection_digest,selected_manifest_digest,
          selected_session_mutation_digest,projection_extensions_json,committed_at,operation_kind
        ) VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            plan.reserved_receipt_id,
            plan.scope.turn_id,
            plan.scope.generation,
            plan.candidate_id,
            selected.status.value,
            selected.reason_code,
            selected.message_key,
            selected.final_answer_mode.value,
            selected.final_answer_event_id,
            selected.variant_key,
            selected.outcome_variant.value,
            selected.session_variant.value,
            plan.reserved_terminal_event_id,
            first_event_seq,
            last_event_seq,
            plan.persisted_event_highwater,
            canonical_json_v1(required_manifest),
            plan.candidate_projection_digest,
            receipt_digest,
            selected_manifest_digest,
            mutation.mutation_digest,
            canonical_json_v1([dict(value) for value in plan.projection_extensions]),
            committed_at,
            selected.operation_kind,
        ),
    )
    for event in staged_rows:
        await connection.execute(
            """
            INSERT INTO turn_events(
              id,turn_id,seq,event_type,payload_json,occurred_at,protocol_version,
              generation,event_seq,actor_scope,phase_scope,payload_digest,
              terminal_receipt_id,source_proposal_id,accept_ordinal
            ) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,NULL,NULL)
            """,
            (
                event["event_id"],
                plan.scope.turn_id,
                event["legacy_seq"],
                event["event_type"],
                event["payload_json"],
                committed_at,
                plan.scope.generation,
                event["event_seq"],
                event["actor_scope"],
                event["phase_scope"],
                event["payload_digest"],
                plan.reserved_receipt_id,
            ),
        )
    event_allocator_update = await connection.execute(
        """
        UPDATE turns SET event_sequence_next=event_sequence_next+?
        WHERE id=? AND terminal_protocol_version=1 AND event_sequence_next=?
        """,
        (len(staged_rows), plan.scope.turn_id, event_sequence_next),
    )
    if event_allocator_update.rowcount != 1:
        raise TerminalProtocolMalformedError("terminal event allocator CAS changed")
    await connection.execute(
        """
        INSERT INTO turn_terminal_session_mutations(
          receipt_id,candidate_id,selected_variant_key,old_active_claim_tuple,
          disposition,new_active_claim_tuple,freeze_revision,buffer_identity_digest,
          consumed_or_retained_count,authz_snapshot_digest_at_commit,
          writer_admission_epoch_at_commit,mutation_projection_json,mutation_digest,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            plan.reserved_receipt_id,
            plan.candidate_id,
            selected.variant_key,
            canonical_json_v1(mutation.old_active),
            mutation.disposition,
            canonical_json_v1(mutation.new_active),
            mutation.freeze_revision,
            mutation.buffer_identity_digest,
            mutation.consumed_or_retained_count,
            authz_snapshot_digest,
            writer_admission_epoch,
            canonical_json_v1(mutation.projection),
            mutation.mutation_digest,
            committed_at,
        ),
    )
    await _apply_session_mutation(
        connection,
        scope=plan.scope,
        source_row=row,
        mutation=mutation,
        committed_at=committed_at,
        update_last_active=selected.status is not TerminalStatus.ABORTED,
        recovery_locked=isinstance(context, PreparedRecoveryContext),
    )
    if isinstance(context, PreparedFinalizationContext):
        generation_update = await connection.execute(
            """
            UPDATE turn_generations
            SET state='closed',row_version=row_version+1,closed_at=?,terminal_receipt_id=?
            WHERE turn_id=? AND generation=? AND protocol_version=1 AND state='closing'
              AND row_version=? AND terminal_candidate_id=? AND terminal_receipt_id IS NULL
              AND finalization_owner_instance_id=? AND finalization_epoch=?
            """,
            (
                committed_at,
                plan.reserved_receipt_id,
                plan.scope.turn_id,
                plan.scope.generation,
                context.finalization.generation_row_version,
                plan.candidate_id,
                context.finalization.finalization_owner_instance_id,
                context.finalization.finalization_epoch,
            ),
        )
    else:
        generation_update = await connection.execute(
            """
            UPDATE turn_generations
            SET state='closed',row_version=row_version+1,closed_at=?,terminal_receipt_id=?,
                recovery_resolution='receipt_committed'
            WHERE turn_id=? AND generation=? AND protocol_version=1 AND state='recovery_pending'
              AND row_version=? AND terminal_candidate_id=? AND terminal_receipt_id IS NULL
              AND finalization_owner_instance_id=? AND finalization_epoch=?
            """,
            (
                committed_at,
                plan.reserved_receipt_id,
                plan.scope.turn_id,
                plan.scope.generation,
                context.processing.generation_row_version,
                plan.candidate_id,
                context.processing.recovery_owner_instance_id,
                context.processing.finalization_epoch,
            ),
        )
    if generation_update.rowcount != 1:
        raise TerminalProtocolMalformedError("terminal generation commit CAS changed")
    if isinstance(context, PreparedRecoveryContext):
        recovery_update = await connection.execute(
            """
            UPDATE turn_recovery_records
            SET state='resolved',phase='resolved',candidate_id=?,receipt_id=?,
                resolution='receipt_committed',resolved_at=?,recovery_lease_expires_at=NULL
            WHERE recovery_id=? AND state='active' AND recovery_owner_instance_id=?
              AND recovery_claim_epoch=?
            """,
            (
                plan.candidate_id,
                plan.reserved_receipt_id,
                committed_at,
                context.processing.recovery_id,
                context.processing.recovery_owner_instance_id,
                context.processing.recovery_claim_epoch,
            ),
        )
        if recovery_update.rowcount != 1:
            raise TerminalProtocolMalformedError("terminal recovery resolution CAS changed")
    await _update_turn_terminal_projection(
        connection,
        row=row,
        selected=selected,
        plan=plan,
        final_answer=final_answer,
        committed_at=committed_at,
    )
    if is_explicit_user_cancel_receipt(
        status=selected.status,
        reason_code=selected.reason_code,
        generation_kind=row["kind"],
        closing_reason=row["closing_reason"],
    ):
        await _cancel_running_plan_for_turn_in_tx(
            connection,
            turn_id=plan.scope.turn_id,
            updated_at=committed_at,
        )
    turn_pointer_update = await connection.execute(
        """
        UPDATE turns
        SET current_terminal_receipt_id=?,last_terminal_generation=?
        WHERE id=? AND terminal_protocol_version=1 AND current_generation=?
          AND current_terminal_receipt_id IS NULL
        """,
        (
            plan.reserved_receipt_id,
            plan.scope.generation,
            plan.scope.turn_id,
            plan.scope.generation,
        ),
    )
    if turn_pointer_update.rowcount != 1:
        raise TerminalProtocolMalformedError("terminal Turn receipt pointer CAS changed")
    candidate_update = await connection.execute(
        """
        UPDATE turn_terminal_candidates SET state='committed',committed_at=?
        WHERE candidate_id=? AND state='prepared' AND candidate_projection_digest=?
        """,
        (committed_at, plan.candidate_id, plan.candidate_projection_digest),
    )
    if candidate_update.rowcount != 1:
        raise TerminalProtocolMalformedError("terminal candidate commit CAS changed")
    if selected.status is TerminalStatus.COMPLETED:
        async with connection.execute(
            """
            SELECT COALESCE(SUM(tokens_input),0),COALESCE(SUM(tokens_output),0),
                   COALESCE(SUM(cost_usd),0)
            FROM turns WHERE session_id=?
            """,
            (plan.scope.session_id,),
        ) as cursor:
            totals = await cursor.fetchone()
        await connection.execute(
            "UPDATE sessions SET tokens_total=?,cost_usd=? WHERE id=?",
            (int(totals[0]) + int(totals[1]), float(totals[2]), plan.scope.session_id),
        )
    return CommittedReceipt(
        receipt_id=plan.reserved_receipt_id,
        operation_kind=selected.operation_kind,
        candidate_id=plan.candidate_id,
        candidate_projection_digest=plan.candidate_projection_digest,
        selected_projection_digest=receipt_digest,
        selected_variant_key=selected.variant_key,
        selected_outcome_variant=selected.outcome_variant,
        selected_session_variant=selected.session_variant,
        terminal_event_id=plan.reserved_terminal_event_id,
        first_event_seq=first_event_seq,
        last_event_seq=last_event_seq,
        status=selected.status,
    )


async def _read_committed_receipt(
    connection: aiosqlite.Connection,
    *,
    candidate_id: str,
    candidate_projection_digest: str,
) -> CommittedReceipt | None:
    async with connection.execute(
        """
        SELECT c.turn_id,c.generation,c.state AS candidate_state,
               t.session_id,w.team_id,
               c.reserved_receipt_id,c.reserved_terminal_event_id,
               c.candidate_projection_digest AS stored_candidate_digest,
               g.state AS generation_state,g.terminal_receipt_id AS generation_receipt_id,
               g.recovery_resolution AS generation_recovery_resolution,
               EXISTS(
                 SELECT 1 FROM turn_terminal_deletion_ledgers d
                 WHERE d.turn_id=c.turn_id AND d.generation=c.generation
               ) AS deletion_exists,
               r.candidate_projection_digest AS receipt_candidate_digest,
               r.turn_id AS receipt_turn_id,r.generation AS receipt_generation,
               r.protocol_version AS receipt_protocol_version,
               r.*,v.required_manifest_template_digest,v.variant_digest,
               m.mutation_projection_json,m.mutation_digest AS ledger_mutation_digest,
               m.selected_variant_key AS ledger_selected_variant_key
        FROM turn_terminal_candidates c
        JOIN turns t ON t.id=c.turn_id
        JOIN sessions ss ON ss.id=t.session_id
        JOIN workspaces w ON w.id=ss.workspace_id
        JOIN turn_generations g ON g.turn_id=c.turn_id AND g.generation=c.generation
        LEFT JOIN turn_terminal_receipts r ON r.candidate_id=c.candidate_id
        LEFT JOIN turn_terminal_candidate_variants v
          ON v.candidate_id=r.candidate_id AND v.variant_key=r.selected_variant_key
        LEFT JOIN turn_terminal_session_mutations m ON m.receipt_id=r.receipt_id
        WHERE c.candidate_id=?
        """,
        (candidate_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    if row["stored_candidate_digest"] != candidate_projection_digest:
        raise TerminalProtocolMalformedError("terminal candidate digest conflict")
    if row["receipt_id"] is None:
        if row["candidate_state"] != "prepared":
            raise TerminalProtocolMalformedError("terminal candidate state lacks receipt")
        return None
    if (
        row["candidate_state"] != "committed"
        or row["reserved_receipt_id"] != row["receipt_id"]
        or row["reserved_terminal_event_id"] != row["terminal_event_id"]
        or row["candidate_id"] != candidate_id
        or row["receipt_turn_id"] != row["turn_id"]
        or row["receipt_generation"] != row["generation"]
        or row["receipt_protocol_version"] != 1
        or row["generation_state"] != GenerationState.CLOSED
        or row["generation_receipt_id"] != row["receipt_id"]
        or row["generation_recovery_resolution"] not in {None, "receipt_committed"}
        or row["deletion_exists"] != 0
        or row["receipt_candidate_digest"] != candidate_projection_digest
        or row["ledger_selected_variant_key"] != row["selected_variant_key"]
        or row["selected_session_mutation_digest"] != row["ledger_mutation_digest"]
    ):
        raise TerminalProtocolMalformedError("terminal receipt identity is malformed")
    scope = TerminalScope(
        team_id=_required_text(row["team_id"], "receipt.team_id"),
        session_id=_required_text(row["session_id"], "receipt.session_id"),
        turn_id=_required_text(row["turn_id"], "receipt.turn_id"),
        generation=_positive_int(row["generation"], "receipt.generation"),
    )
    candidate_plan = await _load_terminal_candidate(connection, scope, candidate_id)
    if candidate_plan.candidate_projection_digest != candidate_projection_digest:
        raise TerminalProtocolMalformedError("terminal receipt candidate reconstruction changed")
    selected = next(
        (variant for variant in candidate_plan.variants if variant.variant_key == row["selected_variant_key"]),
        None,
    )
    if selected is None or (
        row["operation_kind"] != selected.operation_kind
        or row["status"] != selected.status.value
        or row["reason_code"] != selected.reason_code
        or row["message_key"] != selected.message_key
        or row["final_answer_mode"] != selected.final_answer_mode.value
        or row["final_answer_event_id"] != selected.final_answer_event_id
        or row["selected_outcome_variant"] != selected.outcome_variant.value
        or row["selected_session_variant"] != selected.session_variant.value
        or row["required_manifest_template_digest"] != selected.required_manifest_template_digest
        or row["variant_digest"] != selected.variant_digest
        or row["persisted_event_highwater"] != candidate_plan.persisted_event_highwater
        or row["first_event_seq"] != candidate_plan.persisted_event_highwater + 1
        or row["last_event_seq"] != candidate_plan.persisted_event_highwater + len(selected.events)
    ):
        raise TerminalProtocolMalformedError("terminal receipt selected candidate variant changed")
    mutation_projection = _parse_payload(row["mutation_projection_json"])
    if (
        canonical_json_v1(mutation_projection) != row["mutation_projection_json"]
        or canonical_digest_v1(mutation_projection) != row["ledger_mutation_digest"]
    ):
        raise TerminalProtocolMalformedError("terminal Session mutation ledger digest mismatch")
    expected_disposition = "control_preserve_unrelated" if candidate_plan.origin is CandidateOrigin.CONTROL else selected.session_variant.value
    if (
        mutation_projection.get("selected_variant_key") != selected.variant_key
        or mutation_projection.get("disposition") != expected_disposition
        or mutation_projection.get("selected_session_mutation_template_digest") != selected.session_mutation_template_digest
        or mutation_projection.get("next_turn_identity_digest")
        != (
            canonical_digest_v1(dict(selected.session_mutation_template["next_turn"]))
            if selected.session_variant is SessionVariant.FOLLOWUP
            else None
        )
    ):
        raise TerminalProtocolMalformedError("terminal Session mutation is not bound to selected candidate")
    if candidate_plan.origin is CandidateOrigin.CONTROL and (
        mutation_projection.get("old_recovery_tuple") != mutation_projection.get("new_recovery_tuple")
        or mutation_projection.get("old_steer_freeze_tuple") != mutation_projection.get("new_steer_freeze_tuple")
        or not isinstance(mutation_projection.get("old_recovery_tuple"), Mapping)
        or not isinstance(mutation_projection.get("old_steer_freeze_tuple"), Mapping)
    ):
        raise TerminalProtocolMalformedError("control Session mutation does not prove recovery/freeze preservation")
    try:
        extensions = json.loads(row["projection_extensions_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise TerminalProtocolMalformedError("terminal receipt extensions are malformed") from exc
    expected_extensions = [dict(value) for value in candidate_plan.projection_extensions]
    if not isinstance(extensions, list) or canonical_json_v1(extensions) != canonical_json_v1(expected_extensions):
        raise TerminalProtocolMalformedError("terminal receipt extensions changed")
    try:
        manifest = json.loads(row["required_event_manifest_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise TerminalProtocolMalformedError("terminal receipt manifest is malformed") from exc
    if not isinstance(manifest, list) or canonical_json_v1(manifest) != row["required_event_manifest_json"]:
        raise TerminalProtocolMalformedError("terminal receipt manifest is malformed")
    manifest_template = [
        {
            "event_id": item.get("event_id"),
            "event_type": item.get("event_type"),
            "ordinal": ordinal,
            "payload_digest": item.get("payload_digest"),
        }
        for ordinal, item in enumerate(manifest, start=1)
        if isinstance(item, dict)
    ]
    if canonical_digest_v1(manifest_template) != selected.required_manifest_template_digest:
        raise TerminalProtocolMalformedError("terminal receipt manifest no longer matches selected candidate")
    event_ids = [item.get("event_id") for item in manifest if isinstance(item, dict)]
    if len(event_ids) != len(manifest) or any(not isinstance(value, str) or not value for value in event_ids):
        raise TerminalProtocolMalformedError("terminal receipt manifest identity is malformed")
    found: dict[str, aiosqlite.Row] = {}
    for start in range(0, len(event_ids), 500):
        chunk = event_ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        async with connection.execute(
            f"""
            SELECT id,event_seq,event_type,actor_scope,phase_scope,payload_json,payload_digest,generation
            FROM turn_events WHERE turn_id=? AND protocol_version=1 AND id IN ({placeholders})
            """,
            (row["turn_id"], *chunk),
        ) as cursor:
            for event in await cursor.fetchall():
                found[event["id"]] = event
    if set(found) != set(event_ids):
        raise TerminalProtocolMalformedError("terminal receipt required event is missing")
    prior_seq = 0
    for item in manifest:
        assert isinstance(item, dict)
        event = found[item["event_id"]]
        payload = _parse_payload(event["payload_json"])
        projection = {
            "event_id": event["id"],
            "event_seq": event["event_seq"],
            "event_type": event["event_type"],
            "actor_scope": event["actor_scope"],
            "phase_scope": event["phase_scope"],
            "payload_digest": event["payload_digest"],
        }
        if (
            event["generation"] != row["generation"]
            or canonical_digest_v1(payload) != event["payload_digest"]
            or item != {**projection, "canonical_row_digest": _event_row_digest(projection)}
            or int(event["event_seq"]) <= prior_seq
        ):
            raise TerminalProtocolMalformedError("terminal receipt required event digest mismatch")
        prior_seq = int(event["event_seq"])
    manifest_offset = 0
    if selected.final_answer_mode is FinalAnswerMode.PRECOMMITTED:
        first = manifest[0] if manifest else None
        if (
            not isinstance(first, dict)
            or first.get("event_id") != selected.final_answer_event_id
            or first.get("event_type") != "final_answer"
            or first.get("payload_digest") != selected.final_answer_digest
            or found[first["event_id"]]["actor_scope"] != ActorScope.ROOT.value
            or found[first["event_id"]]["phase_scope"] != PhaseScope.IN_GENERATION.value
        ):
            raise TerminalProtocolMalformedError("terminal receipt precommitted answer changed")
        manifest_offset = 1
    staged_manifest = manifest[manifest_offset:]
    if len(staged_manifest) != len(selected.required_events):
        raise TerminalProtocolMalformedError("terminal receipt staged manifest count changed")
    for item, expected_event in zip(staged_manifest, selected.required_events, strict=True):
        assert isinstance(item, dict)
        event = found[item["event_id"]]
        if (
            event["id"] != expected_event.event_id
            or event["event_type"] != expected_event.event_type
            or event["actor_scope"] != expected_event.actor_scope.value
            or event["phase_scope"] != expected_event.phase_scope.value
            or event["payload_json"] != expected_event.payload_json
            or event["payload_digest"] != expected_event.payload_digest
        ):
            raise TerminalProtocolMalformedError("terminal receipt staged event changed")
    async with connection.execute(
        """
        SELECT id,event_seq,event_type,actor_scope,phase_scope,payload_json,payload_digest,
               generation,terminal_receipt_id
        FROM turn_events WHERE turn_id=? AND id=? AND protocol_version=1
        """,
        (row["turn_id"], row["terminal_event_id"]),
    ) as cursor:
        terminal = await cursor.fetchone()
    if (
        terminal is None
        or terminal["generation"] != row["generation"]
        or terminal["event_type"] != "done"
        or terminal["event_seq"] != row["last_event_seq"]
        or terminal["terminal_receipt_id"] != row["receipt_id"]
        or canonical_digest_v1(_parse_payload(terminal["payload_json"])) != terminal["payload_digest"]
        or terminal["payload_json"] != selected.events[-1].payload_json
        or terminal["payload_digest"] != selected.events[-1].payload_digest
        or terminal["actor_scope"] != selected.events[-1].actor_scope.value
        or terminal["phase_scope"] != selected.events[-1].phase_scope.value
    ):
        raise TerminalProtocolMalformedError("terminal receipt done event is malformed")
    staged_event_sequences = [
        *(int(found[event.event_id]["event_seq"]) for event in selected.required_events),
        int(terminal["event_seq"]),
    ]
    if staged_event_sequences != list(
        range(
            _positive_int(row["first_event_seq"], "receipt.first_event_seq"),
            _positive_int(row["last_event_seq"], "receipt.last_event_seq") + 1,
        )
    ):
        raise TerminalProtocolMalformedError("terminal receipt staged batch sequence changed")
    selected_manifest_digest = canonical_digest_v1(
        {
            "required_event_manifest": manifest,
            "terminal_event": {
                "event_id": terminal["id"],
                "event_seq": terminal["event_seq"],
                "event_type": terminal["event_type"],
                "payload_digest": terminal["payload_digest"],
            },
        }
    )
    if selected_manifest_digest != row["selected_manifest_digest"]:
        raise TerminalProtocolMalformedError("terminal receipt selected manifest digest mismatch")
    receipt_projection = {
        **({"operation_kind": selected.operation_kind} if selected.operation_kind is not None else {}),
        "candidate_id": candidate_id,
        "candidate_projection_digest": candidate_projection_digest,
        "message_key": row["message_key"],
        "reason_code": row["reason_code"],
        "selected_manifest_digest": selected_manifest_digest,
        "selected_outcome_variant": row["selected_outcome_variant"],
        "selected_session_mutation_digest": row["ledger_mutation_digest"],
        "selected_session_variant": row["selected_session_variant"],
        "selected_variant_key": row["selected_variant_key"],
        "status": row["status"],
        "terminal_event_id": row["terminal_event_id"],
    }
    selected_projection_digest = canonical_digest_v1(receipt_projection)
    if selected_projection_digest != row["selected_projection_digest"]:
        raise TerminalProtocolMalformedError("terminal receipt projection digest mismatch")
    try:
        outcome = CandidateOutcome(row["selected_outcome_variant"])
        session_variant = SessionVariant(row["selected_session_variant"])
        status = TerminalStatus(row["status"])
    except ValueError as exc:
        raise TerminalProtocolMalformedError("terminal receipt enum is malformed") from exc
    return CommittedReceipt(
        receipt_id=_required_text(row["receipt_id"], "receipt.receipt_id"),
        candidate_id=candidate_id,
        candidate_projection_digest=candidate_projection_digest,
        selected_projection_digest=selected_projection_digest,
        selected_variant_key=_required_text(row["selected_variant_key"], "receipt.selected_variant_key"),
        selected_outcome_variant=outcome,
        selected_session_variant=session_variant,
        terminal_event_id=_required_text(row["terminal_event_id"], "receipt.terminal_event_id"),
        first_event_seq=_positive_int(row["first_event_seq"], "receipt.first_event_seq"),
        last_event_seq=_positive_int(row["last_event_seq"], "receipt.last_event_seq"),
        status=status,
        operation_kind=selected.operation_kind,
    )


def _canonical_admission_request_projection(
    *,
    execution_intent: Mapping[str, object],
    request_id: str,
    session_id: str,
    team_id: str,
    turn_id: str,
    user_text: str,
) -> dict[str, object]:
    return {
        "execution_intent": execution_intent,
        "request_id": request_id,
        "session_id": session_id,
        "team_id": team_id,
        "turn_id": turn_id,
        "user_text": user_text,
    }


def canonical_admission_request_digest(
    *,
    execution_intent: Mapping[str, object],
    request_id: str,
    session_id: str,
    team_id: str,
    turn_id: str,
    user_text: str,
    expected_execution_generation: int | None = None,
    user_message_id: str | None = None,
) -> str:
    """Return the sole canonical digest for a Terminal admission action."""

    if (expected_execution_generation is None) != (user_message_id is None):
        raise ValueError("terminal steer digest identity is incomplete")
    projection = _canonical_admission_request_projection(
        execution_intent=_canonical_mapping_copy(execution_intent),
        request_id=request_id,
        session_id=session_id,
        team_id=team_id,
        turn_id=turn_id,
        user_text=user_text,
    )
    if expected_execution_generation is not None:
        projection = {
            **projection,
            "expected_execution_generation": expected_execution_generation,
            "user_message_id": user_message_id,
        }
    return canonical_digest_v1(projection)


def _canonical_execution_intent(
    request: TerminalAdmissionRequest,
    execution_intent: dict[str, object],
    *,
    user_text: str | None = None,
) -> dict[str, object]:
    return _canonical_admission_request_projection(
        execution_intent=execution_intent,
        request_id=request.context.request_id,
        session_id=request.context.session_id,
        team_id=request.context.team_id,
        turn_id=request.turn_id,
        user_text=request.user_text if user_text is None else user_text,
    )


def _canonical_mapping_copy(value: Mapping[str, object]) -> dict[str, object]:
    parsed = json.loads(canonical_json_v1(value))
    if not isinstance(parsed, dict):
        raise TypeError("terminal execution intent must canonicalize to an object")
    return parsed


def _typed_execution_context_matches(
    execution_intent: Mapping[str, object],
    presentation_locale: str,
) -> bool:
    """Validate the complete durable context carried by one typed Web start."""

    required_keys = {
        "active_datarc_project",
        "attachment_refs",
        "attachments",
        "language_context",
        "member_uid",
        "parts",
        "planning_mode",
        "preferred_model",
        "preferred_provider",
        "text",
        "workspace_id",
    }
    actual_keys = set(execution_intent)
    optional_keys = {
        "context_maintenance",
        "agent_draft_target",
        "billing_rate_card_version",
        "effect_contract_version",
        "paradigm_v2",
        "plan_execution_id",
        "expected_plan_revision",
        "viewer_time_zone",
        "datarc_selection",
    }
    if not required_keys <= actual_keys or not actual_keys - required_keys <= optional_keys:
        return False
    if "context_maintenance" in execution_intent:
        from bowhead_core.context_action import ContextMaintenanceAction

        try:
            ContextMaintenanceAction.model_validate(execution_intent["context_maintenance"])
        except (ValueError, TypeError):
            return False
        if any(
            execution_intent.get(key) for key in ("text", "attachments", "attachment_refs", "parts", "agent_draft_target", "plan_execution_id")
        ) or execution_intent.get("planning_mode") not in (None, "off"):
            return False
    if "viewer_time_zone" in execution_intent and (
        execution_intent["language_context"] is not None
        or not isinstance(execution_intent["viewer_time_zone"], str)
        or load_iana_time_zone(execution_intent["viewer_time_zone"]) is None
        or execution_intent["viewer_time_zone"].strip() != execution_intent["viewer_time_zone"]
    ):
        # Only carried beside a missing language snapshot, as an exact known zone name.
        return False
    if "agent_draft_target" in execution_intent:
        try:
            target = AgentDraftSendTarget.model_validate(execution_intent["agent_draft_target"])
            if (
                target.model_dump() != execution_intent["agent_draft_target"]
                or "plan_execution_id" in execution_intent
                or execution_intent.get("planning_mode") in {"planning", True}
            ):
                return False
        except (ValueError, TypeError):
            return False
    if "paradigm_v2" in execution_intent and not isinstance(
        execution_intent["paradigm_v2"],
        bool,
    ):
        return False
    if "plan_execution_id" in execution_intent and (
        not isinstance(execution_intent["plan_execution_id"], str)
        or not execution_intent["plan_execution_id"]
        or execution_intent["plan_execution_id"].strip() != execution_intent["plan_execution_id"]
        or len(execution_intent["plan_execution_id"]) > 128
    ):
        return False
    if "expected_plan_revision" in execution_intent and (
        type(execution_intent["expected_plan_revision"]) is not int
        or execution_intent["expected_plan_revision"] < 1
        or "plan_execution_id" not in execution_intent
    ):
        return False
    try:
        resolve_effect_execution_contract(execution_intent)
    except (TerminalProtocolMalformedError, TerminalProtocolFutureVersionError):
        return False
    active_datarc_project = execution_intent["active_datarc_project"]
    attachment_refs = execution_intent["attachment_refs"]
    attachments = execution_intent["attachments"]
    member_uid = execution_intent["member_uid"]
    parts = execution_intent["parts"]
    planning_mode = execution_intent["planning_mode"]
    preferred_model = execution_intent["preferred_model"]
    preferred_provider = execution_intent["preferred_provider"]
    text = execution_intent["text"]
    workspace_id = execution_intent["workspace_id"]
    if (
        (active_datarc_project is not None and (not isinstance(active_datarc_project, str) or not active_datarc_project))
        or not isinstance(attachment_refs, list)
        or any(not isinstance(value, dict) for value in attachment_refs)
        or not isinstance(attachments, list)
        or any(not isinstance(value, str) or not value for value in attachments)
        or not isinstance(member_uid, str)
        or (parts is not None and (not isinstance(parts, list) or any(not isinstance(value, dict) for value in parts)))
        or (planning_mode is not None and not isinstance(planning_mode, (str, bool)))
        or (preferred_model is not None and not isinstance(preferred_model, str))
        or (preferred_provider is not None and not isinstance(preferred_provider, str))
        or not isinstance(text, str)
        or not isinstance(workspace_id, str)
        or not workspace_id
    ):
        return False

    if "datarc_selection" in execution_intent:
        from bowhead_core.datarc_selection import DatarcSelectionSnapshot

        try:
            source = DatarcSelectionSnapshot.model_validate(execution_intent["datarc_selection"])
        except ValueError:
            return False
        if (source.datarc_workspace_id, source.member_uid) != (active_datarc_project, member_uid):
            return False
    raw = execution_intent["language_context"]
    if raw is None:
        return presentation_locale == "zh-CN"
    if not isinstance(raw, dict):
        return False
    try:
        language_context = LanguageContext.from_dict(raw)
    except ValueError:
        return False
    return language_context.to_dict() == raw and language_context.ui_language == presentation_locale


async def _read_session_for_admission(
    connection: aiosqlite.Connection,
    session_id: str,
) -> aiosqlite.Row | None:
    async with connection.execute(
        """
        SELECT s.*, w.team_id AS bound_team_id
        FROM sessions s
        JOIN workspaces w ON w.id=s.workspace_id
        WHERE s.id=?
        """,
        (session_id,),
    ) as cursor:
        return await cursor.fetchone()


def _decision_auth_fence_scopes(context: DecisionContext) -> tuple[AuthFenceScope, ...]:
    scopes = {
        AuthFenceScope(AuthFenceScopeKind.TEAM_MEMBERSHIP, context.scope.team_id, context.scope.team_id),
        AuthFenceScope(AuthFenceScopeKind.RESOURCE_GRANT, context.scope.team_id, context.scope.team_id),
        AuthFenceScope(AuthFenceScopeKind.SESSION_VISIBILITY, context.scope.team_id, context.scope.session_id),
    }
    if not is_narrow_system_decision(context):
        for principal in {
            context.principals.requester_ref,
            context.principals.actor_ref,
            context.principals.effect_principal_ref,
        }:
            if not principal.startswith("system:"):
                scopes.add(
                    AuthFenceScope(
                        AuthFenceScopeKind.USER_ACCOUNT,
                        "__global__",
                        principal,
                    )
                )
    return tuple(sorted(scopes, key=AuthFenceScope.sort_key))


async def _read_decision_auth_context(
    connection: aiosqlite.Connection,
    decision_id: str,
) -> DecisionContext:
    async with connection.execute(
        """
        SELECT d.*,t.session_id,w.team_id
        FROM turn_control_decisions d
        JOIN turns t ON t.id=d.turn_id
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        WHERE d.decision_id=?
        """,
        (decision_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise DecisionSourceNotReadyError("decision auth source is missing")
    if row["state"] == DecisionState.TERMINAL_CONTROL_COMMITTED.value and row["target_generation"] is None:
        raise DecisionAuthorizationDeniedError("access_revoked")
    try:
        source_kind = DecisionSourceKind(row["source_kind"])
        action = DecisionAction(row["action"])
    except ValueError as exc:
        raise TerminalProtocolMalformedError("decision auth source enum is malformed") from exc
    return DecisionContext(
        scope=TerminalScope(
            team_id=_required_text(row["team_id"], "decision auth team"),
            session_id=_required_text(
                row["session_id"],
                "decision auth session",
            ),
            turn_id=_required_text(row["turn_id"], "decision auth turn"),
            generation=_positive_int(
                row["prior_generation"],
                "decision auth generation",
            ),
        ),
        decision_id=_required_text(
            row["decision_id"],
            "decision auth id",
        ),
        source_kind=source_kind,
        source_id=_required_text(
            row["source_id"],
            "decision auth source",
        ),
        action=action,
        principals=DecisionPrincipalSet(
            requester_ref=_required_text(
                row["requester_ref"],
                "decision auth requester",
            ),
            actor_ref=_required_text(
                row["actor_ref"],
                "decision auth actor",
            ),
            effect_principal_ref=_required_text(
                row["effect_principal_ref"],
                "decision auth effect principal",
            ),
        ),
        authz_snapshot_digest=_required_text(
            row["authz_snapshot_digest"],
            "decision auth audit digest",
        ),
        prior_receipt_id=_required_text(
            row["prior_receipt_id"],
            "decision auth prior receipt",
        ),
    )


async def _skip_available_decision_effects(
    connection: aiosqlite.Connection,
    *,
    decision_id: str,
    code: str,
    now_text: str,
) -> int:
    """Settle pending or abandoned effects without stealing a live claim."""

    update = await connection.execute(
        """
        UPDATE turn_decision_effects
        SET state='skipped',claim_owner=NULL,claim_expires_at=NULL,
            next_attempt_at=NULL,last_code=NULL,resolution_code=?,
            applied_at=?
        WHERE decision_id=? AND (
          state='pending' OR (
            state='claimed' AND (
              claim_expires_at IS NULL OR claim_expires_at<=?
            )
          )
        )
        """,
        (code[:128], now_text, decision_id, now_text),
    )
    return update.rowcount


async def _validate_current_decision_auth(
    connection: aiosqlite.Connection,
    context: DecisionContext,
) -> None:
    try:
        vector = await _read_auth_fence_vector(connection, _decision_auth_fence_scopes(context))
    except TerminalProtocolMalformedError as exc:
        raise DecisionAccessRevokedError("decision authorization scope is unavailable") from exc
    if vector.auth_fence_vector_digest != context.authz_snapshot_digest:
        raise DecisionAccessRevokedError("decision authorization changed")


async def _validate_effect_dispatch_auth(
    connection: aiosqlite.Connection,
    execution: ExecutionGenerationContext,
) -> None:
    """Rebuild the persisted generation's complete authorization fence."""

    async with connection.execute(
        """
        SELECT g.decision_id,g.execution_authz_snapshot_digest,
               t.session_id,s.user_id,w.team_id
        FROM turn_generations g
        JOIN turns t ON t.id=g.turn_id
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        WHERE g.turn_id=? AND g.generation=?
        """,
        (execution.scope.turn_id, execution.scope.generation),
    ) as cursor:
        generation = await cursor.fetchone()
    if generation is None or generation["session_id"] != execution.scope.session_id or generation["team_id"] != execution.scope.team_id:
        raise TerminalProtocolMalformedError("effect dispatch authorization scope is missing")
    decision_id = generation["decision_id"]
    if decision_id is None:
        scopes = (
            AuthFenceScope(
                AuthFenceScopeKind.TEAM_MEMBERSHIP,
                execution.scope.team_id,
                execution.scope.team_id,
            ),
            AuthFenceScope(
                AuthFenceScopeKind.RESOURCE_GRANT,
                execution.scope.team_id,
                execution.scope.team_id,
            ),
            AuthFenceScope(
                AuthFenceScopeKind.SESSION_VISIBILITY,
                execution.scope.team_id,
                execution.scope.session_id,
            ),
            AuthFenceScope(
                AuthFenceScopeKind.USER_ACCOUNT,
                "__global__",
                _required_text(generation["user_id"], "effect dispatch Session owner"),
            ),
        )
        try:
            current = await _read_auth_fence_vector(connection, scopes)
        except TerminalProtocolMalformedError as exc:
            raise DecisionAccessRevokedError("effect dispatch authorization scope is unavailable") from exc
        if current.auth_fence_vector_digest != _required_text(
            generation["execution_authz_snapshot_digest"],
            "effect dispatch execution auth digest",
        ):
            raise DecisionAccessRevokedError("effect dispatch authorization changed")
        return
    async with connection.execute(
        """
        SELECT d.*,g.execution_authz_snapshot_digest
        FROM turn_generations g
        JOIN turn_control_decisions d ON d.decision_id=g.decision_id
        WHERE g.turn_id=? AND g.generation=? AND d.decision_id=?
        """,
        (
            execution.scope.turn_id,
            execution.scope.generation,
            decision_id,
        ),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise TerminalProtocolMalformedError("effect dispatch decision authorization is missing")
    try:
        source_kind = DecisionSourceKind(row["source_kind"])
        action = DecisionAction(row["action"])
    except ValueError as exc:
        raise TerminalProtocolMalformedError("effect dispatch decision kind is malformed") from exc
    context = DecisionContext(
        scope=TerminalScope(
            team_id=execution.scope.team_id,
            session_id=execution.scope.session_id,
            turn_id=execution.scope.turn_id,
            generation=_positive_int(
                row["prior_generation"],
                "effect dispatch prior generation",
            ),
        ),
        decision_id=_required_text(
            row["decision_id"],
            "effect dispatch decision id",
        ),
        source_kind=source_kind,
        source_id=_required_text(
            row["source_id"],
            "effect dispatch source id",
        ),
        action=action,
        principals=DecisionPrincipalSet(
            requester_ref=_required_text(
                row["requester_ref"],
                "effect dispatch requester",
            ),
            actor_ref=_required_text(
                row["actor_ref"],
                "effect dispatch actor",
            ),
            effect_principal_ref=_required_text(
                row["effect_principal_ref"],
                "effect dispatch principal",
            ),
        ),
        authz_snapshot_digest=_required_text(
            row["execution_authz_snapshot_digest"],
            "effect dispatch execution auth digest",
        ),
        prior_receipt_id=_required_text(
            row["prior_receipt_id"],
            "effect dispatch prior receipt",
        ),
    )
    await _validate_current_decision_auth(connection, context)


async def _has_effect_unknown_recovery_latch(
    connection: aiosqlite.Connection,
    scope: TerminalScope,
) -> bool:
    async with connection.execute(
        """
        SELECT 1 FROM turn_recovery_records
        WHERE turn_id=? AND generation=? AND scope='terminal'
          AND kind='effect_unknown'
        LIMIT 1
        """,
        (scope.turn_id, scope.generation),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _read_decision_protocol_row(
    connection: aiosqlite.Connection,
    scope: TerminalScope,
) -> aiosqlite.Row | None:
    async with connection.execute(
        """
        SELECT t.id AS turn_id,t.session_id,t.terminal_protocol_version,t.current_generation,
               t.generation_transition_epoch,t.current_terminal_receipt_id,t.event_sequence_next,
               g.state AS generation_state,g.terminal_receipt_id AS generation_receipt_id,
               r.status AS receipt_status,
                  r.operation_kind AS receipt_operation_kind,
                  t.operation_kind AS turn_operation_kind,
               s.active_turn_id,s.active_turn_protocol_version,s.active_turn_generation,
               s.active_turn_claim_epoch,s.recovery_lock_turn_id,s.recovery_lock_generation,
               s.recovery_lock_reason,s.steer_buffer_json,s.steer_buffer_state,
               s.steer_buffer_revision,s.steer_freeze_turn_id,s.steer_freeze_generation,
               s.steer_freeze_claim_epoch,s.steer_frozen_revision,s.deleted_at,
               s.workspace_id AS session_workspace_id,w.team_id
        FROM turns t
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        JOIN turn_generations g ON g.turn_id=t.id AND g.generation=?
        LEFT JOIN turn_terminal_receipts r ON r.receipt_id=t.current_terminal_receipt_id
        WHERE t.id=? AND t.session_id=? AND w.team_id=?
        """,
        (scope.generation, scope.turn_id, scope.session_id, scope.team_id),
    ) as cursor:
        return await cursor.fetchone()


def _validate_decision_protocol_row(row: aiosqlite.Row | None, context: DecisionContext) -> None:
    if row is None:
        raise DecisionSourceNotReadyError("decision Turn is unavailable")
    version = row["terminal_protocol_version"]
    if version is None:
        raise DecisionSourceNotReadyError("decision source belongs to a legacy Turn")
    if version != 1:
        raise TerminalProtocolFutureVersionError("decision source uses a future protocol")
    if row["team_id"] != context.scope.team_id or row["session_id"] != context.scope.session_id:
        raise DecisionAccessRevokedError("decision scope access changed")


def _validate_decision_pause_truth(row: aiosqlite.Row, context: DecisionContext) -> None:
    expected_status = {
        DecisionSourceKind.CLARIFICATION: TerminalStatus.CLARIFICATION_PENDING.value,
        DecisionSourceKind.APPROVAL: TerminalStatus.APPROVAL_PENDING.value,
    }.get(context.source_kind)
    if (
        row["current_generation"] != context.scope.generation
        or row["current_terminal_receipt_id"] != context.prior_receipt_id
        or row["generation_state"] != GenerationState.CLOSED.value
        or row["generation_receipt_id"] != context.prior_receipt_id
        or (expected_status is not None and row["receipt_status"] != expected_status)
        or (
            context.source_kind is DecisionSourceKind.SYSTEM_CONTROL
            and row["receipt_status"]
            not in {
                TerminalStatus.CLARIFICATION_PENDING.value,
                TerminalStatus.APPROVAL_PENDING.value,
            }
        )
    ):
        raise DecisionConflictError("decision prior pause truth changed")


async def _read_stored_decision(
    connection: aiosqlite.Connection,
    decision_id: str,
) -> aiosqlite.Row | None:
    async with connection.execute(
        "SELECT * FROM turn_control_decisions WHERE decision_id=?",
        (decision_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _read_decision_replay(
    connection: aiosqlite.Connection,
    *,
    request: DecisionRecordRequest,
    decision_digest: str,
) -> RecordedDecision | None:
    async with connection.execute(
        "SELECT * FROM turn_control_decisions WHERE source_kind=? AND source_id=?",
        (request.context.source_kind.value, request.context.source_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    expected = {
        "decision_id": request.context.decision_id,
        "turn_id": request.context.scope.turn_id,
        "prior_generation": request.context.scope.generation,
        "action": request.context.action.value,
        "decision_digest": decision_digest,
        "actor_ref": request.context.actor_ref,
        "requester_ref": request.context.principals.requester_ref,
        "effect_principal_ref": request.context.principals.effect_principal_ref,
        "prior_receipt_id": request.context.prior_receipt_id,
        "source_projection_json": canonical_json_v1(canonical_decision_source_audit_projection(request)),
        "source_projection_digest": canonical_decision_payload_digest(request),
        "control_outcome_code": _decision_control_outcome_code(request),
    }
    if any(row[field] != value for field, value in expected.items()):
        raise DecisionConflictError("decision source identity conflicts with its durable winner")
    async with connection.execute(
        """
        SELECT effect_id,effect_kind,target_ref,payload_json,payload_digest
        FROM turn_decision_effects WHERE decision_id=? ORDER BY effect_id
        """,
        (request.context.decision_id,),
    ) as cursor:
        effect_rows = await cursor.fetchall()
    expected_effects = sorted(request.side_effects, key=lambda effect: effect.effect_id)
    if len(effect_rows) != len(expected_effects) or any(
        row_value["effect_id"] != effect.effect_id
        or row_value["effect_kind"] != effect.effect_kind
        or row_value["target_ref"] != effect.target_ref
        or row_value["payload_json"] != effect.payload_json
        or row_value["payload_digest"] != effect.payload_digest
        for row_value, effect in zip(effect_rows, expected_effects, strict=True)
    ):
        raise DecisionConflictError("decision side-effect plan conflicts with its durable winner")
    try:
        state = DecisionState(row["state"])
    except ValueError as exc:
        raise TerminalProtocolMalformedError("decision state is malformed") from exc
    target = row["target_generation"]
    return RecordedDecision(
        context=request.context,
        state=state,
        decision_digest=decision_digest,
        target_generation=(_positive_int(target, "decision.target_generation") if target is not None else None),
        replayed=True,
    )


def _validate_stored_decision(
    row: aiosqlite.Row | None,
    expected: RecordedDecision,
) -> None:
    context = expected.context
    if row is None:
        raise DecisionConflictError("durable decision is missing")
    projection = {
        "decision_id": context.decision_id,
        "source_kind": context.source_kind.value,
        "source_id": context.source_id,
        "turn_id": context.scope.turn_id,
        "prior_generation": context.scope.generation,
        "action": context.action.value,
        "decision_digest": expected.decision_digest,
        "actor_ref": context.actor_ref,
        "requester_ref": context.principals.requester_ref,
        "effect_principal_ref": context.principals.effect_principal_ref,
        "prior_receipt_id": context.prior_receipt_id,
    }
    if any(row[field] != value for field, value in projection.items()):
        raise DecisionConflictError("durable decision identity changed")


async def _has_competing_decision(
    connection: aiosqlite.Connection,
    context: DecisionContext,
) -> bool:
    async with connection.execute(
        """
        SELECT decision_id FROM turn_control_decisions
        WHERE turn_id=? AND prior_generation=? LIMIT 1
        """,
        (context.scope.turn_id, context.scope.generation),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None and row["decision_id"] != context.decision_id


async def _read_decision_source(
    connection: aiosqlite.Connection,
    context: DecisionContext,
) -> aiosqlite.Row | None:
    if context.source_kind is DecisionSourceKind.SYSTEM_CONTROL:
        return None
    table, key = ("clarification_requests", "request_id") if context.source_kind is DecisionSourceKind.CLARIFICATION else ("approvals", "id")
    try:
        async with connection.execute(
            f"SELECT * FROM {table} WHERE {key}=?",
            (context.source_id,),
        ) as cursor:
            return await cursor.fetchone()
    except aiosqlite.OperationalError as exc:
        raise DecisionSourceNotReadyError("decision source schema is unavailable") from exc


def _validate_decision_source(
    source: aiosqlite.Row | None,
    request: DecisionRecordRequest,
    *,
    row: aiosqlite.Row,
) -> None:
    context = request.context
    if context.source_kind is DecisionSourceKind.SYSTEM_CONTROL:
        if source is not None or request.target_source_state is not None:
            raise ValueError("system control cannot mutate an external source")
        return
    if source is None:
        raise DecisionConflictError("decision source is missing")
    if source["turn_id"] != context.scope.turn_id or source["status"] != request.expected_source_state:
        raise DecisionConflictError("decision source state changed")
    if source["workspace_id"] != row["session_workspace_id"]:
        raise DecisionConflictError("decision source workspace changed")
    source_session = source["session_id"]
    if source_session is not None and source_session != context.scope.session_id:
        raise DecisionConflictError("decision source Session changed")
    if source["user_id"] != context.principals.requester_ref:
        raise DecisionAccessRevokedError("decision requester changed")
    if context.action is DecisionAction.EXPIRE:
        if not context.principals.actor_ref.startswith("system:") or context.principals.effect_principal_ref != context.principals.requester_ref:
            raise DecisionAccessRevokedError("expiry decision principal is invalid")
    elif context.source_kind is DecisionSourceKind.CLARIFICATION and (
        context.principals.actor_ref != context.principals.requester_ref
        or context.principals.effect_principal_ref != context.principals.requester_ref
    ):
        raise DecisionAccessRevokedError("clarification principals must be the requester")
    elif context.source_kind is DecisionSourceKind.APPROVAL and (context.principals.effect_principal_ref != context.principals.actor_ref):
        raise DecisionAccessRevokedError("approval effect principal must be the decision actor")
    if context.source_kind is DecisionSourceKind.APPROVAL and context.action is DecisionAction.APPROVE:
        values = json.loads(request.source_values_json) if request.source_values_json is not None else {}
        if (
            not isinstance(values, dict)
            or not isinstance(values.get("arguments_digest"), str)
            or not values["arguments_digest"]
            or source["arguments_digest"] != values["arguments_digest"]
        ):
            raise DecisionConflictError("approval arguments digest changed")


def _validate_decision_replay_source(
    source: aiosqlite.Row | None,
    request: DecisionRecordRequest,
    *,
    row: aiosqlite.Row,
) -> None:
    context = request.context
    if context.source_kind is DecisionSourceKind.SYSTEM_CONTROL:
        if source is not None:
            raise TerminalProtocolMalformedError("system decision unexpectedly has a source row")
        return
    if source is None:
        raise TerminalProtocolMalformedError("durable decision source is missing on replay")
    if (
        source["turn_id"] != context.scope.turn_id
        or source["status"] != request.target_source_state
        or source["user_id"] != context.principals.requester_ref
        or source["workspace_id"] != row["session_workspace_id"]
    ):
        raise DecisionConflictError("durable decision source projection changed")
    source_session = source["session_id"]
    if source_session is not None and source_session != context.scope.session_id:
        raise DecisionConflictError("durable decision source Session changed")
    values = json.loads(request.source_values_json) if request.source_values_json is not None else {}
    assert isinstance(values, dict)
    if any(source[name] != value for name, value in values.items()):
        raise DecisionConflictError("durable decision source values changed")
    if context.source_kind is DecisionSourceKind.APPROVAL and source["decided_by"] != context.actor_ref:
        raise DecisionConflictError("durable approval actor changed")


def _validate_rehydrated_decision_source_projection(
    *,
    source: aiosqlite.Row | None,
    stored: aiosqlite.Row,
    context: DecisionContext,
    session_workspace_id: str,
) -> None:
    """Recompute the redacted audit digest from the authoritative source row."""

    audit = _parse_payload(stored["source_projection_json"])
    source_value_keys = audit.get("source_value_keys")
    if not isinstance(source_value_keys, list) or any(not isinstance(key, str) for key in source_value_keys):
        raise TerminalProtocolMalformedError("durable decision source audit keys are invalid")
    if context.source_kind is DecisionSourceKind.SYSTEM_CONTROL:
        if source_value_keys == []:
            # Decisions created before 0011 had no explicit denial audit field.
            # The migration backfills their finite outcome as access_revoked
            # without rewriting their immutable projection/digest identity.
            values: dict[str, object] = {}
        elif source_value_keys == ["denial_reason_code"]:
            outcome = stored["control_outcome_code"]
            if outcome not in {
                "access_revoked",
                "dependency_missing",
                "entitlement_denied",
            }:
                raise TerminalProtocolMalformedError("durable system control outcome is invalid")
            values = {"denial_reason_code": outcome}
        else:
            raise TerminalProtocolMalformedError("durable system control audit keys are invalid")
        expected_state, target_state = "pending", None
    else:
        if source is None:
            raise TerminalProtocolMalformedError("durable decision source is missing")
        if source["workspace_id"] != session_workspace_id:
            raise DecisionConflictError("durable decision source workspace changed")
        expected_state = "pending"
        target_state = {
            DecisionAction.APPROVE: "approved",
            DecisionAction.REJECT: "rejected",
            DecisionAction.RESUME: "resolved",
            DecisionAction.RESOLVE_WITHOUT_RESUME: "resolved",
            DecisionAction.DISMISS: "dismissed",
            DecisionAction.EXPIRE: "expired",
        }.get(context.action)
        if target_state is None:
            raise TerminalProtocolMalformedError("durable decision target state is invalid")
        current_source_state = str(source["status"])
        allowed_source_states = {target_state}
        if context.source_kind is DecisionSourceKind.APPROVAL and context.action is DecisionAction.APPROVE:
            allowed_source_states.update({"executed", "execution_failed"})
        if current_source_state not in allowed_source_states:
            raise DecisionConflictError("durable decision source state changed")
        if context.source_kind is DecisionSourceKind.CLARIFICATION and context.action in {
            DecisionAction.RESUME,
            DecisionAction.RESOLVE_WITHOUT_RESUME,
        }:
            authoritative_values = {
                "answers_json": source["answers_json"],
                "chosen_value": source["chosen_value"],
                "chosen_values_json": source["chosen_values_json"],
                "edited_subs_json": source["edited_subs_json"],
                "user_directive": source["user_directive"],
            }
            if any(key not in authoritative_values for key in source_value_keys):
                raise DecisionConflictError("durable decision source audit keys changed")
            values = {key: authoritative_values[key] for key in source_value_keys}
        elif context.source_kind is DecisionSourceKind.APPROVAL and context.action is DecisionAction.APPROVE:
            authoritative_values = {
                "arguments_digest": source["arguments_digest"],
            }
            if any(key not in authoritative_values for key in source_value_keys):
                raise DecisionConflictError("durable approval source audit keys changed")
            values = {key: authoritative_values[key] for key in source_value_keys}
        else:
            values = {}
    full_projection = canonical_decision_source_projection_for(
        source_kind=context.source_kind,
        action=context.action,
        expected_source_state=expected_state,
        target_source_state=target_state,
        source_values_json=canonical_json_v1(values) if values else None,
    )
    full_digest = canonical_digest_v1(full_projection)
    if (
        stored["source_projection_digest"] != full_digest
        or audit.get("source_projection_digest") != full_digest
        or audit.get("source_values_digest") != canonical_digest_v1(values)
        or audit.get("source_value_keys") != sorted(values)
    ):
        raise DecisionConflictError("durable decision source projection changed")


async def _consume_decision_source(
    connection: aiosqlite.Connection,
    *,
    source: aiosqlite.Row | None,
    request: DecisionRecordRequest,
    decided_at: str,
) -> None:
    context = request.context
    if context.source_kind is DecisionSourceKind.SYSTEM_CONTROL:
        return
    values = json.loads(request.source_values_json) if request.source_values_json is not None else {}
    assert isinstance(values, dict)
    if context.source_kind is DecisionSourceKind.CLARIFICATION:
        table = "clarification_requests"
        key = "request_id"
        allowed = {
            "answers_json",
            "chosen_value",
            "chosen_values_json",
            "user_directive",
            "edited_subs_json",
            "dismissed_at",
        }
        if context.action in {
            DecisionAction.RESUME,
            DecisionAction.RESOLVE_WITHOUT_RESUME,
        }:
            values.setdefault("chosen_at", decided_at)
            allowed.add("chosen_at")
        elif context.action is DecisionAction.DISMISS:
            values.setdefault("dismissed_at", decided_at)
    else:
        table = "approvals"
        key = "id"
        allowed = {"arguments_digest", "decided_by", "decided_at"}
        values.setdefault("decided_by", context.actor_ref)
        values.setdefault("decided_at", decided_at)
    if set(values) - allowed:
        raise ValueError("decision source values contain unsupported fields")
    assignments = ["status=?", *(f"{name}=?" for name in sorted(values))]
    params = [request.target_source_state, *(values[name] for name in sorted(values))]
    params.extend((context.source_id, request.expected_source_state))
    update = await connection.execute(
        f"UPDATE {table} SET {','.join(assignments)} WHERE {key}=? AND status=?",
        tuple(params),
    )
    if update.rowcount != 1:
        raise DecisionConflictError("decision source CAS changed")


async def _validate_pending_source_binding(
    connection: aiosqlite.Connection,
    *,
    row: aiosqlite.Row,
    request: DecisionRecordRequest,
) -> None:
    """Bind a pause receipt to the sole actionable source on that Turn."""

    context = request.context
    receipt_status = row["receipt_status"]
    if receipt_status == TerminalStatus.CLARIFICATION_PENDING.value:
        table, key = "clarification_requests", "request_id"
        expected_kind = DecisionSourceKind.CLARIFICATION
    elif receipt_status == TerminalStatus.APPROVAL_PENDING.value:
        table, key = "approvals", "id"
        expected_kind = DecisionSourceKind.APPROVAL
    else:
        raise DecisionConflictError("decision pause source kind changed")
    async with connection.execute(
        f"SELECT {key} FROM {table} WHERE turn_id=? AND status='pending' ORDER BY {key}",
        (context.scope.turn_id,),
    ) as cursor:
        pending = await cursor.fetchall()
    if len(pending) != 1:
        raise DecisionConflictError("decision pause source is not uniquely bound")
    pending_id = _required_text(
        pending[0][key],
        "decision pending source id",
    )
    if context.source_kind is not DecisionSourceKind.SYSTEM_CONTROL and (context.source_kind is not expected_kind or context.source_id != pending_id):
        raise DecisionConflictError("decision source is not the current pause")


async def _validate_resume_generation_replay(
    connection: aiosqlite.Connection,
    *,
    scope: TerminalScope,
    request: ResumeGenerationRequest,
) -> None:
    async with connection.execute(
        """
        SELECT g.decision_id,g.execution_intent_id,g.execution_intent_digest,
               g.admission_request_digest,g.execution_intent_json,
               g.execution_authz_snapshot_digest,
               g.prior_generation,g.state,g.terminal_receipt_id,
               t.current_generation,t.generation_transition_epoch,
               t.current_terminal_receipt_id,t.event_sequence_next,
               s.active_turn_id,s.active_turn_protocol_version,s.active_turn_generation,
               s.active_turn_claim_epoch
        FROM turn_generations g
        JOIN turns t ON t.id=g.turn_id
        JOIN sessions s ON s.id=t.session_id
        WHERE g.turn_id=? AND g.generation=? AND g.protocol_version=1
        """,
        (scope.turn_id, scope.generation),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or any(
        row[field] != value
        for field, value in {
            "decision_id": request.decision.context.decision_id,
            "execution_intent_id": request.execution_intent_id,
            "execution_intent_digest": request.execution_intent_digest,
            "admission_request_digest": request.decision.decision_digest,
            "execution_intent_json": canonical_json_v1(dict(request.execution_intent)),
            "prior_generation": request.decision.context.scope.generation,
        }.items()
    ):
        raise DecisionConflictError("resume generation replay identity changed")
    event_payload = {
        "decision_id": request.decision.context.decision_id,
        "generation_transition_epoch": request.expected_generation_transition_epoch + 1,
        "prior_generation": request.decision.context.scope.generation,
        "prior_receipt_id": request.decision.context.prior_receipt_id,
        "protocol": 1,
        "target_generation": scope.generation,
    }
    event_id = f"evt_{canonical_digest_v1(event_payload)[:32]}"
    async with connection.execute(
        """
        SELECT id,event_type,payload_json,payload_digest,generation,event_seq
        FROM turn_events WHERE id=? AND turn_id=? AND protocol_version=1
        """,
        (event_id, scope.turn_id),
    ) as cursor:
        marker = await cursor.fetchone()
    if (
        marker is None
        or marker["event_type"] != "turn.resumed"
        or marker["generation"] != scope.generation
        or marker["payload_json"] != canonical_json_v1(event_payload)
        or marker["payload_digest"] != canonical_digest_v1(event_payload)
        or not isinstance(marker["event_seq"], int)
        or marker["event_seq"] < 1
    ):
        raise TerminalProtocolMalformedError("resume marker projection is incomplete")
    current_generation = _positive_int(row["current_generation"], "turn.current_generation")
    expected_transition_epoch = request.expected_generation_transition_epoch + 1
    if current_generation < scope.generation or row["generation_transition_epoch"] < expected_transition_epoch:
        raise TerminalProtocolMalformedError("resume Turn pointer moved backwards")
    state = _generation_state(row["state"])
    if current_generation == scope.generation:
        if row["generation_transition_epoch"] != expected_transition_epoch:
            raise TerminalProtocolMalformedError("resume Turn transition epoch changed")
        if state is GenerationState.CLOSED:
            if row["terminal_receipt_id"] is None or row["current_terminal_receipt_id"] != row["terminal_receipt_id"]:
                raise TerminalProtocolMalformedError("closed resume generation lacks its receipt pointer")
            if row["active_turn_id"] == scope.turn_id:
                raise TerminalProtocolMalformedError("closed resume generation remains active")
        elif (
            row["current_terminal_receipt_id"] is not None
            or row["active_turn_id"] != scope.turn_id
            or row["active_turn_protocol_version"] != 1
            or row["active_turn_generation"] != scope.generation
            or not isinstance(row["active_turn_claim_epoch"], int)
            or row["active_turn_claim_epoch"] < 0
        ):
            raise TerminalProtocolMalformedError("live resume generation Session projection changed")
    elif state is not GenerationState.CLOSED or row["terminal_receipt_id"] is None:
        raise TerminalProtocolMalformedError("superseded resume generation was not closed")


async def _writer_lease_matches_identity(
    connection: aiosqlite.Connection,
    *,
    owner_instance_id: str,
    writer_lease_epoch: int,
    now: datetime,
    allowed_states: frozenset[str],
) -> bool:
    async with connection.execute(
        """
        SELECT owner_instance_id,epoch,admission_state,lease_expires_at
        FROM terminal_protocol_writer_lease WHERE lease_name=?
        """,
        (_WRITER_LEASE_NAME,),
    ) as cursor:
        row = await cursor.fetchone()
    return bool(
        row is not None
        and row["owner_instance_id"] == owner_instance_id
        and row["epoch"] == writer_lease_epoch
        and row["admission_state"] in allowed_states
        and _timestamp_after(row["lease_expires_at"], now)
    )


async def _writer_lease_matches_control_request(
    connection: aiosqlite.Connection,
    request: ControlGenerationRequest,
    now: datetime,
) -> bool:
    async with connection.execute(
        """
        SELECT owner_instance_id,epoch,admission_state,admission_epoch,lease_expires_at
        FROM terminal_protocol_writer_lease WHERE lease_name=?
        """,
        (_WRITER_LEASE_NAME,),
    ) as cursor:
        row = await cursor.fetchone()
    return bool(
        row is not None
        and row["owner_instance_id"] == request.writer_instance_id
        and row["epoch"] == request.writer_lease_epoch
        and row["admission_epoch"] == request.writer_admission_epoch
        and row["admission_state"] in {"open", "draining"}
        and _timestamp_after(row["lease_expires_at"], now)
    )


def _validate_control_outcome_mapping(
    stored: aiosqlite.Row,
    request: ControlGenerationRequest,
) -> None:
    """Bind a system revocation receipt to its immutable durable outcome."""

    context = request.decision.context
    if not (context.source_kind is DecisionSourceKind.SYSTEM_CONTROL and context.action is DecisionAction.REVOKE):
        return
    outcome = stored["control_outcome_code"]
    expected = (
        (
            TerminalStatus.FAILED,
            "terminal_dependency_missing",
            "turn.failed",
        )
        if outcome in {"dependency_missing", "entitlement_denied"}
        else (
            TerminalStatus.ABORTED,
            "access_revoked",
            "turn.access_revoked",
        )
        if outcome == "access_revoked"
        else None
    )
    if expected is None:
        raise TerminalProtocolMalformedError("system control outcome is malformed")
    if expected != (
        request.status,
        request.reason_code,
        request.message_key,
    ):
        raise DecisionConflictError("control terminal mapping conflicts with its durable outcome")


def _recovery_writer_fence_from_row(row: aiosqlite.Row) -> RecoveryWriterFence:
    if row["admission_state"] != "draining":
        raise TerminalProtocolMalformedError("recovery writer fence is not draining")
    return RecoveryWriterFence(
        owner_instance_id=_required_text(row["owner_instance_id"], "writer.owner_instance_id"),
        writer_lease_epoch=_positive_int(row["epoch"], "writer.epoch"),
        writer_admission_epoch=_positive_int(
            row["admission_epoch"],
            "writer.admission_epoch",
        ),
        rollout_config_generation=_required_text(
            row["rollout_config_generation"],
            "writer.rollout_config_generation",
        ),
        lease_expires_at=_required_text(row["lease_expires_at"], "writer.lease_expires_at"),
    )


def _terminal_writer_lease_from_row(row: aiosqlite.Row) -> TerminalWriterLease:
    return TerminalWriterLease(
        owner_instance_id=_required_text(
            row["owner_instance_id"],
            "writer.owner_instance_id",
        ),
        writer_lease_epoch=_positive_int(row["epoch"], "writer.epoch"),
        writer_admission_epoch=_positive_int(
            row["admission_epoch"],
            "writer.admission_epoch",
        ),
        rollout_config_generation=_required_text(
            row["rollout_config_generation"],
            "writer.rollout_config_generation",
        ),
        lease_expires_at=_required_text(
            row["lease_expires_at"],
            "writer.lease_expires_at",
        ),
        admission_state=_required_text(
            row["admission_state"],
            "writer.admission_state",
        ),
    )


def _control_candidate_plan(
    *,
    request: ControlGenerationRequest,
    scope: TerminalScope,
    persisted_event_highwater: int,
) -> TerminalCandidatePlan:
    identity = {
        "decision_id": request.decision.context.decision_id,
        "generation": scope.generation,
        "kind": "terminal_control",
        "protocol": 1,
        "turn_id": scope.turn_id,
    }
    suffix = canonical_digest_v1(identity)[:32]
    candidate_id = f"tc_{suffix}"
    receipt_id = f"trc_{suffix}"
    transition = StagedTerminalEvent.from_payload(
        event_id=f"evt_ctl_{suffix}",
        event_type="turn.control_committed",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.CONTROL_TRANSITION,
        payload={
            "action": request.decision.context.action.value,
            "decision_id": request.decision.context.decision_id,
            "generation": scope.generation,
            "prior_generation": request.decision.context.scope.generation,
            "protocol": 1,
            "status": request.status.value,
        },
    )
    required_events = [transition]
    if request.status is TerminalStatus.FAILED:
        required_events.append(
            StagedTerminalEvent.from_payload(
                event_id=f"evt_failed_{suffix}",
                event_type="turn_failed",
                actor_scope=ActorScope.ROOT,
                phase_scope=PhaseScope.CONTROL_TRANSITION,
                payload={
                    "decision_id": (request.decision.context.decision_id),
                    "message_key": request.message_key,
                    "reason_code": request.reason_code,
                },
            )
        )
    manifest = (
        *(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "ordinal": ordinal,
                "payload_digest": event.payload_digest,
            }
            for ordinal, event in enumerate(required_events, start=1)
        ),
    )
    variant_key = "primary_none"
    done_id = f"evt_done_{suffix}"
    done = StagedTerminalEvent.from_payload(
        event_id=done_id,
        event_type="done",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload={
            "candidate_id": candidate_id,
            "generation": scope.generation,
            "message_key": request.message_key,
            "protocol": 1,
            "reason": request.reason_code,
            "receipt_id": receipt_id,
            "required_manifest_template_digest": canonical_digest_v1(manifest),
            "selected_variant_key": variant_key,
            "status": request.status.value,
        },
    )
    variant = TerminalCandidateVariant(
        variant_key=variant_key,
        outcome_variant=CandidateOutcome.PRIMARY,
        session_variant=SessionVariant.NONE,
        status=request.status,
        reason_code=request.reason_code,
        message_key=request.message_key,
        final_answer_mode=FinalAnswerMode.NONE,
        final_answer_event_id=None,
        final_answer_digest=None,
        session_mutation_template={},
        events=(*required_events, done),
    )
    return TerminalCandidatePlan(
        scope=scope,
        candidate_id=candidate_id,
        reserved_receipt_id=receipt_id,
        reserved_terminal_event_id=done_id,
        origin=CandidateOrigin.CONTROL,
        closing_accepted_through=0,
        persisted_event_highwater=persisted_event_highwater,
        variants=(variant,),
        session_disposition_plan={
            "turn_projection": {"intent": request.decision.context.action.value},
            "variant_templates": {variant_key: {}},
        },
    )


async def _read_control_replay_receipt(
    connection: aiosqlite.Connection,
    *,
    decision: RecordedDecision,
    target_generation: int,
) -> CommittedReceipt:
    async with connection.execute(
        """
        SELECT c.candidate_id,c.candidate_projection_digest,g.decision_id,g.kind
        FROM turn_generations g
        JOIN turn_terminal_candidates c
          ON c.turn_id=g.turn_id AND c.generation=g.generation
        WHERE g.turn_id=? AND g.generation=? AND g.protocol_version=1
        """,
        (decision.context.scope.turn_id, target_generation),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row["decision_id"] != decision.context.decision_id or row["kind"] != "control":
        raise TerminalProtocolMalformedError("control replay generation is malformed")
    receipt = await _read_committed_receipt(
        connection,
        candidate_id=_required_text(row["candidate_id"], "control.candidate_id"),
        candidate_projection_digest=_required_text(
            row["candidate_projection_digest"],
            "control.candidate_projection_digest",
        ),
    )
    if receipt is None:
        raise TerminalProtocolMalformedError("control replay receipt is missing")
    return receipt


def _admission_auth_fence_scopes(
    context: AdmissionContext,
) -> tuple[AuthFenceScope, ...]:
    """Derive the fixed v1 auth vector from immutable admission identity."""

    return (
        AuthFenceScope(
            AuthFenceScopeKind.TEAM_MEMBERSHIP,
            context.team_id,
            context.team_id,
        ),
        AuthFenceScope(
            AuthFenceScopeKind.RESOURCE_GRANT,
            context.team_id,
            context.team_id,
        ),
        AuthFenceScope(
            AuthFenceScopeKind.SESSION_VISIBILITY,
            context.team_id,
            context.session_id,
        ),
        AuthFenceScope(
            AuthFenceScopeKind.USER_ACCOUNT,
            "__global__",
            context.actor,
        ),
    )


def _validate_admission_session(
    session: aiosqlite.Row | None,
    context: AdmissionContext,
    request_digest: str,
) -> TerminalAdmissionResult | None:
    if session is None:
        return _admission_rejection(context, request_digest, AdmissionCode.SESSION_NOT_FOUND)
    if session["bound_team_id"] != context.team_id or session["user_id"] != context.actor:
        return _admission_rejection(context, request_digest, AdmissionCode.SESSION_ACCESS_DENIED)
    if session["deleted_at"] is not None:
        return _admission_rejection(context, request_digest, AdmissionCode.SESSION_DELETED)
    if (
        session["recovery_lock_turn_id"] is None and (session["recovery_lock_generation"] is not None or session["recovery_lock_reason"] is not None)
    ) or (session["recovery_lock_turn_id"] is not None and (session["recovery_lock_generation"] is None or session["recovery_lock_reason"] is None)):
        raise TerminalProtocolMalformedError("session recovery lock shape is malformed")
    return None


def _admission_rejection(
    context: AdmissionContext,
    request_digest: str,
    code: AdmissionCode,
) -> TerminalAdmissionResult:
    return TerminalAdmissionResult(
        disposition=AdmissionDisposition.REJECTED,
        code=code,
        request_id=context.request_id,
        request_digest=request_digest,
    )


async def _has_prior_effect_unknown(connection: aiosqlite.Connection, session_id: str) -> bool:
    async with connection.execute(
        """
        SELECT 1
        FROM turn_terminal_deletion_ledgers l
        JOIN turns t ON t.id=l.turn_id AND t.session_id=?
        WHERE NOT EXISTS (
            SELECT 1 FROM turn_effect_retention_tombstones rt
            WHERE rt.turn_id=l.turn_id AND rt.generation=l.generation
              AND rt.effect_bundle_digest=l.effect_bundle_digest AND rt.state='aggregated'
          )
          AND (
            EXISTS (
              SELECT 1 FROM turn_effect_attempts a
              WHERE a.turn_id=l.turn_id AND a.generation=l.generation AND a.state!='settled'
            )
            OR l.unsettled_effect_count > (
              SELECT COUNT(*) FROM turn_effect_attempts a
              WHERE a.turn_id=l.turn_id AND a.generation=l.generation AND a.state='settled'
            )
          )
        LIMIT 1
        """,
        (session_id,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _has_active_session_lifecycle(
    connection: aiosqlite.Connection,
    session_id: str,
) -> bool:
    async with connection.execute(
        """
        SELECT 1 FROM session_lifecycle_operations
        WHERE session_audit_ref=?
          AND state IN (
            'prepared','fenced','materializing','processing_targets',
            'protocol_settled','content_detached','pending_delivery','blocked'
          )
        LIMIT 1
        """,
        (session_id,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _read_start_admission_replay(
    connection: aiosqlite.Connection,
    *,
    request: TerminalAdmissionRequest,
    request_digest: str,
) -> TerminalAdmissionResult | None:
    async with connection.execute(
        """
        SELECT t.id AS turn_id,t.session_id,t.terminal_protocol_version,t.user_text,
               t.terminal_ingress_surface,t.presentation_locale,
               t.message_catalog_version,
               g.generation AS intent_generation,
               g.protocol_version AS generation_protocol_version,g.kind AS generation_kind,
               w.team_id,g.execution_intent_id,
               g.execution_intent_digest,
               g.admission_request_digest,g.execution_intent_json,
               s.active_turn_id,s.active_turn_protocol_version,s.active_turn_generation,
               s.active_turn_claim_epoch
        FROM turns t
        LEFT JOIN turn_generations g ON g.turn_id=t.id AND g.generation=1
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        WHERE t.id=?
        """,
        (request.turn_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is not None:
        if row["team_id"] != request.context.team_id or row["session_id"] != request.context.session_id or row["turn_id"] != request.turn_id:
            return _admission_rejection(
                request.context,
                request_digest,
                AdmissionCode.REQUEST_CONFLICT,
            )
        if row["terminal_protocol_version"] is None:
            return _admission_rejection(
                request.context,
                request_digest,
                AdmissionCode.REQUEST_CONFLICT,
            )
        _require_v1_protocol_version(
            row["terminal_protocol_version"],
            "terminal admission Turn",
        )
        _required_text(
            row["presentation_locale"],
            "terminal admission presentation locale",
        )
        _positive_int(
            row["message_catalog_version"],
            "terminal admission message catalog version",
        )
        if row["intent_generation"] is None:
            raise TerminalProtocolMalformedError(
                "terminal admission generation is missing",
            )
        execution_intent_id = _required_text(
            row["execution_intent_id"],
            "generation.execution_intent_id",
        )
        if execution_intent_id != request.context.request_id or row["admission_request_digest"] != request_digest:
            return _admission_rejection(
                request.context,
                request_digest,
                AdmissionCode.REQUEST_CONFLICT,
            )
        _require_v1_protocol_version(
            row["generation_protocol_version"],
            "terminal admission generation",
        )
        if (
            row["terminal_ingress_surface"] != "web"
            or row["generation_kind"] != "execution"
            or _positive_int(row["intent_generation"], "generation.generation") != 1
        ):
            raise TerminalProtocolMalformedError(
                "terminal admission generation identity is malformed",
            )
        effective_intent = _parse_payload(row["execution_intent_json"])
        if not _typed_execution_context_matches(
            effective_intent,
            _required_text(
                row["presentation_locale"],
                "terminal admission presentation locale",
            ),
        ):
            raise TerminalProtocolMalformedError(
                "terminal admission execution context is malformed",
            )
        if (
            canonical_json_v1(effective_intent) != row["execution_intent_json"]
            or canonical_digest_v1(
                _canonical_execution_intent(
                    request,
                    effective_intent,
                    user_text=_required_string(row["user_text"], "turn.user_text"),
                )
            )
            != row["execution_intent_digest"]
        ):
            raise TerminalProtocolMalformedError("execution intent replay envelope changed")
        if request.user_source is not None:
            try:
                persisted_source = await _read_chat_turn_user_source_in_tx(
                    connection,
                    turn_id=request.turn_id,
                )
            except (TypeError, ValueError) as exc:
                raise TerminalProtocolMalformedError(
                    "terminal admission user source is malformed",
                ) from exc
            if persisted_source is None:
                raise TerminalProtocolMalformedError("terminal admission user source is missing")
            if canonical_json_v1(
                chat_turn_user_source_projection(persisted_source),
            ) != canonical_json_v1(
                chat_turn_user_source_projection(request.user_source),
            ):
                raise TerminalProtocolMalformedError("terminal admission user source changed")
        scope = TerminalScope(
            team_id=request.context.team_id,
            session_id=request.context.session_id,
            turn_id=str(row["turn_id"]),
            generation=_positive_int(row["intent_generation"], "generation.generation"),
        )
        active = _active_claim_projection(row)
        return TerminalAdmissionResult(
            disposition=AdmissionDisposition.ADMITTED_REPLAY,
            code=None,
            request_id=request.context.request_id,
            request_digest=request_digest,
            scope=scope,
            active_turn_id=(active["turn_id"] if active is not None else None),
            active_protocol_version=(active["protocol_version"] if active is not None else None),
            active_generation=(active["generation"] if active is not None else None),
            effective_user_text=_required_string(row["user_text"], "turn.user_text"),
            effective_execution_intent=effective_intent,
            effective_presentation_locale=_required_text(
                row["presentation_locale"],
                "turn.presentation_locale",
            ),
        )

    async with connection.execute(
        "SELECT terminal_protocol_version FROM turns WHERE id=?",
        (request.turn_id,),
    ) as cursor:
        turn_conflict = await cursor.fetchone()
    if turn_conflict is not None:
        return _admission_rejection(request.context, request_digest, AdmissionCode.REQUEST_CONFLICT)
    return None


async def _validate_session_turn_protocol_history(
    connection: aiosqlite.Connection,
    session_id: str,
) -> bool:
    async with connection.execute(
        """
        SELECT t.id AS turn_id,t.terminal_protocol_version,t.current_generation,
               g.generation,g.protocol_version AS generation_protocol_version
        FROM turns t
        LEFT JOIN turn_generations g ON g.turn_id=t.id
        WHERE t.session_id=?
        ORDER BY t.seq,g.generation
        """,
        (session_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    current_generation_seen: dict[str, bool] = {}
    has_v1_history = False
    for row in rows:
        turn_id = _required_text(row["turn_id"], "Session history Turn")
        turn_protocol = row["terminal_protocol_version"]
        generation = row["generation"]
        generation_protocol = row["generation_protocol_version"]
        if turn_protocol is None:
            if has_v1_history:
                raise TerminalProtocolMalformedError(
                    "Session protocol history moved backward from v1 to legacy",
                )
            if row["current_generation"] is not None or generation is not None or generation_protocol is not None:
                raise TerminalProtocolMalformedError(
                    "legacy Session Turn has terminal generations",
                )
            continue
        _require_v1_protocol_version(
            turn_protocol,
            "Session Turn history",
        )
        has_v1_history = True
        current_generation = _positive_int(
            row["current_generation"],
            "Session history current generation",
        )
        current_generation_seen.setdefault(turn_id, False)
        if generation is None:
            continue
        generation_number = _positive_int(
            generation,
            "Session history generation",
        )
        _require_v1_protocol_version(
            generation_protocol,
            "Session generation history",
        )
        if generation_number == current_generation:
            current_generation_seen[turn_id] = True
    if any(not seen for seen in current_generation_seen.values()):
        raise TerminalProtocolMalformedError(
            "Session Turn current generation is missing",
        )
    return has_v1_history


async def _validate_admission_active_turn(
    connection: aiosqlite.Connection,
    session: aiosqlite.Row,
) -> None:
    active = _active_claim_projection(session)
    if active is None:
        return
    active_turn_id = _required_text(active["turn_id"], "Session active Turn")
    async with connection.execute(
        """
        SELECT t.session_id,t.terminal_protocol_version,t.current_generation,
               g.protocol_version AS generation_protocol_version,
               g.claim_epoch AS generation_claim_epoch
        FROM turns t
        LEFT JOIN turn_generations g
          ON g.turn_id=t.id AND g.generation=t.current_generation
        WHERE t.id=?
        """,
        (active_turn_id,),
    ) as cursor:
        turn = await cursor.fetchone()
    if turn is None or turn["session_id"] != session["id"]:
        raise TerminalProtocolMalformedError(
            "Session active Turn identity is malformed",
        )
    active_protocol = active["protocol_version"]
    turn_protocol = turn["terminal_protocol_version"]
    if active_protocol is None:
        if turn_protocol is None:
            return
        _require_v1_protocol_version(turn_protocol, "Session active Turn")
        raise TerminalProtocolMalformedError(
            "Session active Turn protocol does not match its Session",
        )
    _require_v1_protocol_version(turn_protocol, "Session active Turn")
    current_generation = _positive_int(
        turn["current_generation"],
        "Session active Turn current generation",
    )
    if current_generation != active["generation"]:
        raise TerminalProtocolMalformedError(
            "Session active Turn generation does not match its Session",
        )
    _require_v1_protocol_version(
        turn["generation_protocol_version"],
        "Session active generation",
    )
    if turn["generation_claim_epoch"] != active["claim_epoch"]:
        raise TerminalProtocolMalformedError(
            "Session active Turn claim epoch does not match its generation",
        )


async def read_session_protocol_facts_in_tx(
    connection: aiosqlite.Connection,
    session: aiosqlite.Row,
) -> SessionProtocolFacts:
    """Build the shared Session protocol projection inside the caller's transaction."""

    has_v1_history = await _validate_session_turn_protocol_history(
        connection,
        _required_text(session["id"], "Session"),
    )
    await _validate_admission_active_turn(connection, session)
    active = _active_claim_projection(session)
    if active is None:
        return SessionProtocolFacts(has_v1_history=has_v1_history)
    if active["protocol_version"] is None:
        if has_v1_history:
            raise TerminalProtocolMalformedError(
                "v1 Session has a legacy active Turn",
            )
        return SessionProtocolFacts(
            has_v1_history=False,
            legacy_active_turn_id=str(active["turn_id"]),
        )
    if not has_v1_history:
        raise TerminalProtocolMalformedError(
            "active v1 Turn is missing from Session history",
        )
    return SessionProtocolFacts(
        has_v1_history=True,
        v1_active_claim=SessionActiveClaimSummary(
            turn_id=str(active["turn_id"]),
            protocol_version=1,
            generation=_positive_int(
                active["generation"],
                "Session active generation",
            ),
            claim_epoch=_nonnegative_int(
                active["claim_epoch"],
                "Session active claim epoch",
            ),
        ),
    )


async def _read_legacy_admission_replay(
    connection: aiosqlite.Connection,
    *,
    request: TerminalAdmissionRequest,
    request_digest: str,
) -> TerminalAdmissionResult | None:
    async with connection.execute(
        """
        SELECT t.id AS turn_id,t.session_id,t.terminal_protocol_version,t.user_text,
               g.generation AS intent_generation,w.team_id,g.execution_intent_digest,
               g.admission_request_digest,g.execution_intent_json,
               s.active_turn_id,s.active_turn_protocol_version,s.active_turn_generation,
               s.active_turn_claim_epoch
        FROM turn_generations g
        JOIN turns t ON t.id=g.turn_id
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        WHERE g.kind='execution' AND g.execution_intent_id=?
        ORDER BY t.id,g.generation
        """,
        (request.context.request_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    if len(rows) > 1:
        raise TerminalProtocolMalformedError("execution intent identity is not unique")
    if rows:
        row = rows[0]
        if (
            row["team_id"] != request.context.team_id
            or row["session_id"] != request.context.session_id
            or row["turn_id"] != request.turn_id
            or row["terminal_protocol_version"] != 1
            or row["admission_request_digest"] != request_digest
        ):
            return _admission_rejection(
                request.context,
                request_digest,
                AdmissionCode.REQUEST_CONFLICT,
            )
        effective_intent = _parse_payload(row["execution_intent_json"])
        if (
            canonical_json_v1(effective_intent) != row["execution_intent_json"]
            or canonical_digest_v1(
                _canonical_execution_intent(
                    request,
                    effective_intent,
                    user_text=_required_text(row["user_text"], "turn.user_text"),
                )
            )
            != row["execution_intent_digest"]
        ):
            raise TerminalProtocolMalformedError("execution intent replay envelope changed")
        scope = TerminalScope(
            team_id=request.context.team_id,
            session_id=request.context.session_id,
            turn_id=str(row["turn_id"]),
            generation=_positive_int(row["intent_generation"], "generation.generation"),
        )
        active_protocol_version, active_generation, _active_claim_epoch = _active_protocol_tuple(row)
        return TerminalAdmissionResult(
            disposition=AdmissionDisposition.ADMITTED_REPLAY,
            code=None,
            request_id=request.context.request_id,
            request_digest=request_digest,
            scope=scope,
            active_turn_id=row["active_turn_id"],
            active_protocol_version=active_protocol_version,
            active_generation=active_generation,
        )

    async with connection.execute(
        "SELECT terminal_protocol_version FROM turns WHERE id=?",
        (request.turn_id,),
    ) as cursor:
        turn_conflict = await cursor.fetchone()
    if turn_conflict is not None:
        return _admission_rejection(request.context, request_digest, AdmissionCode.REQUEST_CONFLICT)

    async with connection.execute(
        """
        SELECT s.id AS buffer_session_id,w.team_id,s.active_turn_id,
               s.active_turn_protocol_version,s.active_turn_generation,s.active_turn_claim_epoch,
               item.value AS item_json
        FROM sessions s
        JOIN workspaces w ON w.id=s.workspace_id
        JOIN json_each(CASE WHEN json_valid(s.steer_buffer_json) THEN s.steer_buffer_json ELSE '[]' END) item
        WHERE json_extract(item.value,'$.steer_id')=?
           OR json_extract(item.value,'$.requested_turn_id')=?
        ORDER BY s.id,item.key
        """,
        (request.context.request_id, request.turn_id),
    ) as cursor:
        buffered_rows = await cursor.fetchall()
    if not buffered_rows:
        return None
    if len(buffered_rows) > 1:
        raise TerminalProtocolMalformedError("buffered admission identity is not unique")
    session = buffered_rows[0]
    try:
        item = json.loads(session["item_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise TerminalProtocolMalformedError("v1 steer item is malformed") from exc
    if not isinstance(item, dict):
        raise TerminalProtocolMalformedError("v1 steer item is malformed")
    if (
        session["team_id"] != request.context.team_id
        or session["buffer_session_id"] != request.context.session_id
        or item.get("steer_id") != request.context.request_id
        or item.get("request_digest") != request_digest
        or item.get("requested_turn_id") != request.turn_id
    ):
        return _admission_rejection(request.context, request_digest, AdmissionCode.REQUEST_CONFLICT)
    revision = item.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise TerminalProtocolMalformedError("v1 steer revision is malformed")
    protocol_version, generation, _claim_epoch = _active_protocol_tuple(session)
    scope = None
    if protocol_version == 1:
        assert generation is not None
        scope = TerminalScope(
            team_id=request.context.team_id,
            session_id=request.context.session_id,
            turn_id=_required_text(session["active_turn_id"], "sessions.active_turn_id"),
            generation=generation,
        )
    return TerminalAdmissionResult(
        disposition=AdmissionDisposition.STEER_REPLAY,
        code=None,
        request_id=request.context.request_id,
        request_digest=request_digest,
        scope=scope,
        active_turn_id=session["active_turn_id"],
        active_protocol_version=protocol_version,
        active_generation=generation,
        steer_revision=revision,
    )


async def _writer_lease_matches(
    connection: aiosqlite.Connection,
    *,
    context: AdmissionContext,
    now: datetime,
    allowed_states: frozenset[str],
) -> bool:
    async with connection.execute(
        """
        SELECT owner_instance_id,epoch,admission_state,admission_epoch,
               rollout_config_generation,lease_expires_at
        FROM terminal_protocol_writer_lease WHERE lease_name=?
        """,
        (_WRITER_LEASE_NAME,),
    ) as cursor:
        row = await cursor.fetchone()
    return bool(
        row is not None
        and row["owner_instance_id"] == context.writer_instance_id
        and row["epoch"] == context.writer_lease_epoch
        and row["admission_state"] in allowed_states
        and row["admission_epoch"] == context.writer_admission_epoch
        and row["rollout_config_generation"] == context.rollout_config_generation
        and _timestamp_after(row["lease_expires_at"], now)
    )


async def _accept_fixed_steer_ack_in_tx(
    connection: aiosqlite.Connection,
    *,
    request: TerminalAdmissionRequest,
    scope: TerminalScope,
    accepted_now: datetime,
    accepted_at: str,
) -> tuple[AcceptedEventProposal, bool, bool]:
    """Accept the sole claimed-before-start control event allowed by R0.3."""

    if request.expected_execution_generation != scope.generation or request.turn_id != scope.turn_id or request.user_message_id is None:
        raise TerminalProtocolMalformedError(
            "terminal steer acknowledgement scope changed",
        )
    row = await _read_generation_claim_row(connection, scope)
    if row is None:
        raise TerminalProtocolMalformedError(
            "terminal steer generation is unavailable",
        )
    context = ExecutionGenerationContext(
        scope=scope,
        owner_instance_id=request.context.writer_instance_id,
        claim_epoch=_positive_int(
            row["claim_epoch"],
            "generation.claim_epoch",
        ),
        generation_row_version=_positive_int(
            row["row_version"],
            "generation.row_version",
        ),
        writer_lease_epoch=request.context.writer_lease_epoch,
        lease_expires_at=_required_text(
            row["lease_expires_at"],
            "generation.lease_expires_at",
        ),
    )
    if not _execution_context_matches(row, context, accepted_now):
        raise TerminalProtocolMalformedError(
            "terminal steer generation ownership fence changed",
        )
    if (
        row["steer_buffer_state"] != "collecting"
        or row["terminal_candidate_id"] is not None
        or row["terminal_receipt_id"] is not None
        or row["closing_accepted_through"] is not None
    ):
        raise TerminalProtocolMalformedError(
            "terminal steer generation is sealed",
        )
    if not await _writer_lease_matches(
        connection,
        context=request.context,
        now=accepted_now,
        allowed_states=frozenset({"open", "draining"}),
    ):
        raise TerminalProtocolMalformedError(
            "terminal steer writer lease is unavailable",
        )
    payload = {
        "emoji": "🤝",
        "steer_id": request.context.request_id,
        "user_msg_id": request.user_message_id,
    }
    proposal, inserted = await _insert_or_replay_event_proposal_in_tx(
        connection,
        context=context,
        generation_row=row,
        event_type="emoji_ack",
        actor_scope=ActorScope.ROOT,
        phase_scope=PhaseScope.IN_GENERATION,
        payload_json=canonical_json_v1(payload),
        payload_digest=canonical_digest_v1(payload),
        publish_mode=EventPublishMode.BEFORE_LIVE,
        root_durability=RootDurability.COMMITTED,
        idempotency_key=f"steer_ack:{request.context.request_id}",
        accepted_at=accepted_at,
    )
    return proposal, inserted, row["execution_started_at"] is not None


async def _append_or_replay_steer(
    connection: aiosqlite.Connection,
    *,
    request: TerminalAdmissionRequest,
    request_digest: str,
    intent_payload: dict[str, object],
    session: aiosqlite.Row,
    accepted_now: datetime,
    accepted_at: str,
) -> TerminalAdmissionResult:
    protocol_version, generation, claim_epoch = _active_protocol_tuple(session)
    async with connection.execute("SELECT operation_kind FROM turns WHERE id=?", (session["active_turn_id"],)) as cursor:
        active_operation = await cursor.fetchone()
    if active_operation is not None and active_operation[0] == "context_maintenance":
        return _admission_rejection(request.context, request_digest, AdmissionCode.SESSION_TURN_ACTIVE)
    if not request.allow_steer:
        return _admission_rejection(request.context, request_digest, AdmissionCode.SESSION_TURN_ACTIVE)
    exact_v1_steer = request.expected_execution_generation is not None
    buffer = _parse_steer_buffer(session["steer_buffer_json"])
    matches = [item for item in buffer if item.get("steer_id") == request.context.request_id]
    if matches:
        if len(matches) > 1:
            raise TerminalProtocolMalformedError("steer request identity is not unique")
        item = matches[0]
        if item.get("request_digest") != request_digest or item.get("requested_turn_id") != request.turn_id:
            return _admission_rejection(request.context, request_digest, AdmissionCode.REQUEST_CONFLICT)
        disposition = AdmissionDisposition.STEER_REPLAY
        revision = _positive_int(item.get("revision"), "steer.revision")
    else:
        current_revision = _nonnegative_int(session["steer_buffer_revision"], "sessions.steer_buffer_revision")
        revision = current_revision + 1
        buffer.append(
            {
                "accepted_at": accepted_at,
                "execution_intent": intent_payload,
                "protocol_version": 1,
                "request_digest": request_digest,
                "requested_turn_id": request.turn_id,
                "revision": revision,
                "steer_id": request.context.request_id,
                "text": request.user_text,
            }
        )
        update = await connection.execute(
            """
            UPDATE sessions
            SET steer_buffer_json=?,steer_buffer_revision=?
            WHERE id=? AND active_turn_id IS ? AND active_turn_protocol_version IS ?
              AND active_turn_generation IS ? AND active_turn_claim_epoch IS ?
              AND steer_buffer_state='collecting' AND steer_buffer_revision=?
              AND recovery_lock_turn_id IS NULL AND deleted_at IS NULL
            """,
            (
                canonical_json_v1(buffer),
                revision,
                request.context.session_id,
                session["active_turn_id"],
                protocol_version,
                generation,
                claim_epoch,
                current_revision,
            ),
        )
        if update.rowcount != 1:
            return _admission_rejection(request.context, request_digest, AdmissionCode.SESSION_TRANSITIONING)
        disposition = AdmissionDisposition.STEERED
    scope = None
    steer_ack_proposal = None
    execution_started = None
    if protocol_version == 1:
        assert generation is not None
        scope = TerminalScope(
            team_id=request.context.team_id,
            session_id=request.context.session_id,
            turn_id=_required_text(session["active_turn_id"], "sessions.active_turn_id"),
            generation=generation,
        )
        if exact_v1_steer:
            assert request.user_message_id is not None
            steer_ack_proposal, inserted, execution_started = await _accept_fixed_steer_ack_in_tx(
                connection,
                request=request,
                scope=scope,
                accepted_now=accepted_now,
                accepted_at=accepted_at,
            )
            expected_insert = disposition is AdmissionDisposition.STEERED
            if inserted is not expected_insert:
                raise TerminalProtocolMalformedError(
                    "terminal steer buffer and acknowledgement disagree",
                )
    return TerminalAdmissionResult(
        disposition=disposition,
        code=None,
        request_id=request.context.request_id,
        request_digest=request_digest,
        scope=scope,
        active_turn_id=session["active_turn_id"],
        active_protocol_version=protocol_version,
        active_generation=generation,
        steer_revision=revision,
        steer_ack_proposal=steer_ack_proposal,
        execution_started=execution_started,
    )


async def _insert_admitted_turn(
    connection: aiosqlite.Connection,
    *,
    request: TerminalAdmissionRequest,
    request_digest: str,
    session: aiosqlite.Row,
    effective_intent: dict[str, object],
    admitted_at: str,
) -> TerminalAdmissionResult:
    buffer = _parse_steer_buffer(session["steer_buffer_json"])
    if buffer:
        return _admission_rejection(request.context, request_digest, AdmissionCode.SESSION_TRANSITIONING)
    current_revision = _nonnegative_int(session["steer_buffer_revision"], "sessions.steer_buffer_revision")
    async with connection.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?",
        (request.context.session_id,),
    ) as cursor:
        seq_row = await cursor.fetchone()
    seq = int(seq_row[0]) if seq_row is not None else 1
    await connection.execute(
        """
        INSERT INTO turns(
          id,session_id,seq,user_text,status,started_at,
          terminal_protocol_version,terminal_ingress_surface,terminal_admission_cohort,
          protocol_admitted_at,current_generation,generation_transition_epoch,
          event_sequence_next,delivery_fence_epoch,presentation_locale,message_catalog_version
        ) VALUES (?,?,?,?, 'running',?, 1,?,?,?,1,1,1,0,?,?)
        """,
        (
            request.turn_id,
            request.context.session_id,
            seq,
            request.user_text,
            admitted_at,
            request.context.surface,
            request.context.cohort,
            admitted_at,
            request.presentation_locale,
            request.message_catalog_version,
        ),
    )
    execution_projection = _canonical_execution_intent(
        request,
        effective_intent,
        user_text=request.user_text,
    )
    execution_digest = canonical_digest_v1(execution_projection)
    await connection.execute(
        """
        INSERT INTO turn_generations(
          turn_id,generation,protocol_version,kind,execution_intent_id,
          execution_intent_digest,admission_request_digest,execution_intent_json,
          execution_authz_snapshot_digest,opened_at,state
        ) VALUES (?,1,1,'execution',?,?,?,?,?,?,'opened')
        """,
        (
            request.turn_id,
            request.context.request_id,
            execution_digest,
            request_digest,
            canonical_json_v1(effective_intent),
            request.context.auth_fence_vector_digest,
            admitted_at,
        ),
    )
    session_update = await connection.execute(
        """
        UPDATE sessions
        SET active_turn_id=?,active_turn_protocol_version=1,
            active_turn_generation=1,active_turn_claim_epoch=0,
            last_active_at=CASE WHEN last_active_at IS NULL OR last_active_at < ? THEN ? ELSE last_active_at END
        WHERE id=? AND active_turn_id IS NULL AND active_turn_protocol_version IS NULL
          AND active_turn_generation IS NULL AND active_turn_claim_epoch IS NULL
          AND recovery_lock_turn_id IS NULL AND steer_buffer_state='collecting'
          AND steer_buffer_revision=? AND deleted_at IS NULL
        """,
        (
            request.turn_id,
            admitted_at,
            admitted_at,
            request.context.session_id,
            current_revision,
        ),
    )
    if session_update.rowcount != 1:
        return _admission_rejection(request.context, request_digest, AdmissionCode.SESSION_TRANSITIONING)
    if request.user_source is not None:
        await _insert_chat_turn_user_source_in_tx(
            connection,
            source=request.user_source,
            created_at=admitted_at,
        )
    await connection.execute(
        """
        DELETE FROM user_session_archives
        WHERE session_id=? AND user_id=(SELECT user_id FROM sessions WHERE id=?)
        """,
        (request.context.session_id, request.context.session_id),
    )
    if seq == 1 and (request.user_source is None or request.user_source.context_maintenance is None):
        lines = request.user_text.strip().splitlines()
        title = (lines[0][:40] if lines else "") or "新对话"
        await connection.execute(
            """
            UPDATE sessions SET title=?
            WHERE id=? AND title_source='auto' AND (title IS NULL OR title='' OR title=id)
            """,
            (title, request.context.session_id),
        )
    scope = TerminalScope(
        team_id=request.context.team_id,
        session_id=request.context.session_id,
        turn_id=request.turn_id,
        generation=1,
    )
    return TerminalAdmissionResult(
        disposition=AdmissionDisposition.ADMITTED,
        code=None,
        request_id=request.context.request_id,
        request_digest=request_digest,
        scope=scope,
        active_turn_id=request.turn_id,
        active_protocol_version=1,
        active_generation=1,
        effective_user_text=request.user_text,
        effective_execution_intent=effective_intent,
        effective_presentation_locale=request.presentation_locale,
    )


async def _insert_deferred_admitted_turn(
    connection: aiosqlite.Connection,
    *,
    request: TerminalAdmissionRequest,
    request_digest: str,
    session: aiosqlite.Row,
    current_intent: dict[str, object],
    admitted_at: str,
) -> TerminalAdmissionResult:
    buffer = _parse_steer_buffer(session["steer_buffer_json"])
    if not buffer:
        raise TerminalProtocolMalformedError("deferred Session has an empty steer buffer")
    if any(
        session[name] is not None
        for name in (
            "steer_freeze_turn_id",
            "steer_freeze_generation",
            "steer_freeze_claim_epoch",
            "steer_frozen_revision",
        )
    ):
        raise TerminalProtocolMalformedError("deferred Session retains a steer freeze")
    revisions: list[int] = []
    intents: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    for raw in buffer:
        revision = _positive_int(raw.get("revision"), "steer.revision")
        intent = raw.get("execution_intent")
        if not isinstance(intent, dict):
            raise TerminalProtocolMalformedError("deferred execution intent is malformed")
        intent_copy = _canonical_mapping_copy(intent)
        if not isinstance(intent_copy.get("text"), str):
            raise TerminalProtocolMalformedError("deferred execution text is malformed")
        revisions.append(revision)
        intents.append(intent_copy)
        sources.append(
            {
                "request_digest": _required_text(
                    raw.get("request_digest"),
                    "steer.request_digest",
                ),
                "revision": revision,
                "steer_id": _required_text(raw.get("steer_id"), "steer.steer_id"),
            }
        )
    current_revision = _nonnegative_int(
        session["steer_buffer_revision"],
        "sessions.steer_buffer_revision",
    )
    if revisions != sorted(set(revisions)) or revisions[-1] != current_revision:
        raise TerminalProtocolMalformedError("deferred steer FIFO revision changed")
    intents.append(current_intent)
    texts = [str(intent["text"]) for intent in intents if str(intent.get("text") or "").strip()]
    merged_text = _merge_steer_text(texts)
    attachments: list[str] = []
    planning_mode: object = None
    workspace_id = current_intent.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise TerminalProtocolMalformedError("deferred current workspace is malformed")
    for intent in intents:
        if intent.get("workspace_id") != workspace_id:
            raise TerminalProtocolMalformedError("deferred execution workspace changed")
        raw_attachments = intent.get("attachments", [])
        if not isinstance(raw_attachments, list) or any(not isinstance(value, str) or not value for value in raw_attachments):
            raise TerminalProtocolMalformedError("deferred attachments are malformed")
        for value in raw_attachments:
            if value not in attachments:
                attachments.append(value)
        raw_planning = intent.get("planning_mode")
        if raw_planning is not None and not isinstance(raw_planning, (str, bool)):
            raise TerminalProtocolMalformedError("deferred planning mode is malformed")
        if raw_planning is not None:
            planning_mode = raw_planning
    effective_intent: dict[str, object] = {
        "attachments": attachments,
        "deferred_sources": sources,
        "planning_mode": planning_mode,
        "text": merged_text,
        "workspace_id": workspace_id,
    }
    execution_projection = _canonical_execution_intent(
        request,
        effective_intent,
        user_text=merged_text,
    )
    execution_digest = canonical_digest_v1(execution_projection)
    async with connection.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?",
        (request.context.session_id,),
    ) as cursor:
        seq_row = await cursor.fetchone()
    seq = int(seq_row[0]) if seq_row is not None else 1
    await connection.execute(
        """
        INSERT INTO turns(
          id,session_id,seq,user_text,status,started_at,
          terminal_protocol_version,terminal_ingress_surface,terminal_admission_cohort,
          protocol_admitted_at,current_generation,generation_transition_epoch,
          event_sequence_next,delivery_fence_epoch,presentation_locale,message_catalog_version
        ) VALUES (?,?,?,?, 'running',?, 1,?,?,?,1,1,1,0,?,?)
        """,
        (
            request.turn_id,
            request.context.session_id,
            seq,
            merged_text,
            admitted_at,
            request.context.surface,
            request.context.cohort,
            admitted_at,
            request.presentation_locale,
            request.message_catalog_version,
        ),
    )
    await connection.execute(
        """
        INSERT INTO turn_generations(
          turn_id,generation,protocol_version,kind,execution_intent_id,
          execution_intent_digest,admission_request_digest,execution_intent_json,
          execution_authz_snapshot_digest,opened_at,state
        ) VALUES (?,1,1,'execution',?,?,?,?,?,?,'opened')
        """,
        (
            request.turn_id,
            request.context.request_id,
            execution_digest,
            request_digest,
            canonical_json_v1(effective_intent),
            request.context.auth_fence_vector_digest,
            admitted_at,
        ),
    )
    update = await connection.execute(
        """
        UPDATE sessions
        SET active_turn_id=?,active_turn_protocol_version=1,
            active_turn_generation=1,active_turn_claim_epoch=0,
            steer_buffer_json='[]',steer_buffer_state='collecting',
            last_active_at=CASE WHEN last_active_at IS NULL OR last_active_at<? THEN ? ELSE last_active_at END
        WHERE id=? AND active_turn_id IS NULL AND active_turn_protocol_version IS NULL
          AND active_turn_generation IS NULL AND active_turn_claim_epoch IS NULL
          AND recovery_lock_turn_id IS NULL AND steer_buffer_state='deferred'
          AND steer_buffer_revision=? AND steer_buffer_json=? AND deleted_at IS NULL
          AND steer_freeze_turn_id IS NULL AND steer_freeze_generation IS NULL
          AND steer_freeze_claim_epoch IS NULL AND steer_frozen_revision IS NULL
        """,
        (
            request.turn_id,
            admitted_at,
            admitted_at,
            request.context.session_id,
            current_revision,
            canonical_json_v1(buffer),
        ),
    )
    if update.rowcount != 1:
        return _admission_rejection(
            request.context,
            request_digest,
            AdmissionCode.SESSION_TRANSITIONING,
        )
    await connection.execute(
        """
        DELETE FROM user_session_archives
        WHERE session_id=? AND user_id=(SELECT user_id FROM sessions WHERE id=?)
        """,
        (request.context.session_id, request.context.session_id),
    )
    scope = TerminalScope(
        team_id=request.context.team_id,
        session_id=request.context.session_id,
        turn_id=request.turn_id,
        generation=1,
    )
    return TerminalAdmissionResult(
        disposition=AdmissionDisposition.ADMITTED,
        code=None,
        request_id=request.context.request_id,
        request_digest=request_digest,
        scope=scope,
        active_turn_id=request.turn_id,
        active_protocol_version=1,
        active_generation=1,
        effective_user_text=merged_text,
        effective_execution_intent=effective_intent,
        effective_presentation_locale=request.presentation_locale,
    )


def _active_protocol_tuple(session: aiosqlite.Row) -> tuple[int | None, int | None, int | None]:
    protocol_version = session["active_turn_protocol_version"]
    generation = session["active_turn_generation"]
    claim_epoch = session["active_turn_claim_epoch"]
    if protocol_version is None:
        if generation is not None or claim_epoch is not None:
            raise TerminalProtocolMalformedError("legacy Session active tuple is malformed")
        return None, None, None
    _require_v1_protocol_version(
        protocol_version,
        "Session active tuple",
    )
    return (
        1,
        _positive_int(generation, "sessions.active_turn_generation"),
        _nonnegative_int(claim_epoch, "sessions.active_turn_claim_epoch"),
    )


def _merge_steer_text(values: list[str]) -> str:
    """Preserve FIFO message boundaries identically for every drain path."""

    return "\n\n".join(value for value in values if value.strip())


def _parse_steer_buffer(value: object) -> list[dict[str, object]]:
    if not isinstance(value, str):
        raise TerminalProtocolMalformedError("Session steer buffer is malformed")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise TerminalProtocolMalformedError("Session steer buffer is malformed") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise TerminalProtocolMalformedError("Session steer buffer is malformed")
    return parsed


async def _read_generation_claim_row(
    connection: aiosqlite.Connection,
    scope: TerminalScope,
) -> aiosqlite.Row | None:
    async with connection.execute(
        """
        SELECT g.*,t.session_id,t.current_generation,t.terminal_protocol_version,
               t.event_sequence_next,
               s.active_turn_id,s.active_turn_protocol_version,s.active_turn_generation,
               s.active_turn_claim_epoch,s.recovery_lock_turn_id,
               s.recovery_lock_generation,s.recovery_lock_reason,
               s.steer_buffer_json,s.steer_buffer_state,s.steer_buffer_revision,
               s.steer_freeze_turn_id,s.steer_freeze_generation,
               s.steer_freeze_claim_epoch,s.steer_frozen_revision,w.team_id,
               s.user_id AS session_user_id,
               s.workspace_id AS session_workspace_id
        FROM turn_generations g
        JOIN turns t ON t.id=g.turn_id
        JOIN sessions s ON s.id=t.session_id
        JOIN workspaces w ON w.id=s.workspace_id
        WHERE g.turn_id=? AND g.generation=? AND t.session_id=? AND w.team_id=?
        """,
        (scope.turn_id, scope.generation, scope.session_id, scope.team_id),
    ) as cursor:
        return await cursor.fetchone()


async def _read_context_maintenance_source(connection, *, turn_id: str):
    async with connection.execute(
        "SELECT t.operation_kind,s.context_maintenance_json FROM turns t LEFT JOIN chat_turn_user_sources s ON s.turn_id=t.id WHERE t.id=?",
        (turn_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row[0] not in (None, "context_maintenance") or (row[0] is None) != (row[1] is None):
        raise TerminalProtocolMalformedError("terminal operation source is invalid")
    if row[0] is None:
        return None
    source = await _read_chat_turn_user_source_in_tx(connection, turn_id=turn_id)
    if source is None or source.context_maintenance is None:
        raise TerminalProtocolMalformedError("terminal maintenance source is missing")
    return source.context_maintenance


async def require_live_execution_in_connection(
    connection: aiosqlite.Connection,
    *,
    context: ExecutionGenerationContext,
    now: datetime | None = None,
) -> None:
    """Check the original running capability inside a caller-owned transaction.

    This authorizes context staging only; it does not grant source access or
    finalization authority, renew either lease, or mutate lifecycle state.
    """
    if not connection.in_transaction or not isinstance(context, ExecutionGenerationContext):
        raise TypeError("Live execution validation requires a transaction and capability")
    previous_factory = connection.row_factory
    connection.row_factory = aiosqlite.Row
    try:
        observed_at = now if now is not None else datetime.now(UTC)
        row = await _read_generation_claim_row(connection, context.scope)
        if row is None or not _execution_context_matches(row, context, observed_at):
            raise TerminalProtocolMalformedError("context execution ownership changed")
        if not await _writer_lease_matches_execution(connection, context=context, now=observed_at):
            raise TerminalProtocolMalformedError("context writer lease is unavailable")
    finally:
        connection.row_factory = previous_factory


async def require_context_finalization_in_connection(
    connection: aiosqlite.Connection,
    *,
    context: PreparedFinalizationContext,
    now: datetime | None = None,
) -> None:
    """Retain the original prepared-finalization fence for context adoption."""
    if not connection.in_transaction or not isinstance(context, PreparedFinalizationContext):
        raise TypeError("Context publication requires a prepared finalization transaction")
    previous_factory = connection.row_factory
    connection.row_factory = aiosqlite.Row
    try:
        row = await _read_generation_claim_row(connection, context.finalization.scope)
        if (
            row is None
            or not _finalization_context_matches(row, context.finalization, now if now is not None else datetime.now(UTC))
            or row["terminal_candidate_id"] != context.candidate_id
            or row["terminal_receipt_id"] is not None
        ):
            raise TerminalProtocolMalformedError("context finalization ownership changed")
    finally:
        connection.row_factory = previous_factory


async def _writer_lease_matches_claim(
    connection: aiosqlite.Connection,
    *,
    context: GenerationClaimContext,
    now: datetime,
) -> bool:
    async with connection.execute(
        """
        SELECT owner_instance_id,epoch,admission_state,lease_expires_at
        FROM terminal_protocol_writer_lease WHERE lease_name=?
        """,
        (_WRITER_LEASE_NAME,),
    ) as cursor:
        row = await cursor.fetchone()
    return bool(
        row is not None
        and row["owner_instance_id"] == context.owner_instance_id
        and row["epoch"] == context.writer_lease_epoch
        and row["admission_state"] in ({"open"} if context.require_open_writer else {"open", "draining"})
        and _timestamp_after(row["lease_expires_at"], now)
    )


async def _writer_lease_matches_execution(
    connection: aiosqlite.Connection,
    *,
    context: ExecutionGenerationContext,
    now: datetime,
) -> bool:
    async with connection.execute(
        """
        SELECT owner_instance_id,epoch,admission_state,lease_expires_at
        FROM terminal_protocol_writer_lease WHERE lease_name=?
        """,
        (_WRITER_LEASE_NAME,),
    ) as cursor:
        row = await cursor.fetchone()
    return bool(
        row is not None
        and row["owner_instance_id"] == context.owner_instance_id
        and row["epoch"] == context.writer_lease_epoch
        and row["admission_state"] in {"open", "draining"}
        and _timestamp_after(row["lease_expires_at"], now)
    )


def _claim_replay(
    row: aiosqlite.Row,
    context: GenerationClaimContext,
    now: datetime,
) -> GenerationClaimResult | None:
    state = _generation_state(row["state"])
    expected_claim_epoch = context.expected_session_claim_epoch + 1
    if (
        state is not GenerationState.RUNNING
        or row["owner_instance_id"] != context.owner_instance_id
        or row["claim_epoch"] != expected_claim_epoch
        or row["writer_lease_epoch_at_claim"] != context.writer_lease_epoch
        or not _session_claim_matches(row, context, claim_epoch=expected_claim_epoch)
        or not _timestamp_after(row["lease_expires_at"], now)
    ):
        return None
    return GenerationClaimResult(
        disposition=GenerationClaimDisposition.CLAIM_REPLAY,
        code=None,
        observed_state=state,
        observed_owner_instance_id=context.owner_instance_id,
        observed_claim_epoch=expected_claim_epoch,
    )


def _claim_rejection(row: aiosqlite.Row, code: GenerationClaimCode) -> GenerationClaimResult:
    return GenerationClaimResult(
        disposition=GenerationClaimDisposition.REJECTED,
        code=code,
        observed_state=_generation_state(row["state"]),
        observed_owner_instance_id=row["owner_instance_id"],
        observed_claim_epoch=_nonnegative_int(row["claim_epoch"], "generation.claim_epoch"),
    )


def _session_claim_matches(
    row: aiosqlite.Row,
    context: GenerationClaimContext,
    *,
    claim_epoch: int | None = None,
) -> bool:
    expected_claim_epoch = context.expected_session_claim_epoch if claim_epoch is None else claim_epoch
    return bool(
        row["terminal_protocol_version"] == 1
        and row["current_generation"] == context.scope.generation
        and row["active_turn_id"] == context.scope.turn_id
        and row["active_turn_protocol_version"] == 1
        and row["active_turn_generation"] == context.scope.generation
        and row["active_turn_claim_epoch"] == expected_claim_epoch
        and row["recovery_lock_turn_id"] is None
    )


def _execution_context_matches(
    row: aiosqlite.Row,
    context: ExecutionGenerationContext,
    now: datetime,
) -> bool:
    return bool(
        row["protocol_version"] == 1
        and row["kind"] == "execution"
        and row["terminal_protocol_version"] == 1
        and row["current_generation"] == context.scope.generation
        and row["state"] == GenerationState.RUNNING
        and row["owner_instance_id"] == context.owner_instance_id
        and row["claim_epoch"] == context.claim_epoch
        and row["writer_lease_epoch_at_claim"] == context.writer_lease_epoch
        and row["active_turn_id"] == context.scope.turn_id
        and row["active_turn_protocol_version"] == 1
        and row["active_turn_generation"] == context.scope.generation
        and row["active_turn_claim_epoch"] == context.claim_epoch
        and row["recovery_lock_turn_id"] is None
        and _timestamp_after(row["lease_expires_at"], now)
    )


def _generation_state(value: object) -> GenerationState:
    try:
        return GenerationState(value)
    except (TypeError, ValueError) as exc:
        raise TerminalProtocolMalformedError("generation state is malformed") from exc


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_after(value: object, now: datetime) -> bool:
    parsed = _aware_timestamp(value)
    return parsed is not None and parsed > now


def _is_aware_timestamp(value: object) -> bool:
    return _aware_timestamp(value) is not None


def _aware_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TerminalProtocolMalformedError(f"{field} is malformed")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TerminalProtocolMalformedError(f"{field} is malformed")
    return value


def _require_v1_protocol_version(value: object, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TerminalProtocolMalformedError(f"{field} protocol is malformed")
    if value > 1:
        raise TerminalProtocolFutureVersionError(f"{field} uses a future terminal protocol")


def _summary_from_row(row: aiosqlite.Row) -> TerminalProtocolSummary:
    version = row["terminal_protocol_version"]
    if version is None:
        return TerminalProtocolSummary(version=None)
    if not isinstance(version, int) or isinstance(version, bool):
        raise TerminalProtocolMalformedError("terminal protocol version is malformed")
    if version > 1:
        raise TerminalProtocolFutureVersionError("terminal protocol future version")
    if version != 1:
        raise TerminalProtocolMalformedError("terminal protocol version is malformed")

    current_generation = _positive_int(row["current_generation"], "turns.current_generation")
    transition_epoch = _positive_int(row["generation_transition_epoch"], "turns.generation_transition_epoch")
    event_sequence_next = _positive_int(row["event_sequence_next"], "turns.event_sequence_next")
    delivery_fence_epoch = _nonnegative_int(row["delivery_fence_epoch"], "turns.delivery_fence_epoch")
    _nonempty(row["terminal_ingress_surface"], "turns.terminal_ingress_surface")
    _nonempty(row["terminal_admission_cohort"], "turns.terminal_admission_cohort")
    _nonempty(row["protocol_admitted_at"], "turns.protocol_admitted_at")
    locale = _nonempty(row["presentation_locale"], "turns.presentation_locale")
    catalog_version = _positive_int(row["message_catalog_version"], "turns.message_catalog_version")
    if row["generation_protocol_version"] != 1:
        raise TerminalProtocolMalformedError("current generation is missing or has wrong protocol")
    generation_kind = _enum_value(
        row["generation_kind"],
        "generation_kind",
        "generation.kind",
    )
    generation_closing_reason = _optional_nonempty(
        row["generation_closing_reason"],
        "generation.closing_reason",
    )
    try:
        generation_state = GenerationState(row["generation_state"])
    except (TypeError, ValueError) as exc:
        raise TerminalProtocolMalformedError("current generation state is malformed") from exc

    last_terminal_generation = row["last_terminal_generation"]
    if last_terminal_generation is not None:
        last_terminal_generation = _positive_int(last_terminal_generation, "turns.last_terminal_generation")
        if last_terminal_generation > current_generation:
            raise TerminalProtocolMalformedError("last terminal generation exceeds current generation")

    resolution = row["generation_recovery_resolution"]
    if resolution is not None:
        resolution = _enum_value(resolution, "recovery_resolution", "generation.recovery_resolution")
    receipt = _receipt_from_row(row, current_generation)
    recovery = _recovery_from_row(row)
    if generation_state is GenerationState.CLOSED:
        if resolution == "fenced_deleted":
            if receipt is not None or row["generation_receipt_id"] is not None:
                raise TerminalProtocolMalformedError("fenced-deleted generation has a receipt")
        elif receipt is None or row["generation_receipt_id"] != receipt.receipt_id:
            raise TerminalProtocolMalformedError("closed generation is missing its current receipt")
    elif receipt is not None or row["generation_receipt_id"] is not None:
        raise TerminalProtocolMalformedError("open generation carries a terminal receipt")
    if generation_state is GenerationState.RECOVERY_PENDING and recovery is None:
        raise TerminalProtocolMalformedError("recovery-pending generation has no active recovery record")
    if generation_state is not GenerationState.RECOVERY_PENDING and recovery is not None:
        raise TerminalProtocolMalformedError("active recovery record does not match generation state")

    return TerminalProtocolSummary(
        version=1,
        current_generation=current_generation,
        current_generation_kind=generation_kind,
        current_generation_closing_reason=generation_closing_reason,
        generation_transition_epoch=transition_epoch,
        current_generation_state=generation_state,
        current_generation_resolution=resolution,
        last_terminal_generation=last_terminal_generation,
        event_sequence_next=event_sequence_next,
        delivery_fence_epoch=delivery_fence_epoch,
        current_receipt=receipt,
        recovery=recovery,
        ingress_surface=row["terminal_ingress_surface"],
        admission_cohort=row["terminal_admission_cohort"],
        presentation_locale=locale,
        message_catalog_version=catalog_version,
    )


def _receipt_from_row(row: aiosqlite.Row, current_generation: int) -> TerminalReceiptSummary | None:
    receipt_id = row["receipt_id"]
    turn_receipt_id = row["current_terminal_receipt_id"]
    if receipt_id is None and turn_receipt_id is None:
        return None
    if receipt_id is None or receipt_id != turn_receipt_id:
        raise TerminalProtocolMalformedError("current receipt pointer is dangling")
    if row["receipt_turn_id"] != row["turn_id"] or row["receipt_generation"] != current_generation or row["receipt_protocol_version"] != 1:
        raise TerminalProtocolMalformedError("current receipt identity does not match the current generation")
    try:
        status = TerminalStatus(row["receipt_status"])
        final_answer_mode = FinalAnswerMode(row["receipt_final_answer_mode"])
    except (TypeError, ValueError) as exc:
        raise TerminalProtocolMalformedError("receipt status is malformed") from exc
    final_answer_event_id = _optional_nonempty(
        row["receipt_final_answer_event_id"],
        "receipt.final_answer_event_id",
    )
    if (
        not validate_final_answer_mode(
            status=status,
            mode=final_answer_mode,
            final_answer_event_id=final_answer_event_id,
            operation_kind=row["receipt_operation_kind"],
        )
        or row["receipt_operation_kind"] != row["turn_operation_kind"]
    ):
        raise TerminalProtocolMalformedError("receipt final answer mode is malformed")
    first_seq = _positive_int(row["first_event_seq"], "receipt.first_event_seq")
    last_seq = _positive_int(row["last_event_seq"], "receipt.last_event_seq")
    highwater = _nonnegative_int(row["persisted_event_highwater"], "receipt.persisted_event_highwater")
    if first_seq > last_seq or highwater > last_seq:
        raise TerminalProtocolMalformedError("receipt event range is malformed")
    return TerminalReceiptSummary(
        receipt_id=receipt_id,
        candidate_id=_nonempty(row["receipt_candidate_id"], "receipt.candidate_id"),
        generation=current_generation,
        status=status,
        reason_code=_optional_nonempty(row["receipt_reason_code"], "receipt.reason_code"),
        message_key=_nonempty(row["receipt_message_key"], "receipt.message_key"),
        final_answer_mode=final_answer_mode,
        final_answer_event_id=final_answer_event_id,
        operation_kind=row["receipt_operation_kind"],
        selected_variant_key=_nonempty(
            row["receipt_selected_variant_key"],
            "receipt.selected_variant_key",
        ),
        required_manifest_template_digest=_nonempty(
            row["receipt_required_manifest_template_digest"],
            "receipt.required_manifest_template_digest",
        ),
        terminal_event_id=_nonempty(row["terminal_event_id"], "receipt.terminal_event_id"),
        first_event_seq=first_seq,
        last_event_seq=last_seq,
        persisted_event_highwater=highwater,
        committed_at=_nonempty(row["receipt_committed_at"], "receipt.committed_at"),
    )


def _recovery_from_row(row: aiosqlite.Row) -> TerminalRecoverySummary | None:
    recovery_id = row["recovery_id"]
    if recovery_id is None:
        return None
    return TerminalRecoverySummary(
        recovery_id=_nonempty(recovery_id, "recovery.recovery_id"),
        kind=_enum_value(row["recovery_kind"], "recovery_kind", "recovery.kind"),
        phase=_nonempty(row["recovery_phase"], "recovery.phase"),
        state=_nonempty(row["recovery_state"], "recovery.state"),
        last_code=_optional_nonempty(row["recovery_last_code"], "recovery.last_code"),
        next_attempt_at=_optional_nonempty(row["recovery_next_attempt_at"], "recovery.next_attempt_at"),
    )


async def _schema_objects(connection: aiosqlite.Connection, kind: str) -> frozenset[str]:
    async with connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,)) as cursor:
        rows = await cursor.fetchall()
    return frozenset(str(row[0]) for row in rows)


async def _table_columns(connection: aiosqlite.Connection, table: str) -> frozenset[str]:
    async with connection.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return frozenset(str(row[1]) for row in rows)


def _parse_payload(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        raise TerminalProtocolMalformedError("event payload is malformed")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise TerminalProtocolMalformedError("event payload is malformed") from exc
    if not isinstance(parsed, dict):
        raise TerminalProtocolMalformedError("event payload is malformed")
    return parsed


def _decision_control_outcome_code(
    request: DecisionRecordRequest,
) -> str | None:
    if request.context.source_kind is not DecisionSourceKind.SYSTEM_CONTROL or request.context.action is not DecisionAction.REVOKE:
        return None
    if request.source_values_json is None:
        return "access_revoked"
    values = _parse_payload(request.source_values_json)
    code = values.get("denial_reason_code")
    if code not in {
        "access_revoked",
        "dependency_missing",
        "entitlement_denied",
    }:
        raise ValueError("system control outcome code is invalid")
    return str(code)


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TerminalProtocolMalformedError(f"{field} is malformed")
    return value


def _optional_nonempty(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty(value, field)


def _enum_value(value: object, enum_name: str, field: str) -> str:
    parsed = _nonempty(value, field)
    if parsed not in terminal_protocol_enum_values(enum_name):
        raise TerminalProtocolMalformedError(f"{field} is malformed")
    return parsed


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TerminalProtocolMalformedError(f"{field} is malformed")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TerminalProtocolMalformedError(f"{field} is malformed")
    return value


def _nonnegative_float(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TerminalProtocolMalformedError(f"{field} is malformed")
    parsed = float(value)
    if parsed < 0 or parsed != parsed or parsed == float("inf"):
        raise TerminalProtocolMalformedError(f"{field} is malformed")
    return parsed
