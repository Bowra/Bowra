"""Public interfaces for agent orchestration, execution and persistence."""

import httpx2

# shortcut: MCP and other SDKs still import legacy names; remove after they migrate.
httpx2.alias_httpx()


from bowhead_core.access_control import (
    AccessContext,
    AccessControlStore,
    GroupRecord,
    ResourceGrantRecord,
    normalize_workspace_role,
)
from bowhead_core.approval_store import Approval, ApprovalStore
from bowhead_core.artifact_store import (
    Artifact,
    ArtifactBackend,
    ArtifactStore,
    LocalBackend,
    S3Backend,
)
from bowhead_core.canvas_store import Canvas, CanvasRevConflictError, CanvasStore, Revision, Section
from bowhead_core.events import (
    EventBusRegistry,
    OrchestratorEvent,
    TurnEventBus,
    get_event_registry,
)
from bowhead_core.language_context import LanguageContext
from bowhead_core.notification_store import (
    NotificationCount,
    NotificationForUser,
    NotificationItem,
    NotificationStore,
)
from bowhead_core.orchestrator import Orchestrator, OrchestratorConfig
from bowhead_core.personal_memory_governance import (
    CandidateNotFoundError,
    CandidateTargetError,
    GovernanceCandidate,
    PersonalMemoryGovernanceStore,
)
from bowhead_core.resource_access import (
    BUILTIN_CAPABILITIES,
    canonical_builtin_capability_id,
    canonical_permission,
    list_builtin_capability_ids,
)
from bowhead_core.session_store import Session, SessionStore, Turn
from bowhead_core.skill_access import skill_visible_by_grants
from bowhead_core.skill_instance_store import SkillInstance, SkillInstanceStore
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
    build_default_registry,
)
from bowhead_core.turn_log_store import OrderedTurnLogSink, TurnEventRow, TurnLogStore
from bowhead_core.types import (
    IntentType,
    ToolInvocation,
    TurnRequest,
    TurnResult,
    TurnStatus,
)
from bowhead_core.user_memory import (
    MemoryEntry,
    MemoryEntryLockedError,
    MemoryScopeConflictError,
    UserMemoryStore,
    summarize_for_prompt,
)

__all__ = [
    "BUILTIN_CAPABILITIES",
    "AccessContext",
    "AccessControlStore",
    "Approval",
    "ApprovalStore",
    "Artifact",
    "ArtifactBackend",
    "ArtifactStore",
    "Canvas",
    "CanvasRevConflictError",
    "CanvasStore",
    "CandidateNotFoundError",
    "CandidateTargetError",
    "EventBusRegistry",
    "GroupRecord",
    "GovernanceCandidate",
    "IntentType",
    "LanguageContext",
    "LocalBackend",
    "MemoryEntry",
    "MemoryEntryLockedError",
    "MemoryScopeConflictError",
    "NotificationCount",
    "NotificationForUser",
    "NotificationItem",
    "NotificationStore",
    "Orchestrator",
    "OrchestratorConfig",
    "OrchestratorEvent",
    "OrderedTurnLogSink",
    "PersonalMemoryGovernanceStore",
    "ResourceGrantRecord",
    "Revision",
    "S3Backend",
    "Section",
    "Session",
    "SessionStore",
    "SkillInstance",
    "SkillInstanceStore",
    "ToolExecutionContext",
    "ToolInvocation",
    "ToolRegistry",
    "ToolSpec",
    "Turn",
    "TurnEventBus",
    "TurnEventRow",
    "TurnLogStore",
    "TurnRequest",
    "TurnResult",
    "TurnStatus",
    "UserMemoryStore",
    "build_default_registry",
    "canonical_builtin_capability_id",
    "canonical_permission",
    "get_event_registry",
    "list_builtin_capability_ids",
    "normalize_workspace_role",
    "skill_visible_by_grants",
    "summarize_for_prompt",
]

__version__ = "0.1.0"
