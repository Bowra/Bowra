"""Gateway 层公共 Pydantic DTO —— 所有 route 里都能用的响应模型.

原则：
    - 前端 TS 类型由此 OpenAPI 生成；字段名对齐前端 camelCase 习惯（不用 alias，
      直接 camelCase Python 名可行 —— pydantic v2 生成 OpenAPI 时按 field 名）
    - 嵌套 DTO 单独命名（*DTO 后缀），避免与业务 dataclass 撞名
    - 不放 request body 模型（每个 route 文件里局部定义，更易发现）
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
# Artifact / Attachment
# ─────────────────────────────────────────────────────────────
class ArtifactMetaDTO(BaseModel):
    """文件 artifact 元数据 —— 前端附件 chip / pinned 列表 / /files 页共用."""

    id: str
    filename: str
    mime: str
    size: int


class PinnedAttachmentListDTO(BaseModel):
    items: list[ArtifactMetaDTO] = Field(default_factory=list)


class PinAttachmentResultDTO(BaseModel):
    ok: bool
    artifact_id: str


# ─────────────────────────────────────────────────────────────
# Session / Turn
# ─────────────────────────────────────────────────────────────
class SessionDTO(BaseModel):
    id: str
    workspace_id: str
    title: str | None = None
    started_at: str
    last_active_at: str
    status: str
    execution_generation: int = 1
    cost_usd: float = 0.0
    tokens_total: int = 0
    intents: list[str] = Field(default_factory=list)


class TurnAttachmentDTO(BaseModel):
    file_id: str
    filename: str
    mime: str
    size: int
    reference_kind: Literal["result"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    selected_context: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None)
    bundle_artifact_ids: list[str] | None = None


class TraceStepDTO(BaseModel):
    """chat-hydrate-overhaul: 一条 trace step. 字段对齐 FE TraceStep."""

    id: str  # FE TraceStep.id (eg "classify" / "tool_xxx")
    kind: str  # "running" | "success" | "error" | "pending"
    title: str | None = None
    tool_name: str | None = None
    duration_ms: int | None = None
    summary: str | None = None  # ≤1KB result preview in the FE collapsed area.
    # Round 5: subagent_id links to the spawn parent. None = top-level (direct Lead call).
    # FE uses this to render nested calls under the spawn node, rather than as siblings.
    subagent_id: str | None = None


class TurnSummaryDTO(BaseModel):
    """chat-hydrate-overhaul: turn-end summary. 对齐 FE TurnSummary."""

    total_ms: int
    cost_text: str | None = None
    tools_count: int
    llm_count: int
    tokens_input: int | None = None
    tokens_output: int | None = None
    usage_partial: bool = False


class TurnDTO(BaseModel):
    id: str
    seq: int
    user_text: str
    intent: str | None = None
    status: str
    execution_generation: int = 1
    terminal_protocol_version: int | None = None
    # 0708 ⑩: failure cause (e.g. "interrupted" for a gateway-restart-killed turn) so
    # the FE can surface a targeted retry affordance on the archived turn.
    error_kind: str | None = None
    started_at: str
    ended_at: str | None = None
    final_answer: str | None = None
    attachments: list[TurnAttachmentDTO] = Field(default_factory=list)
    # B-4: agent owning this turn (Lead/Analyst/Modeler/Thinker), default Lead.
    # turn_runner records the actual value when spawning a subagent; return default "Lead" for now
    # until turn_runner persists it (turns.agent_type column). Frontend W2-3 is ready.
    agent_type: str | None = None
    # chat-hydrate-overhaul: restore IntentStrip + step_card after refresh. Old turn without a row -> empty list.
    trace_steps: list[TraceStepDTO] = Field(default_factory=list)
    summary: TurnSummaryDTO | None = None


class ChatExecutionBootstrapDTO(BaseModel):
    """Advisory protocol for the next explicit write; POST revalidates it."""

    write_protocol: Literal[
        "terminal_v1",
        "legacy_v0",
        "refresh_required",
        "unavailable",
    ]
    config_generation: str
    readiness_generation: str
    attachment_refs_wire_version: Literal[1] | None = None
    active_turn_id: str | None = None
    active_execution_generation: int | None = None


class SessionDetailDTO(BaseModel):
    session: SessionDTO
    turns: list[TurnDTO]
    chat_execution: ChatExecutionBootstrapDTO


class SessionListItemDTO(BaseModel):
    id: str
    title: str
    intents: list[str]
    time: str
    # Last activity as a timezone-aware ISO 8601 instant. ``time`` is a pre-rendered English relative string
    # ("1d ago") kept for older clients; current clients format this value with the shared calendar-day list rule
    # ("Yesterday") in the viewer's time zone.
    updated_at: str | None = None
    state: str  # "active" | "done" | "error" | "pending"
    # B-2: session labels, all optional; backfill after real backend signals reach the sessions table.
    # Default None gracefully omits the frontend (W3-1) badge.
    unread: bool | None = None
    pinned: bool | None = None
    archived: bool | None = None
    skill_applied: str | None = None


class SessionGroupDTO(BaseModel):
    id: str  # "today" / "yest" / "last7" / "older"
    label: str
    items: list[SessionListItemDTO]


class SessionListDTO(BaseModel):
    groups: list[SessionGroupDTO]
    archived_count: int | None = None
    # Opaque keyset cursor for the next page (null on the last page); ``has_more`` mirrors it.
    # Cursor pages reuse the group ids; clients merge items by group id and dedupe by session id.
    next_cursor: str | None = None
    has_more: bool = False


# ─────────────────────────────────────────────────────────────
# File upload / management
# ─────────────────────────────────────────────────────────────


class FileUploadDTO(BaseModel):
    file_id: str
    filename: str
    size: int
    mime: str
    url: str


class FileUsageEntryDTO(BaseModel):
    session_id: str
    session_title: str | None = None
    turn_id: str
    turn_seq: int
    user_text: str
    used_at: str


class FileUsagesDTO(BaseModel):
    # file-management-v2 P0-8: field must be usages; frontend fetchFileUsages consumes
    # r.usages (api.ts). Sending items made the drawer's usage section undefined from launch.
    usages: list[FileUsageEntryDTO] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Generic
# ─────────────────────────────────────────────────────────────
class OkDTO(BaseModel):
    ok: bool = True


class DeletedDTO(BaseModel):
    deleted: str


# ─────────────────────────────────────────────────────────────
# Memory
# ─────────────────────────────────────────────────────────────
class MemoryEntryDTO(BaseModel):
    id: str
    kind: str
    key: str
    value: Any
    locked: bool = False
    lock_reason: str | None = None
    source: str = ""
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class MemoryListDTO(BaseModel):
    user_id: str
    entries: list[MemoryEntryDTO] = Field(default_factory=list)


class MemoryUpsertResultDTO(BaseModel):
    id: str
    kind: str
    key: str
    value: Any


# ─── V3 PRD-B unified memory page DTOs ────────────────────


class UnifiedMemoryItemDTO(BaseModel):
    """A personal memory item used by /memory/snapshot."""

    id: str
    origin: str
    kind: str  # preference|constraint|datarc_binding
    key: str
    value: Any
    locked: bool = False
    lock_reason: str | None = None
    source: str = ""
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    workspace_id: str | None = None
    scope: str | None = None
    language: str | None = None


class MemoryStatsDTO(BaseModel):
    active_count: int = 0
    trash_count: int = 0
    pending_candidates: int = 0


class DreamingStateDTO(BaseModel):
    paused: bool = False


class MemorySnapshotDTO(BaseModel):
    user_id: str
    items: list[UnifiedMemoryItemDTO] = Field(default_factory=list)
    stats: MemoryStatsDTO = Field(default_factory=MemoryStatsDTO)
    dreaming: DreamingStateDTO = Field(default_factory=DreamingStateDTO)
    notes: MemoryNoteDTO | None = None  # V3.5 PRD-C; V3 client ignores


class MemoryCapabilityActionsDTO(BaseModel):
    edit: bool = False
    permanent_delete: bool = False
    recent_resets: bool = False
    destructive_operations: bool = False


class MemoryCapabilitiesDTO(BaseModel):
    v2_enabled: bool
    v2_ready: bool
    presentation: str
    schema_version: int = 3
    kind_policy_version: int = 1
    actions: MemoryCapabilityActionsDTO = Field(
        default_factory=MemoryCapabilityActionsDTO,
    )
    permanent_delete_ready: bool = False
    permanent_delete_reason: str | None = None
    reason: str | None = None


class CanonicalMemoryScopeDTO(BaseModel):
    type: str
    space_id: str | None = None
    space_name: str | None = None
    availability: str = "available"
    label: str


class CanonicalMemoryDisplaySchemaDTO(BaseModel):
    name: str
    payload: dict[str, Any]
    editable_fields: list[str] = Field(default_factory=list)


class CanonicalMemoryCreatedFromDTO(BaseModel):
    type: str
    source_label: str
    # Stable key for the source line (legacy_migration, auto_captured, confirmed_suggestion, told_in_chat,
    # system_maintained, unavailable); clients localize it instead of showing `source_label`.
    source_label_key: str | None = None
    source_available: bool = False
    source_open_token: str | None = None


class CanonicalMemoryLastModifiedDTO(BaseModel):
    actor_type: str | None = None
    actor_id: str | None = None
    modified_at: str | None = None


class CanonicalMemoryProvenanceDTO(BaseModel):
    created_from: CanonicalMemoryCreatedFromDTO
    last_modified: CanonicalMemoryLastModifiedDTO


class CanonicalMemoryItemCapabilitiesDTO(BaseModel):
    edit: bool = False
    soft_delete: bool = False
    restore: bool = False
    permanent_delete: bool = False


class CanonicalMemorySupersededByDTO(BaseModel):
    """`memory_entries.superseded_by` 的**已解析**形态（记忆重做 · 条目详情面）。

    库里存的是 id，界面上要说的是「已被『ranking_scope』取代」⇒ 这里带上 key。
    `key=None` 表示取代方已被硬删、解析不出来；前端此时走通用兜底文案，
    **不要**把 `id` 显示给用户（`mem_1bf370c9c86a` 对用户没有任何意义）。
    """

    id: str
    key: str | None = None


class CanonicalMemoryItemDTO(BaseModel):
    id: str
    resource_type: str = "memory_entry"
    display_type: str
    display_value: str
    display_schema: CanonicalMemoryDisplaySchemaDTO
    scope: CanonicalMemoryScopeDTO
    provenance: CanonicalMemoryProvenanceDTO
    capabilities: CanonicalMemoryItemCapabilitiesDTO
    locked: bool = False
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    purge_at: str | None = None
    version: str
    # Per-entry metadata from memory-redesign batches 0/1. Batch 0 persisted columns without any UI reader;
    # these fields provide that read path (memory-item-detail-design.md §六张画面).
    # ⚠ All nullable: only rows written through `commit_memory_write` have confidence / memory_type values.
    # Existing/pre-migration writes are NULL; frontend renders them unlabeled (screen G). **Never** fill backend defaults:
    # presenting an unassessed value as fact is worse than leaving it empty.
    confidence: str | None = None
    memory_type: str | None = None
    previous_value: str | None = None
    previous_value_at: str | None = None
    superseded_by: CanonicalMemorySupersededByDTO | None = None


class CanonicalMemoryItemsPageDTO(BaseModel):
    items: list[CanonicalMemoryItemDTO] = Field(default_factory=list)
    next_cursor: str | None = None


class MemoryAboutYouV2DTO(BaseModel):
    content: str = ""
    scope: CanonicalMemoryScopeDTO
    version: str = "0"
    updated_at: str | None = None
    is_locked: bool = False
    capabilities: CanonicalMemoryItemCapabilitiesDTO


class MemoryAboutYouEditSessionDTO(BaseModel):
    edit_token: str
    version: str
    expires_at: str


class MemoryAboutYouDeleteDTO(BaseModel):
    deleted: str = "about_you"
    recovery_id: str
    purge_at: str
    version: str


class MemoryDestructiveOperationDTO(BaseModel):
    operation_id: str
    idempotency_key: str
    action: str = "permanent_delete_all"
    target_id: str | None = None
    status: str
    phase: str
    cutoff_at: str
    privacy_epoch: int
    progress_current: int = 0
    progress_total: int = 0
    retryable: bool = False
    last_error_code: str | None = None
    completed_at: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    skipped_locked: int = 0
    dreaming_paused: bool = False
    partial_failures: list[dict[str, Any]] = Field(default_factory=list)
    logical_scrub_completed: bool = False
    physical_purge_completed: bool = False
    deletion_receipt_committed: bool = False


class MemoryOverviewStatsDTO(BaseModel):
    notes_configured: bool = False
    saved_entries: int = 0
    pending_candidates: int = 0
    spaces_represented: int = 0
    trash_count: int = 0
    recoverable_resets: int = 0


class MemoryLearningV2DTO(BaseModel):
    automatic_learning: str = "active"


class MemoryOverviewV2DTO(BaseModel):
    schema_version: int = 2
    about_you: MemoryAboutYouV2DTO
    suggestions: list[MemoryCandidateDTO] = Field(default_factory=list)
    stats: MemoryOverviewStatsDTO = Field(
        default_factory=MemoryOverviewStatsDTO,
    )
    learning: MemoryLearningV2DTO = Field(
        default_factory=MemoryLearningV2DTO,
    )
    capabilities: MemoryCapabilitiesDTO


class TrashListDTO(BaseModel):
    user_id: str
    items: list[UnifiedMemoryItemDTO] = Field(default_factory=list)


class RestoreResultDTO(BaseModel):
    id: str
    restored: bool


class ResetMemoryResultDTO(BaseModel):
    snapshot_id: str
    affected_memory: int = 0
    affected_notes: int = 0  # V3.5 PRD-C
    counts: dict[str, int] = Field(default_factory=dict)
    expires_at: str | None = None
    undo_available: bool = True


class MemoryResetPreviewDTO(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)


class MemoryResetSummaryDTO(BaseModel):
    id: str
    created_at: str
    expires_at: str
    counts: dict[str, int] = Field(default_factory=dict)
    state_counts: dict[str, int] = Field(default_factory=dict)
    remaining_recoverable: int
    recoverable: bool
    reason: str | None = None


class MemoryResetListDTO(BaseModel):
    items: list[MemoryResetSummaryDTO] = Field(default_factory=list)
    next_cursor: str | None = None


class MemoryResetRecoveryItemDTO(BaseModel):
    resource_type: str
    resource_id: str
    display_type: str | None = None
    restore_state: str
    reason: str | None = None
    governance_target: str | None = None
    can_retry: bool = False


class MemoryResetDetailDTO(BaseModel):
    id: str
    created_at: str
    expires_at: str
    state_counts: dict[str, int] = Field(default_factory=dict)
    remaining_recoverable: int
    items: list[MemoryResetRecoveryItemDTO] = Field(default_factory=list)


# ─── V3.5 PRD-C memory_notes DTOs ─────────────────────────


class BulletAnchorDTO(BaseModel):
    """V4 Sprint 2 PRD-D-4-1: per-bullet verification anchor."""

    bullet_text: str
    canvas_id: str
    section_id: str
    verifier_result_hash: str
    saved_at: str
    status: str  # "valid" | "stale" | "unknown"
    invalidated_at: str | None = None
    invalidation_reason: str | None = None


class MemoryNoteDTO(BaseModel):
    user_id: str
    version: str = "0"
    content: str = ""
    char_count: int = 0
    # memory-revive S1: deliver the budget cap in the DTO (FE full-store warning; avoid hardcoded drift).
    char_limit: int = 3000
    pinned_count: int = 0
    bullet_count: int = 0
    free_text_lines: int = 0
    last_edited_at: str | None = None
    last_edited_source: str = ""
    is_locked: bool = False
    lock_expires_at: str | None = None
    # V4 Sprint 2 PRD-D-4-1: per-bullet anchors (sidecar table).
    bullet_anchors: list[BulletAnchorDTO] = Field(default_factory=list)
    # V4 Sprint 1 row-level fields, DEPRECATED (Sprint 2 moved to sidecar). Retain schema
    # for compatibility (Sprint 1 dev clients), always NULL/'valid'. Remove in V5+.
    verification_anchor: dict | None = None
    verification_status: str = "valid"  # "valid" | "stale" | "unknown"


class NotesPatchRequest(BaseModel):
    content: str = Field(max_length=3000)
    version: str = Field(pattern=r"^[0-9]+$")
    edit_token: str = Field(min_length=32, max_length=256)
    ignore_lock: bool = False


class NoteAppendRequest(BaseModel):
    bullet: str = Field(min_length=1, max_length=500)
    pinned: bool = False
    source: str = "user_explicit"
    source_turn_id: str | None = None
    ignore_lock: bool = False


class NotesLockResultDTO(BaseModel):
    locked_until: str
    edit_token: str
    version: str


class MemoryProposalCardDTO(BaseModel):
    """SSE event payload for memory.proposal.suggested."""

    candidate_id: str
    kind: str
    content: str
    confidence: float
    recommended_target: str  # "structured" | "notes"


class UndoResetResultDTO(BaseModel):
    snapshot_id: str
    restored_memory: int = 0
    restored_notes: int = 0  # V3.5 PRD-C
    status: str = "restored"
    restored_counts: dict[str, int] = Field(default_factory=dict)
    conflicts: list[dict[str, str]] = Field(default_factory=list)
    unavailable_counts: dict[str, int] = Field(default_factory=dict)
    remaining_recoverable: int = 0


# ─────────────────────────────────────────────────────────────
# Datarc passthrough
# ─────────────────────────────────────────────────────────────
class AuthorizeResponseDTO(BaseModel):
    authorized_url: str
    strategy: str
    identity: str | None = None


class GuideQuestionsDTO(BaseModel):
    workspace_id: str
    items: list[str] = Field(default_factory=list)
    error: str | None = None


# ─────────────────────────────────────────────────────────────
# Approval
# ─────────────────────────────────────────────────────────────
class ApprovalDTO(BaseModel):
    id: str
    turnId: str
    sessionId: str | None = None
    workspaceId: str | None = None
    memberUid: str | None = None
    userId: str | None = None
    toolName: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    riskLevel: str | None = None
    status: str
    decidedBy: str | None = None
    decidedAt: str | None = None
    executionOk: bool | None = None
    executionResult: Any = None
    executionError: str | None = None
    executedAt: str | None = None
    createdAt: str
    updatedAt: str
    decisionId: str | None = None
    decisionState: str | None = None


class ApprovalListDTO(BaseModel):
    items: list[ApprovalDTO] = Field(default_factory=list)
    scope: str = "pending"


# ─────────────────────────────────────────────────────────────
# Notification Center
# ─────────────────────────────────────────────────────────────
class NotificationActionDTO(BaseModel):
    id: str
    label: str
    kind: str = "link"
    url: str | None = None


class NotificationItemDTO(BaseModel):
    id: str
    teamId: str
    category: str
    type: str
    priority: str
    title: str
    body: str | None = None
    subjectType: str | None = None
    subjectId: str | None = None
    actorUserId: str | None = None
    actionUrl: str | None = None
    actions: list[NotificationActionDTO] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    state: str
    recipientRole: str | None = None
    deliveredAt: str
    readAt: str | None = None
    archivedAt: str | None = None
    createdAt: str
    updatedAt: str


class NotificationListDTO(BaseModel):
    items: list[NotificationItemDTO] = Field(default_factory=list)
    nextCursor: str | None = None


class NotificationCountDTO(BaseModel):
    actionRequired: int
    updatesUnread: int
    totalBadge: int


# ─────────────────────────────────────────────────────────────
# Memory candidate (Dreaming Light)
# ─────────────────────────────────────────────────────────────
class MemoryCandidateScopeDTO(BaseModel):
    type: str
    space_id: str | None = None
    space_name: str | None = None
    availability: str = "available"
    label: str
    requires_confirmation: bool = False


class MemoryCandidateSourceTurnDTO(BaseModel):
    availability: str = "unavailable"
    label: str
    source_open_token: str | None = None


class MemoryCandidateCapabilitiesDTO(BaseModel):
    approve: bool = True
    reject: bool = True
    reason: str | None = None


class MemoryCandidateSupersedesItemDTO(BaseModel):
    """候选保存后会取代的那条现有记忆（记忆重做 · 画面 D）。

    `GovernanceCandidate.supersedes` 里是一串 id；要画出「保存后会取代 N 条现有记忆」
    这张卡，用户得看见被取代的是**哪一条、现在写着什么**，所以这里带 key + 当前值。
    """

    id: str
    key: str | None = None
    current_value: str | None = None


class MemoryCandidateDTO(BaseModel):
    id: str
    userId: str
    kind: str | None = None
    key: str | None = None
    value: Any = None
    reason: str | None = None
    sourceTurnId: str | None = None
    status: str = "pending"
    decidedBy: str | None = None
    decidedAt: str | None = None
    createdAt: str
    updatedAt: str
    write_error: str | None = None  # Only on failed user_memory upsert.
    source_space: MemoryCandidateScopeDTO | None = None
    source_turn: MemoryCandidateSourceTurnDTO | None = None
    default_target: MemoryCandidateScopeDTO | None = None
    allowed_targets: list[MemoryCandidateScopeDTO] = Field(default_factory=list)
    selected_target: MemoryCandidateScopeDTO | None = None
    scope_conflict: dict[str, Any] | None = None
    display_value: str | None = None
    display_reason: str | None = None
    expires_at: str | None = None
    #: Candidate pool: `dreaming_light` (background distillation) / `feedback_draft` (drafting a user's request).
    #: ★ Frontend uses this to decide whether editing is enabled; redrafting makes sense only for drafts
    #: (distilled candidates are not rewrites of a user's statement and have no original statement to rewrite).
    #: Backend returns 400 for nondrafts. Without this field, every card gets a button
    #: that always fails; all 92 production candidates were dreaming_light.
    origin: str = "dreaming_light"
    #: ID of the memory written after approval. ★ Storage has always had `approved_resource_id`,
    #: but the DTO **omitted it**, leaving the frontend no target for the chat card's undo action.
    #: One-click undo is the prerequisite for automatic recording; without this target, the receipt is an empty promise.
    approved_entry_id: str | None = None
    canonical_state: str = "pending"
    capabilities: MemoryCandidateCapabilitiesDTO = Field(
        default_factory=MemoryCandidateCapabilitiesDTO,
    )
    version: str = "1"
    # Memory-redesign batch 1 review added supersedes to GovernanceCandidate, but DTO omitted it, preventing
    # frontend screen D. Empty list = this suggestion replaces nothing; omit the card.
    supersedes: list[MemoryCandidateSupersedesItemDTO] = Field(default_factory=list)


class MemoryCandidateListDTO(BaseModel):
    items: list[MemoryCandidateDTO] = Field(default_factory=list)
    scope: str = "pending"


class TurnMemoryExplicitRequestDTO(BaseModel):
    """The most specific structured "explicitly asked to remember" fact recorded for a turn.

    `reason` is a finite code from the receipt allowlist, or null for the generic
    "not remembered" copy. The frontend owns the wording.
    """

    source: Literal["remember_tool", "capture", "language_setting", "nl_intent"]
    outcome: Literal["remembered", "candidate", "not_remembered"]
    reason: str | None = None


class TurnMemoryReceiptEntryDTO(BaseModel):
    """A memory entry this turn created. `state` is derived from persisted trash state."""

    id: str
    kind: str
    key: str
    display_value: str | None = None
    origin: Literal["remember_tool", "capture", "candidate"]
    state: Literal["remembered", "undone"]
    undoable: bool
    #: Set for entries approved from a memory candidate, so the candidate card can show its own receipt.
    candidate_id: str | None = None


class TurnMemoryReceiptDTO(BaseModel):
    """`GET /memory/turn-receipt?turn_id=…`: the per-turn memory receipt (REST projection, no turn event)."""

    schema_version: Literal[1] = 1
    turn_id: str
    explicit_request: TurnMemoryExplicitRequestDTO | None = None
    pending_candidates: list[MemoryCandidateDTO] = Field(default_factory=list)
    entries: list[TurnMemoryReceiptEntryDTO] = Field(default_factory=list)


MemoryOverviewV2DTO.model_rebuild()


# ─────────────────────────────────────────────────────────────
# Turn events / replay
# ─────────────────────────────────────────────────────────────
class TurnEventDTO(BaseModel):
    seq: int
    event_type: str
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = None
    execution_generation: int | None = None
    terminal_receipt_id: str | None = None
    terminal_current: bool | None = None


class TurnEventsListDTO(BaseModel):
    turn_id: str
    events: list[TurnEventDTO] = Field(default_factory=list)
    terminal_protocol_version: int | None = None
    current_generation: int | None = None
    persistent_event_highwater: int | None = None
    current_effect_disclosure: dict[str, Any] | None = None


class TurnIdsDTO(BaseModel):
    turn_ids: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Canvas
# Canvas sections are heterogeneous (kpi_row / data_table / sql_block /
# trend_chart / attribution / narrative / eyebrow / h1 / h2 / meta / section /
# sources / datarc_iframe / pycell), with different payload shapes. Strict Union +
# discriminator needs about 200 lines of types and changes compose_answer's output contract. Use
# permissive `list[dict[str, Any]]` for now, tightening after the canvas model stabilizes.
# ─────────────────────────────────────────────────────────────
class CanvasMetaDTO(BaseModel):
    id: str
    sessionId: str
    workspaceId: str
    title: str | None = None
    status: str = "active"  # R2 stored state: active | verified | stale | archived.
    # R2 display state: archived takes precedence; quarantined sections imply stale. Used by the FE report-header chip.
    effectiveStatus: str = "active"
    quarantinedCount: int = 0
    createdAt: str
    updatedAt: str


class CanvasSurfaceEntryDTO(BaseModel):
    sectionId: str
    sectionRev: int
    order: int
    placement: str
    parentSectionId: str | None = None
    effectiveKind: str
    parityRequirement: str
    visualInputSha256: str | None = None


class CanvasSurfaceExclusionDTO(BaseModel):
    sectionId: str
    reason: str


class CanvasSurfaceProjectionDTO(BaseModel):
    contractVersion: int
    contentProjectionToken: str
    surfaceToken: str
    entries: list[CanvasSurfaceEntryDTO] = Field(default_factory=list)
    exclusions: list[CanvasSurfaceExclusionDTO] = Field(default_factory=list)


class CanvasExportV2CapabilityDTO(BaseModel):
    format: Literal["pdf", "pptx", "xlsx", "docx"]
    profile: str


class CanvasDetailDTO(BaseModel):
    """canvas 读接口的响应 —— canvas 元数据 + 全部 sections（宽松 dict 透传）."""

    canvas: CanvasMetaDTO | None = None
    sections: list[dict[str, Any]] = Field(default_factory=list)
    # B-1: full SourceRef metadata (id/tool/query/sql/artifact_id/turn_id/tool_call_id).
    # Actual aggregation collects tool_invocations across all turns in the session (implementation follow-up).
    # Currently returns an empty array; frontend (W1-9) popover falls back to an unconfirmed-source label.
    sourceRefs: list[dict[str, Any]] = Field(default_factory=list)
    # Canvas export reliability W1: persisted content/Surface identity shared by page and export.
    projectionToken: str | None = None
    surfaceProjection: CanvasSurfaceProjectionDTO | None = None
    surfaceToken: str | None = None
    exportV2Capabilities: list[CanvasExportV2CapabilityDTO] = Field(default_factory=list)
    # A3 (report-edit-audit, authoritative seq): true = stored seq is authoritative display order; FE renders by seq
    # to retain user drag order. false = old data/flag off; FE retains preprocessSections' forced-rank fallback.
    # Use an explicit field rather than the frontend heuristic that a header not leading means unnormalized:
    # it would misclassify an intentionally unusual header position and reorder it (design review F1).
    seqAuthoritative: bool = False
    # source-accessibility §1.6 consumer half c1: turn_id -> viewer-safe citation rows
    # ({seq, kind, access_state, safe_label?, safe_anchor_id?}). citation-plugin resolves body [cite:N] against
    # this table using the paragraph's turn_id. Report-library/shared views lack turn events, so
    # detail must carry resolution data. citation_presentation alone maps rows (same shape as event projections).
    # review F5: None = not fetched/gate closed (FE reconciliation retains old value); {} = truly no citations
    # (FE clears them). These meanings must remain distinct; default None.
    turnCitations: dict[str, list[dict[str, Any]]] | None = None


class CanvasRevisionDTO(BaseModel):
    id: str
    canvasId: str
    sectionId: str | None = None
    op: str  # append | patch | reorder | delete
    prevPayload: dict[str, Any] | None = None
    newPayload: dict[str, Any] | None = None
    byTurnId: str | None = None
    createdAt: str
    # Option A (report-edit-audit): True when a delete revision carries a recovery envelope;
    # drawer shows Restore on the host row. Old-format deletion records always False.
    restorable: bool = False
    # Appearance refresh batch 4b: this edit's AI-turn ordinal across the entire report (1-based, unaffected by the 100-row window);
    # manual edits / machine organization use null. Drawer displays the AI-update turn number.
    turnOrdinal: int | None = None


class CanvasRevisionsDTO(BaseModel):
    items: list[CanvasRevisionDTO] = Field(default_factory=list)


class CanvasRestoreResultDTO(BaseModel):
    restoredSectionIds: list[str] = Field(default_factory=list)


class ExportJobDTO(BaseModel):
    jobId: str
    canvasId: str
    contractVersion: int = 1
    fmt: str  # pdf | pptx | xlsx
    status: str  # queued | running | done | failed
    url: str | None = None
    artifactId: str | None = None
    filename: str | None = None
    size: int | None = None
    error: str | None = None
    createdAt: str
    updatedAt: str


class ExportJobV2DTO(BaseModel):
    """Durable Canvas export contract; V2 intentionally uses snake_case."""

    job_id: str
    canvas_id: str
    contract_version: Literal[2, 3] = 2
    format: Literal["pdf", "pptx", "xlsx"]
    profile: str
    parity_contract: str
    surface_token: str
    status: Literal["queued", "running", "done", "failed", "canceled"]
    phase: str
    quality_status: Literal["pending", "clean", "warnings", "rejected"]
    progress: dict[str, Any] | None = None
    warning_count: int = 0
    snapshot_id: str
    artifact_id: str | None = None
    manifest_artifact_id: str | None = None
    download_url: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None


class ExportJobsV2ListDTO(BaseModel):
    """Migration history may contain legacy V1 and durable V2 rows."""

    items: list[ExportJobV2DTO | ExportJobDTO] = Field(default_factory=list)


class CanvasSectionPatchResultDTO(BaseModel):
    section: dict[str, Any]
    rev: int


class CanvasOrderResultDTO(BaseModel):
    ok: bool = True
    order: list[str] = Field(default_factory=list)


class CanvasTitleResultDTO(BaseModel):
    ok: bool = True
    title: str


class CanvasStatusResultDTO(BaseModel):
    ok: bool = True
    status: str  # R2: new stored state.


class CanvasStatusMetaDTO(BaseModel):
    """R2: 报告头 chip 用的轻量状态读接口(不拉 sections)."""

    status: str = "active"  # Stored state.
    effectiveStatus: str = "active"  # Display state (including quarantine).
    quarantinedCount: int = 0


# ─────────────────────────────────────────────────────────────
# Files
# ─────────────────────────────────────────────────────────────
class FileKbStatusDTO(BaseModel):
    """知识库体验整治 FR7：文件页「知识库」列所需的 KB 行投影。

    只在调用方**按 KB 读权限能看到**这行时给出；字段与 `KbFileDTO` 同名同义，
    前端用同一个五态映射（`lib/kb-file-status.ts`）。
    """

    file_id: str
    scope: Literal["personal", "space", "team"]
    # Space rows only: owning space (caller verified as a member); frontend switches the deep link to that space.
    workspace_id: str | None = None
    parse_status: str
    parse_progress_pct: int | None = None
    parse_stuck: bool = False
    parse_error_code: str | None = None


class FileListItemDTO(BaseModel):
    file_id: str
    filename: str
    mime: str
    mime_family: str | None = None
    size: int
    category: str | None = None
    category_source: str | None = None
    source: str
    # Sprint ι (PRD §4.B.3): source → origin enum, frontend i18n via dataTab.origin keys.
    # Sprint pptx-from-template M1: += "web" (web_clip) / "deck" (deliverable).
    # file-management-v2 P0-2: += "other" (unknown-source fallback, no longer masquerades as upload).
    origin: Literal["query", "compute", "upload", "export", "web", "deck", "other"] = "upload"
    # M1 asset-model: kind drives the FE badge; trust drives unverified styling.
    kind: str = "tool_data"
    trust: str = "trusted"
    created_at: str
    last_used_at: str | None = None
    deleted_at: str | None = None
    usage_count: int = 0
    # file-management-v2 P1: active kb_files overlay for this artifact (cross-store batch lookup;
    # see KbFilesStore.artifact_ids_in_kb). Drives KB badge + deletion-entry consolidation (D2).
    in_kb: bool = False
    # file-management-v2 P2.5: backend decides from kind + authoritative KB MIME rules; all three frontend
    # Add-to-KB entries consume it. Never duplicate the MIME allowlist.
    kb_promotable: bool = False
    # KB experience cleanup FR7: status of the KB row visible to the caller. in_kb=True with kb=None
    # means present in KB outside caller visibility (e.g. another user put a shared file in their
    # personal KB). Do not disclose its status.
    kb: FileKbStatusDTO | None = None
    # file-management-v2 P1: source session, the runtime-data column indicating the task that produced it.
    # Use the session with the earliest pin in session_attachments.
    session_id: str | None = None
    session_title: str | None = None
    # file-management-v2 P2: this artifact is attached to a canvas/report (`artifacts.canvas_id`).
    # Warn additionally before deletion: historical-report downloads break (recoverable within 30 days).
    has_canvas_ref: bool = False
    # file-management-v2 D7: long_term | one_off | None. Drives the automatic-addition mark (✨) on
    # the KB badge and provides observability for automatic-indexing criteria.
    retention_hint: str | None = None
    # file-management-v2 P3: this row was **shared by someone else**, so read-only. Frontend hides
    # delete / recategorize / add-to-KB entries; services also enforce ownership independently.
    shared_readonly: bool = False
    url: str


class FileListDTO(BaseModel):
    items: list[FileListItemDTO] = Field(default_factory=list)
    total: int = 0
    size_total_bytes: int = 0


class FilePreviewDTO(BaseModel):
    family: str
    mode: str = "text"  # text | rows | image | pdf_pages | xlsx_sheets | pptx_slides
    text: str | None = None
    truncated: bool = False
    note: str | None = None
    error: str | None = None
    error_code: str | None = None
    # #100 rows mode (CSV / TSV structured preview)
    columns: list[str] | None = None
    rows: list[dict[str, Any]] | None = None
    total_rows: int | None = None
    shown_rows: int | None = None


# ─────────────────────────────────────────────────────────────
# Skill management (Phase 1)
# ─────────────────────────────────────────────────────────────
class ResourceUsageSummaryDTO(BaseModel):
    current_agent_count: int = 0
    running_task_count: int = 0
    draft_agent_count: int = 0
    historical_agent_count: int = 0
    scheduled_binding_count: int = 0
    restricted_count: int = 0
    completeness: str = "complete"
    reason_codes: list[str] = Field(default_factory=list)


class SkillSummaryDTO(BaseModel):
    # Persisted personal/team preference-instance ID; null for built-in Skills without an instance.
    id: str | None = None
    name: str
    description: str = ""
    version: str | None = None
    source_type: str  # builtin | user_upload | user_generated
    enabled: bool = True
    size_bytes: int = 0
    created_at: str | None = None
    # Skills-page IA redesign (2026-07-10): readable user-facing title + category (display only,
    # from SKILL.md frontmatter title:/category:; FE infers from slug prefix when absent).
    title: str = ""
    # Optional Chinese display title for shipped built-ins; user-authored names remain unchanged.
    title_zh: str = ""
    # Reviewed user-facing descriptions for shipped built-ins (SKILL.md `listing`), by interface
    # language. Empty for authored Skills and when a personal description override is shown;
    # `description` stays the model-facing text the agent routes on.
    description_en: str = ""
    description_zh: str = ""
    description_de: str = ""
    description_fr: str = ""
    category: str = ""
    usage_summary: ResourceUsageSummaryDTO | None = None
    # skill-import batch 1 (tech-design r3 §2): pending import state: null | pending | cleared | overridden;
    # `effective = enabled ∧ import_status != "pending"`; FE displays enabled-but-pending-import as ineffective.
    import_status: str | None = None


class SkillScriptInfoDTO(BaseModel):
    name: str
    size: int


class SkillUsedByDTO(BaseModel):
    """引用了某 skill 的自定义子代理（详情 drawer「被子代理使用」反查行）."""

    id: str
    display_name: str


class SkillDetailDTO(SkillSummaryDTO):
    script_execution: str = "none"
    authoring_enabled: bool = True
    # Display: body **without frontmatter** (drawer Markdown; promotion content CAS compares this too).
    body_md: str = ""
    # Editing: **entire** on-disk SKILL.md including frontmatter. Keep separate: editing writes the whole
    # SKILL.md; saving body_md as the draft omits frontmatter and must fail validation
    # (116 device reproduction: Edit on description tab, then Save -> 400 E_SKILL_NAME_INVALID).
    body_raw: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    scripts: list[SkillScriptInfoDTO] = Field(default_factory=list)
    source_turn_id: str | None = None
    # #82 preview extension.
    trigger_examples: list[str] = Field(default_factory=list)
    references: list[SkillScriptInfoDTO] = Field(default_factory=list)
    eval_files: list[SkillScriptInfoDTO] = Field(default_factory=list)
    evals_count: int = 0
    has_evals: bool = False
    # Skills-page IA redesign: reverse team-subagent lookup (always empty when subagent_creator flag is off).
    used_by_subagents: list[SkillUsedByDTO] = Field(default_factory=list)
    # Whole-package fingerprint read with editable bytes, also used for Team synchronization.
    content_sha256: str | None = None
    # skill-import batch 1: bundle inventory report (BundleInventory.to_dict); nonnull only for inventoried personal Skills.
    import_report: dict[str, Any] | None = None


class SkillListDTO(BaseModel):
    authoring_enabled: bool = True
    impact_guard_enabled: bool = False
    builtin: list[SkillSummaryDTO] = Field(default_factory=list)
    user_upload: list[SkillSummaryDTO] = Field(default_factory=list)
    user_generated: list[SkillSummaryDTO] = Field(default_factory=list)
