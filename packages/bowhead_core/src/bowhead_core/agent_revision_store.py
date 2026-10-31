"""M2 Agent working-copy, publication-candidate, and activation state machine.

The Store owns one ``BEGIN IMMEDIATE`` transaction for revision freeze, exact
name reservation, approval decisions, live projection, grants, audit, release
events, and notification outbox rows.  It deliberately has no HTTP or Creator
surface yet; M2-8 will route legacy writers through this boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_automation_context import (
    AutomationPreparation,
    check_prepare_conn,
    operation_key_hash,
    preparation_conn,
    require_workspace_conn,
)
from bowhead_core.agent_behavior_authoring import (
    AgentBehaviorAuthoringError,
    AgentBehaviorAuthoringService,
    AgentBehaviorIntentValidation,
    default_agent_behavior_authoring_service,
)
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor
from bowhead_core.agent_behavior_materializer import (
    AgentBehaviorMaterializationError,
    AgentBehaviorMaterializer,
    default_agent_behavior_materializer,
)
from bowhead_core.agent_candidate_name_store import AgentCandidateNameStore
from bowhead_core.agent_compatibility import AgentCompatibilityError, AgentCompatibilityStore
from bowhead_core.agent_dependency_compiler import (
    AGENT_DEPENDENCY_COMPILER_VERSION,
    AgentDependencyCompilation,
    AgentDependencyCompiler,
    AgentDependencyContext,
)
from bowhead_core.agent_eval_plan_store import AgentEvalPlanError, AgentEvalPlanStore, AgentPreparationActor
from bowhead_core.agent_eval_policy_store import AgentEvalPolicyError, AgentEvalPolicyStore
from bowhead_core.agent_knowledge_policy import (
    KnowledgeAccessError,
    require_policy_preserved,
    require_supported_knowledge_access,
    source_bindings_enabled,
)
from bowhead_core.agent_listing_safety import LISTING_SAFETY_SCANNER_VERSION, scan_agent_listing
from bowhead_core.agent_runtime_extension_store import require_extension_policy_write, retain_extension_policy_in_connection
from bowhead_core.agent_taxonomy import AgentTaxonomyError, validate_taxonomy_listing_conn
from bowhead_core.agents._generated.tool_renames import canonical_tool_name
from bowhead_core.audit_actions import AuditAction
from bowhead_core.catalog_localization import (
    materialize_confirmed_agent_localizations_conn,
    without_server_derived_listing_keys,
)
from bowhead_core.resource_dependency_projection import (
    edges_from_compilation,
    invalidate_working_copy_projection_conn,
    replace_subject_projection_conn,
)
from bowhead_core.scrubber import scrub_with_count
from bowhead_core.secret_redaction import extract_secrets
from bowhead_core.subagent_authoring_catalog import APPROVAL_EXECUTOR_UNAVAILABLE, SubagentAuthoringCatalogService
from bowhead_core.subagent_write_boundary import _issue_team_subagent_transaction_token
from bowhead_core.team_plugin_store import TeamPluginError, expand_plugins_conn, selected_plugin_ids
from bowhead_core.user_store import UserStore

AudienceMode = Literal["private", "custom", "teamwide"]
SafetyStatus = Literal["pass", "warning", "review_required", "blocked", "unknown"]
SemanticStatus = Literal["not_run", "pass", "needs_review", "blocked", "error"]

AGENT_PUBLICATION_SAFETY_VERSION = f"agent-publication-safety-v1:{LISTING_SAFETY_SCANNER_VERSION}"
_MAX_DOCUMENT_BYTES = 512 * 1024
_MAX_RELEASE_NOTE_LENGTH = 2_000
_RECEIPT_TTL = timedelta(days=7)
_MAX_LISTING_TEXT_VALUES = 256
_SCRUBBER_REASON_CODES = {
    "身份证": "pii_cn_national_id",
    "手机号": "pii_cn_mobile",
    "邮箱": "pii_email",
    "api_key": "credential_or_secret",
}
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,159}$")
_USE_PUBLICATION_EVAL_GATE = object()


def _expand_builtin_plugins_for_publication(definition: dict[str, Any]) -> dict[str, Any]:
    """Validate and expand built-in Plugin selections."""
    from bowhead_core.system_plugins import BuiltinPluginSelectionError, expand_builtin_plugins

    if not definition.get("builtin_plugin_ids"):
        return definition
    try:
        return expand_builtin_plugins(definition)
    except BuiltinPluginSelectionError as exc:
        raise AgentPublicationBlockedError(exc.code, blocked_items=exc.blocked_items) from exc


class AgentRevisionError(RuntimeError):
    """Base error with a stable code suitable for an API adapter."""

    def __init__(self, code: str, *, details: Mapping[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})


class AgentRevisionForbiddenError(AgentRevisionError):
    pass


class AgentRevisionConflictError(AgentRevisionError):
    pass


def candidate_already_active_error(candidate: aiosqlite.Row, *, code: str = "agent_release_candidate_already_active") -> AgentRevisionConflictError:
    """Name the active candidate and the two ways forward, so the caller can act instead of retrying."""

    return AgentRevisionConflictError(
        code,
        details={
            "active_candidate_id": str(candidate["id"]),
            "active_candidate_state": str(candidate["state"]),
            "next_step": "Continue with the active candidate, or ask the Owner to choose Prepare from latest draft on the Release tab, which replaces it.",
        },
    )


@dataclass(frozen=True, slots=True)
class CandidateReplacement:
    """The prepared candidate the Owner replaces with one built from the latest draft (exact CAS identity)."""

    candidate_id: str
    row_version: int
    candidate_hash: str


async def owned_agent_working_copy_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    actor_user_id: str,
) -> tuple[aiosqlite.Row | None, str]:
    """Resolve an Owner's editable working copy, saying WHY when there is none.

    Single implementation on purpose.  Localization and taxonomy each carried a
    near-identical copy of this query and had already drifted -- one filtered
    ``lifecycle_state='active'`` and the other did not, so an archived Agent could
    have its localizations edited but not its taxonomy.

    Returns ``(row, "")`` on success, otherwise ``(None, reason)`` where reason is:

    * ``"release_cutover_required"`` -- the Agent exists and the caller owns it,
      but it has never been imported into release history, so it has no working
      copy.  Reporting that as "not found" tells an Owner that an Agent the
      catalog just listed does not exist.
    * ``"unavailable"`` -- no such Agent for this caller.
    """
    async with db.execute(
        """
        SELECT working.*
        FROM team_subagent_working_copy working
        JOIN team_subagent agent
          ON agent.team_id=working.team_id AND agent.id=working.subagent_id
        JOIN teams team ON team.id=working.team_id AND team.status='active'
        JOIN users actor ON actor.id=? AND actor.status='active'
        JOIN team_members member
          ON member.team_id=working.team_id AND member.user_id=actor.id
         AND member.status='active'
        WHERE working.team_id=? AND working.subagent_id=?
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
          AND agent.lifecycle_state='active'
        """,
        (actor_user_id, team_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is not None:
        return row, ""
    async with db.execute(
        """
        SELECT definition_mode FROM team_subagent
        WHERE team_id=? AND id=? AND lifecycle_state='active'
          AND COALESCE(owner_user_id,created_by)=?
        """,
        (team_id, agent_id, actor_user_id),
    ) as cursor:
        legacy = await cursor.fetchone()
    if legacy is not None and str(legacy["definition_mode"]) == "legacy_mutable":
        return None, "release_cutover_required"
    return None, "unavailable"


class AgentReleaseCutoverRequiredError(AgentRevisionError):
    """The Agent exists but this deployment has not cut over to release semantics.

    Distinct from "not found" on purpose: an owner opening the manager for an Agent
    the catalog just listed (with actions.manage=true and a manager_url) must not be
    told it does not exist.  The remedy is the offline release baseline import.
    """


class AgentPublicationBlockedError(AgentRevisionError):
    def __init__(
        self,
        code: str,
        *,
        reason_codes: tuple[str, ...] = (),
        blocked_items: tuple[Mapping[str, Any], ...] = (),
        details: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code, details=details)
        self.reason_codes = reason_codes
        # [{kind: "skill" | "tool" | "definition", name, identity, reason_codes}]: the
        # dependencies that block publication, so the error can name them (agent-skill-gates D5).
        self.blocked_items = tuple(dict(item) for item in blocked_items)


class AgentReleaseStaleError(AgentRevisionError):
    def __init__(self, candidate_id: str, *, reason_codes: tuple[str, ...]) -> None:
        super().__init__("agent_release_candidate_stale")
        self.candidate_id = candidate_id
        self.reason_codes = reason_codes


@dataclass(frozen=True, slots=True)
class AgentAudience:
    mode: AudienceMode
    user_ids: tuple[str, ...] = ()
    group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"private", "custom", "teamwide"}:
            raise ValueError("unsupported Agent audience mode")
        if tuple(sorted(set(self.user_ids))) != self.user_ids or tuple(sorted(set(self.group_ids))) != self.group_ids:
            raise ValueError("Agent audience principals must be sorted and unique")
        if any(not value or len(value) > 200 for value in (*self.user_ids, *self.group_ids)):
            raise ValueError("Agent audience principal is invalid")
        if len(self.user_ids) + len(self.group_ids) > 200:
            raise ValueError("Agent audience exceeds 200 principals")
        if self.mode == "custom" and not (self.user_ids or self.group_ids):
            raise ValueError("custom Agent audience requires a principal")
        if self.mode != "custom" and (self.user_ids or self.group_ids):
            raise ValueError("private/teamwide Agent audience cannot include a roster")

    def to_dict(self) -> dict[str, Any]:
        return {
            "effective_mode": self.mode,
            "group_ids": list(self.group_ids),
            "schema_version": 1,
            "user_ids": list(self.user_ids),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AgentAudience:
        if set(value) != {"effective_mode", "group_ids", "schema_version", "user_ids"} or value.get("schema_version") != 1:
            raise ValueError("Agent audience snapshot is malformed")
        users = value.get("user_ids")
        groups = value.get("group_ids")
        if not isinstance(users, list) or not all(isinstance(item, str) for item in users):
            raise ValueError("Agent audience user roster is malformed")
        if not isinstance(groups, list) or not all(isinstance(item, str) for item in groups):
            raise ValueError("Agent audience group roster is malformed")
        mode = value.get("effective_mode")
        if mode not in {"private", "custom", "teamwide"}:
            raise ValueError("Agent audience mode is malformed")
        return cls(
            mode=mode,
            user_ids=tuple(users),
            group_ids=tuple(groups),
        )

    @classmethod
    def from_release_snapshot(cls, snapshot: Mapping[str, Any]) -> AgentAudience:
        """Shared by Owner reads and restore; never rewrite the frozen release."""
        if set(snapshot) == {
            "effective_mode",
            "group_ids",
            "schema_version",
            "user_ids",
        }:
            try:
                return cls.from_mapping(snapshot)
            except ValueError as exc:
                raise AgentPublicationBlockedError("agent_release_audience_malformed") from exc
        mode = snapshot.get("effective_mode")
        grants = snapshot.get("grants")
        if mode not in {"private", "custom", "teamwide"} or not isinstance(grants, list):
            raise AgentPublicationBlockedError("agent_release_audience_malformed")
        user_ids: set[str] = set()
        group_ids: set[str] = set()
        for item in grants:
            if not isinstance(item, Mapping):
                raise AgentPublicationBlockedError("agent_release_audience_malformed")
            if item.get("effect") != "allow" or item.get("permission") not in {
                "use",
                "write",
                "admin",
            }:
                continue
            principal_id = item.get("principal_id")
            if not isinstance(principal_id, str) or not principal_id:
                raise AgentPublicationBlockedError("agent_release_audience_malformed")
            if item.get("principal_type") == "user":
                user_ids.add(principal_id)
            elif item.get("principal_type") == "group":
                group_ids.add(principal_id)
        try:
            return cls(
                mode,
                user_ids=tuple(sorted(user_ids)) if mode == "custom" else (),
                group_ids=tuple(sorted(group_ids)) if mode == "custom" else (),
            )
        except ValueError as exc:
            raise AgentPublicationBlockedError("agent_release_audience_malformed") from exc


@dataclass(frozen=True, slots=True)
class AgentPublicationValidation:
    compilation: AgentDependencyCompilation
    safety_status: SafetyStatus
    deterministic_reason_codes: tuple[str, ...]
    semantic_status: SemanticStatus
    semantic_reason_codes: tuple[str, ...]
    scanner_version: str = AGENT_PUBLICATION_SAFETY_VERSION
    semantic_scanner_version: str = "none"
    behavior_status: Literal["not_requested", "blocked", "shadow", "reviewed"] = "not_requested"
    behavior_fingerprint: str | None = None
    behavior_reason_codes: tuple[str, ...] = ()
    behavior_primary_requested: bool = False

    def __post_init__(self) -> None:
        if self.safety_status not in {"pass", "warning", "review_required", "blocked", "unknown"}:
            raise ValueError("unsupported Agent Safety status")
        if self.semantic_status not in {"not_run", "pass", "needs_review", "blocked", "error"}:
            raise ValueError("unsupported Agent semantic Safety status")
        if self.behavior_status not in {"not_requested", "blocked", "shadow", "reviewed"}:
            raise ValueError("unsupported Agent behavior status")
        if self.behavior_fingerprint is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", self.behavior_fingerprint) is None:
            raise ValueError("Agent behavior fingerprint is invalid")
        if not self.scanner_version or len(self.scanner_version) > 120:
            raise ValueError("Agent Safety scanner version is invalid")
        if not self.semantic_scanner_version or len(self.semantic_scanner_version) > 120:
            raise ValueError("Agent semantic scanner version is invalid")
        for values in (self.deterministic_reason_codes, self.semantic_reason_codes):
            if tuple(sorted(set(values))) != values or any(not value or len(value) > 160 for value in values):
                raise ValueError("Agent Safety reason codes must be sorted and unique")
        if tuple(sorted(set(self.behavior_reason_codes))) != self.behavior_reason_codes or any(
            not value or len(value) > 256 for value in self.behavior_reason_codes
        ):
            raise ValueError("Agent behavior reason codes must be sorted and unique")

    def deterministic_dict(self) -> dict[str, Any]:
        return {
            "reason_codes": list(self.deterministic_reason_codes),
            "status": "blocked" if self.deterministic_reason_codes else "pass",
        }

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "reason_codes": list(self.semantic_reason_codes),
            "status": self.semantic_status,
        }

    def safe_summary_dict(self) -> dict[str, Any]:
        return {
            "reason_codes": sorted(
                set(self.compilation.reason_codes)
                | set(self.deterministic_reason_codes)
                | set(self.semantic_reason_codes)
                | _public_behavior_reason_codes(self),
            ),
            "status": self.safety_status,
        }

    def behavior_dict(self) -> dict[str, Any]:
        return {
            "behavior_fingerprint": self.behavior_fingerprint,
            "primary_requested": self.behavior_primary_requested,
            "reason_codes": list(self.behavior_reason_codes),
            "status": self.behavior_status,
        }


class AgentPublicationValidator(Protocol):
    async def validate(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        owner_user_id: str,
        routing_name: str,
        definition: Mapping[str, Any],
        listing: Mapping[str, Any],
        audience: AgentAudience,
        semantic_evidence_definition: Mapping[str, Any] | None = None,
    ) -> AgentPublicationValidation: ...


class AgentPublicationEvalGate(Protocol):
    async def evidence_for_activation(
        self,
        db: aiosqlite.Connection,
        *,
        candidate: Mapping[str, Any],
        audience: AgentAudience,
    ) -> Mapping[str, Any] | None: ...

    async def prepare_for_activation(
        self,
        db_path: str,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> None:
        """Install whatever the gate requires, before the activation transaction.

        ``evidence_for_activation`` runs while the caller holds BEGIN IMMEDIATE, so
        it cannot create the platform contract suite itself without deadlocking.
        Without this hook the very first non-private publication of every Agent fails
        with `agent_eval_platform_suite_missing` -- a blocker the owner can only clear
        by finding the eval panel first, which nothing tells them to do.
        """
        ...


class AgentReferenceCoverageResolver(Protocol):
    """Resolve exact Skill coverage from the current Audience/grant truth."""

    async def resolve(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        owner_user_id: str,
        definition: Mapping[str, Any],
        audience: AgentAudience,
    ) -> frozenset[str]: ...


class AgentMcpEntitlementResolver(Protocol):
    """Resolve the current team entitlement instead of caching it in the Store."""

    async def is_entitled(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AgentSemanticSafetyResult:
    status: SemanticStatus
    reason_codes: tuple[str, ...]
    scanner_version: str

    def __post_init__(self) -> None:
        if self.status not in {"pass", "needs_review", "blocked", "error"}:
            raise ValueError("unsupported Agent semantic Safety result")
        if not self.scanner_version or len(self.scanner_version) > 120:
            raise ValueError("Agent semantic scanner version is invalid")
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes or any(not _REASON_CODE_RE.fullmatch(value) for value in self.reason_codes):
            raise ValueError("Agent semantic Safety reason codes are invalid")


class AgentSemanticSafetyScanner(Protocol):
    scanner_version: str

    async def scan(
        self,
        *,
        team_id: str,
        actor_user_id: str,
        definition: Mapping[str, Any],
        listing: Mapping[str, Any],
        audience_mode: AudienceMode,
        compilation: AgentDependencyCompilation,
    ) -> AgentSemanticSafetyResult: ...


class AgentSemanticEvidenceResolver(Protocol):
    async def resolve(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        content_hash: str,
        dependency_fingerprint: str,
        compiler_version: str,
    ) -> AgentSemanticSafetyResult | None: ...


class DeterministicAgentPublicationValidator:
    """Current conservative validator until M2-9 adds semantic orchestration."""

    def __init__(
        self,
        *,
        catalog_store: object,
        dependency_compiler: AgentDependencyCompiler,
        catalog_service: SubagentAuthoringCatalogService | None = None,
        mcp_entitlement_resolver: AgentMcpEntitlementResolver | None = None,
        reference_coverage_resolver: AgentReferenceCoverageResolver | None = None,
        semantic_evidence_resolver: AgentSemanticEvidenceResolver | None = None,
        behavior_authoring: AgentBehaviorAuthoringService | None = None,
        behavior_materializer: AgentBehaviorMaterializer | None = None,
    ) -> None:
        self._catalog_store = catalog_store
        self._dependency_compiler = dependency_compiler
        self._catalog_service = catalog_service or SubagentAuthoringCatalogService()
        self._mcp_entitlement_resolver = mcp_entitlement_resolver
        self._reference_coverage_resolver = reference_coverage_resolver
        self._semantic_evidence_resolver = semantic_evidence_resolver
        self._behavior_authoring = behavior_authoring or default_agent_behavior_authoring_service()
        self._behavior_materializer = behavior_materializer or default_agent_behavior_materializer()

    async def normalize_legacy_definition(
        self,
        definition: Mapping[str, Any],
        *,
        team_id: str,
    ) -> dict[str, Any]:
        """Resolve an imported baseline definition for current-time revalidation."""
        normalized = dict(definition)
        for legacy_key, current_key in (
            ("tool_refs_json", "tool_refs"),
            ("output_schema_json", "output_schema"),
        ):
            if current_key in normalized or legacy_key not in normalized:
                continue
            raw = normalized.pop(legacy_key)
            if raw is None and current_key == "output_schema":
                normalized[current_key] = None
                continue
            try:
                normalized[current_key] = json.loads(str(raw))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AgentPublicationBlockedError(
                    "agent_legacy_definition_malformed",
                ) from exc
        if "skill_grants" not in normalized and "skills_json" in normalized:
            raw_skills = normalized.pop("skills_json")
            try:
                skill_names = json.loads(str(raw_skills))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AgentPublicationBlockedError(
                    "agent_legacy_definition_malformed",
                ) from exc
            if not isinstance(skill_names, list) or not all(isinstance(item, str) for item in skill_names):
                raise AgentPublicationBlockedError("agent_legacy_definition_malformed")
            try:
                grants = await self._dependency_compiler.exact_grants_for_legacy_names(
                    skill_names,
                    team_id=team_id,
                    tool_refs=normalized.get("tool_refs", ()),
                )
            except ValueError as exc:
                raise AgentPublicationBlockedError(
                    "agent_legacy_dependency_unavailable",
                ) from exc
            normalized["skill_grants"] = [grant.to_canonical_dict() for grant in grants]
        return normalized

    async def validate(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        owner_user_id: str,
        routing_name: str,
        definition: Mapping[str, Any],
        listing: Mapping[str, Any],
        audience: AgentAudience,
        semantic_evidence_definition: Mapping[str, Any] | None = None,
    ) -> AgentPublicationValidation:
        """Validate ``definition`` for publication to ``audience``.

        ``semantic_evidence_definition`` names the definition whose stored
        semantic Safety report applies. It is only for a definition that differs
        from ``definition`` in its Skill grant revisions alone: the semantic scan
        reads the prompt, the listing and the compiled dependency summary, and
        the dependency fingerprint (which leaves Skill revisions out) is still
        matched against ``definition``'s own compilation.
        """
        require_supported_knowledge_access(definition)
        try:
            effective_definition = await expand_plugins_conn(db, team_id=team_id, definition=definition)
        except TeamPluginError as exc:
            raise AgentPublicationBlockedError(exc.code) from exc
        effective_definition = _expand_builtin_plugins_for_publication(effective_definition)
        behavior_intent = self._behavior_authoring.validate_definition_intent(effective_definition)
        mcp_entitled = False
        if self._mcp_entitlement_resolver is not None:
            mcp_entitled = await self._mcp_entitlement_resolver.is_entitled(
                db,
                team_id=team_id,
                actor_user_id=owner_user_id,
            )
        reference_coverage = frozenset()
        if self._reference_coverage_resolver is not None:
            reference_coverage = await self._reference_coverage_resolver.resolve(
                db,
                team_id=team_id,
                owner_user_id=owner_user_id,
                definition=effective_definition,
                audience=audience,
            )
        tool_catalog = await self._catalog_service.build(
            store=self._catalog_store,
            team_id=team_id,
            actor_user_id=owner_user_id,
            actor_active=True,
            mcp_entitled=mcp_entitled,
        )
        compilation = await self._dependency_compiler.compile(
            effective_definition,
            context=AgentDependencyContext(
                agent_id=agent_id,
                team_id=team_id,
                owner_user_id=owner_user_id,
                audience_mode=audience.mode,
                reference_coverage=reference_coverage,
            ),
            tool_catalog=tool_catalog,
        )
        behavior = _compile_behavior_preview(
            materializer=self._behavior_materializer,
            agent_id=agent_id,
            definition=effective_definition,
            compilation=compilation,
            intent=behavior_intent,
        )
        scanner_version = _publication_scanner_version(behavior)
        deterministic: set[str] = set()
        if behavior_intent.primary_capability_ref is not None and behavior.verification_state != "reviewed":
            deterministic.update({"behavior_compile_blocked", "primary_capability_unproven"})
        elif definition.get("capability_composition") is not None and behavior.verification_state != "reviewed":
            deterministic.add("behavior_compile_blocked")
        listing_result = scan_agent_listing(
            name=routing_name,
            display_name=listing.get("display_name"),
            description=listing.get("description", listing.get("summary", "")),
        )
        deterministic.update(listing_result.reason_codes)
        try:
            await validate_taxonomy_listing_conn(db, team_id=team_id, listing=listing)
        except AgentTaxonomyError:
            deterministic.add("agent_taxonomy_invalid")
        try:
            listing_texts = _listing_text_values(listing)
        except ValueError:
            deterministic.add("listing_safety_limit_exceeded")
        else:
            for value in listing_texts:
                deterministic.update(
                    scan_agent_listing(
                        name="",
                        display_name="",
                        description=value,
                    ).reason_codes,
                )
        prompt = definition.get("system_prompt", "")
        if not isinstance(prompt, str):
            deterministic.add("prompt_malformed")
            prompt = ""
        deterministic.update(_prompt_data_safety_reason_codes(prompt))
        try:
            from bowhead_core.agents.manifest_security import lint_user_append

            lint_user_append(prompt)
        except Exception as exc:
            from bowhead_core.agents.exceptions import ManifestSecurityError

            deterministic.add(
                "prompt_injection_risk" if isinstance(exc, ManifestSecurityError) else "prompt_safety_check_failed",
            )

        if deterministic:
            safety_status: SafetyStatus = "blocked"
            semantic_status: SemanticStatus = "not_run"
            semantic_reasons: tuple[str, ...] = ()
            semantic_scanner_version = "none"
        elif compilation.status == "blocked":
            safety_status = "unknown"
            semantic_status = "not_run"
            semantic_reasons = ()
            semantic_scanner_version = "none"
        else:
            semantic = None
            if self._semantic_evidence_resolver is not None:
                evidence_definition = definition
                evidence_scanner_version = scanner_version
                if semantic_evidence_definition is not None:
                    evidence_definition = semantic_evidence_definition
                    evidence_scanner_version = await self._evidence_scanner_version(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        definition=semantic_evidence_definition,
                        compilation=compilation,
                    )
                semantic = await self._semantic_evidence_resolver.resolve(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    content_hash=_hash_json(
                        {"definition": evidence_definition, "listing": listing},
                    ),
                    dependency_fingerprint=compilation.dependency_fingerprint,
                    compiler_version=evidence_scanner_version,
                )
            semantic_status = semantic.status if semantic else "not_run"
            semantic_reasons = semantic.reason_codes if semantic else ("semantic_scanner_unavailable",)
            semantic_scanner_version = semantic.scanner_version if semantic else "none"
            safety_status = _effective_semantic_safety_status(
                semantic_status,
                audience=audience,
            )
        return AgentPublicationValidation(
            compilation=compilation,
            safety_status=safety_status,
            deterministic_reason_codes=tuple(sorted(deterministic)),
            semantic_status=semantic_status,
            semantic_reason_codes=semantic_reasons,
            scanner_version=scanner_version,
            semantic_scanner_version=semantic_scanner_version,
            behavior_status=(
                behavior.verification_state
                if behavior_intent.primary_capability_ref is not None or definition.get("capability_composition") is not None
                else "not_requested"
            ),
            behavior_fingerprint=behavior.behavior_fingerprint,
            behavior_reason_codes=behavior.reason_codes,
            behavior_primary_requested=behavior_intent.primary_capability_ref is not None,
        )

    async def _evidence_scanner_version(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        definition: Mapping[str, Any],
        compilation: AgentDependencyCompilation,
    ) -> str:
        """The scanner version a Safety check of ``definition`` was stored under.

        The version names the behavior preview, whose source hash covers the
        whole definition, so it is recomputed from the evidence definition.
        """
        try:
            effective_definition = await expand_plugins_conn(db, team_id=team_id, definition=definition)
        except TeamPluginError as exc:
            raise AgentPublicationBlockedError(exc.code) from exc
        effective_definition = _expand_builtin_plugins_for_publication(effective_definition)
        behavior = _compile_behavior_preview(
            materializer=self._behavior_materializer,
            agent_id=agent_id,
            definition=effective_definition,
            compilation=compilation,
            intent=self._behavior_authoring.validate_definition_intent(effective_definition),
        )
        return _publication_scanner_version(behavior)


@dataclass(frozen=True, slots=True)
class AgentWorkingCopy:
    id: str
    team_id: str
    agent_id: str
    base_release_id: str
    definition: dict[str, Any]
    listing: dict[str, Any]
    audience: AgentAudience
    row_version: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class AgentPublicationResult:
    state: Literal["pending", "activated", "rejected", "withdrawn", "stale"]
    agent_id: str
    candidate_id: str | None
    revision_id: str
    release_id: str | None
    candidate_row_version: int | None
    aggregate_row_version: int


@dataclass(frozen=True, slots=True)
class AgentPreparationResult:
    state: Literal["prepared"]
    agent_id: str
    candidate_id: str
    candidate_hash: str
    candidate_row_version: int
    revision_id: str
    compatibility_major: int
    compatibility_verdict: Literal["compatible", "breaking", "unknown"]
    policy_revision_id: str
    policy_hash: str
    plan_id: str
    plan_hash: str
    control_row_version: int
    source_working_copy_hash: str
    aggregate_row_version: int


@dataclass(frozen=True, slots=True)
class AgentLifecycleResult:
    state: Literal["active", "archived"]
    agent_id: str
    aggregate_row_version: int
    paused_schedule_count: int
    pending_candidate_withdrawn: bool


@dataclass(frozen=True, slots=True)
class _LiveProjection:
    system_prompt: str
    tool_refs: tuple[str, ...]
    model: str | None
    max_iterations: int
    output_schema: dict[str, Any] | None
    skill_names: tuple[str, ...]
    display_name: str
    description: str


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def requires_admin_review(
    validation: AgentPublicationValidation,
    *,
    audience: AgentAudience,
    eval_gate_installed: bool,
) -> bool:
    """Whether publishing this draft produces a pending candidate instead of a release.

    The single authority for that question.  The Owner surface has to render from
    this and not re-derive it: guessing from the audience alone was wrong for
    ``custom`` the moment the Eval gate went in, and the UI went on promising
    "a normal-risk custom share releases directly" while the server queued it for
    approval.
    """
    if audience.mode == "private":
        return False
    if eval_gate_installed:
        # M3 Eval is candidate-bound.  A shared candidate must remain frozen
        # long enough to run the platform contract before Admin activation.
        return True
    return (
        audience.mode == "teamwide"
        or validation.safety_status == "review_required"
        or validation.compilation.status == "review_required"
        or _risk_requires_release_approval(validation.compilation.risk)
    )


def _risk_requires_release_approval(risk: Any) -> bool:
    """Whether the risk assessment alone needs administrator approval before release.

    A shared Agent whose Tools write externally or destroy data, or that carries a high-risk Skill
    finding, needs approval. Selecting such Tools into a private Agent needs none: the Owner's
    selection is the grant.
    """
    return bool(risk.approval_required) or risk.level == "high"


class AgentRevisionStore:
    def __init__(
        self,
        db_path: str,
        *,
        validator: AgentPublicationValidator,
        eval_gate: AgentPublicationEvalGate | None = None,
        clock: _Clock | None = None,
        knowledge_source_validator: Any | None = None,
        behavior_authoring: AgentBehaviorAuthoringService | None = None,
        behavior_materializer: AgentBehaviorMaterializer | None = None,
    ) -> None:
        self._knowledge_source_validator = knowledge_source_validator
        self.db_path = str(db_path)
        self._validator = validator
        self._eval_gate = eval_gate
        self._clock = clock or _SystemClock()
        self._behavior_authoring = behavior_authoring or default_agent_behavior_authoring_service()
        self._behavior_materializer = behavior_materializer or default_agent_behavior_materializer()
        self._name_store = AgentCandidateNameStore()
        self._access_store = AccessControlStore(self.db_path)
        self._compatibility_store = AgentCompatibilityStore(self.db_path)
        self._eval_policy_store = AgentEvalPolicyStore(self.db_path, clock=self._clock)
        self._eval_plan_store = AgentEvalPlanStore(self.db_path, clock=self._clock)
        self._transaction_token = _issue_team_subagent_transaction_token()

    async def initialize_created_agent_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        definition: Mapping[str, Any],
        listing: Mapping[str, Any],
        target_audience: AgentAudience,
    ) -> AgentPublicationResult:
        """Create the first release inside the Creator's aggregate transaction.

        A newly authored Agent always receives a private live release first.
        If the legacy Creator requested sharing, the same transaction freezes a
        second candidate for Admin review while the private release remains
        live.  This preserves the friendly Creator flow without letting a new
        shared definition bypass M2 publication governance.
        """
        definition_json, definition_value = _canonical_document(definition, name="definition")
        listing_json, listing_value = _canonical_document(listing, name="listing")
        _names_from_listing(listing_value)
        _live_projection_values(definition_value, listing_value)
        aggregate = await self._owned_aggregate_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )
        if (
            str(aggregate["definition_mode"]) != "legacy_mutable"
            or aggregate["live_release_id"] is not None
            or aggregate["live_revision_id"] is not None
            or int(aggregate["release_sequence"]) != 0
        ):
            raise AgentRevisionConflictError("agent_initial_release_already_exists")
        if str(aggregate["status"]) != "enabled" or str(aggregate["lifecycle_state"] or "active") != "active":
            raise AgentPublicationBlockedError("agent_not_active")
        await self._validate_audience_conn(db, team_id=team_id, audience=target_audience, agent_id=agent_id)

        timestamp = self._now()
        private_audience = AgentAudience("private")
        definition_value, private_validation = await self._validate_with_current_skills(
            db,
            team_id=team_id,
            agent_id=agent_id,
            owner_user_id=actor_user_id,
            routing_name=str(aggregate["name"]),
            definition=definition_value,
            listing=listing_value,
            audience=private_audience,
        )
        definition_json = _canonical_json(definition_value)
        self._require_publishable(private_validation, audience=private_audience)
        initial_working = await self._insert_initial_working_copy_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            base_release_id=None,
            definition_json=definition_json,
            listing_json=listing_json,
            audience=private_audience,
            timestamp=timestamp,
        )
        frozen = await self._freeze_candidate_conn(
            db,
            aggregate=aggregate,
            working=initial_working,
            definition=definition_value,
            listing=listing_value,
            audience=private_audience,
            validation=private_validation,
            actor_user_id=actor_user_id,
            release_note="Initial private release",
            eval_json=None,
            timestamp=timestamp,
        )
        activated = await self._activate_conn(
            db,
            aggregate=aggregate,
            candidate=frozen.candidate,
            definition=definition_value,
            listing=listing_value,
            audience=private_audience,
            validation=private_validation,
            actor_user_id=actor_user_id,
            self_review=False,
            decision_reason="initial_private_release",
            timestamp=timestamp,
        )
        if target_audience.mode == "private":
            return activated

        shared_validation = await self._validator.validate(
            db,
            team_id=team_id,
            agent_id=agent_id,
            owner_user_id=actor_user_id,
            routing_name=str(aggregate["name"]),
            definition=definition_value,
            listing=listing_value,
            audience=target_audience,
        )
        self._require_publishable(shared_validation, audience=target_audience)
        if not await self._has_qualified_reviewer_conn(db, team_id=team_id):
            raise AgentPublicationBlockedError("qualified_reviewer_unavailable")
        aggregate = await self._aggregate_conn(db, team_id=team_id, agent_id=agent_id)
        shared_working = await self._insert_initial_working_copy_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
            base_release_id=str(aggregate["live_release_id"]),
            definition_json=definition_json,
            listing_json=listing_json,
            audience=target_audience,
            timestamp=timestamp,
        )
        shared = await self._freeze_candidate_conn(
            db,
            aggregate=aggregate,
            working=shared_working,
            definition=definition_value,
            listing=listing_value,
            audience=target_audience,
            validation=shared_validation,
            actor_user_id=actor_user_id,
            release_note="Initial sharing request",
            eval_json=None,
            timestamp=timestamp,
        )
        event_id = await self._insert_event_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=shared.candidate_id,
            release_id=None,
            event_type="candidate.submitted",
            actor_user_id=actor_user_id,
            payload={
                "audience_mode": target_audience.mode,
                "behavior_fingerprint": shared.behavior_fingerprint,
                "candidate_hash": shared.candidate_hash,
                "primary_capability_ref": shared.primary_capability_ref,
                "risk_level": shared_validation.compilation.risk.level,
            },
            timestamp=timestamp,
        )
        await self._enqueue_reviewers_conn(
            db,
            team_id=team_id,
            event_id=event_id,
            candidate_id=shared.candidate_id,
            timestamp=timestamp,
        )
        await self._write_audit_conn(
            db,
            team_id=team_id,
            actor_user_id=actor_user_id,
            action=AuditAction.AGENT_RELEASE_CANDIDATE_SUBMITTED,
            agent_id=agent_id,
            payload={
                "candidate_id": shared.candidate_id,
                "audience_mode": target_audience.mode,
                "behavior_fingerprint": shared.behavior_fingerprint,
                "primary_capability_ref": shared.primary_capability_ref,
                "risk_level": shared_validation.compilation.risk.level,
            },
        )
        return AgentPublicationResult(
            state="pending",
            agent_id=agent_id,
            candidate_id=shared.candidate_id,
            revision_id=shared.revision_id,
            release_id=activated.release_id,
            candidate_row_version=shared.candidate_row_version,
            aggregate_row_version=int(aggregate["row_version"]),
        )

    async def _insert_initial_working_copy_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        base_release_id: str | None,
        definition_json: str,
        listing_json: str,
        audience: AgentAudience,
        timestamp: str,
    ) -> aiosqlite.Row:
        working_id = _id("awc")
        await db.execute(
            """
            INSERT INTO team_subagent_working_copy(
              id,team_id,subagent_id,base_release_id,definition_json,listing_json,
              audience_json,validation_json,row_version,updated_by,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,'{"status":"not_validated"}',1,?,?,?)
            """,
            (
                working_id,
                team_id,
                agent_id,
                base_release_id,
                definition_json,
                listing_json,
                _canonical_json(audience.to_dict()),
                actor_user_id,
                timestamp,
                timestamp,
            ),
        )
        row = await self._working_copy_conn(db, team_id=team_id, agent_id=agent_id)
        if row is None or str(row["id"]) != working_id:
            raise AgentRevisionConflictError("agent_working_copy_create_failed")
        return row

    async def open_working_copy(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_live_release_id: str,
        authorize: Callable[[aiosqlite.Connection], Awaitable[None]],
    ) -> tuple[AgentWorkingCopy, bool]:
        """Ensure an Owner baseline without replacing an existing working copy.

        Repeated opens read the current copy. They do not recover an earlier
        save or attest that an old copy survived an explicit Owner discard.
        """
        from bowhead_core.agent_release_read_store import _live_snapshot

        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await authorize(db)
                aggregate = await self._owned_aggregate_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                await self._require_revisioned_active(aggregate)
                if aggregate["live_release_id"] != expected_live_release_id:
                    raise AgentRevisionConflictError("agent_working_copy_base_stale")
                row = await self._working_copy_conn(db, team_id=team_id, agent_id=agent_id)
                created = row is None
                if row is None:
                    live = await _live_snapshot(db, aggregate=aggregate)
                    if live is None:
                        raise AgentRevisionConflictError("agent_live_release_missing")
                    row = await self._insert_initial_working_copy_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        actor_user_id=actor_user_id,
                        base_release_id=expected_live_release_id,
                        definition_json=_canonical_json(live["definition"]),
                        listing_json=_canonical_json(live["listing"]),
                        audience=AgentAudience.from_mapping(live["audience"]),
                        timestamp=self._now(),
                    )
                await authorize(db)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return _working_copy_from_row(row), created

    async def save_working_copy(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        definition: Mapping[str, Any],
        listing: Mapping[str, Any],
        audience: AgentAudience,
        expected_row_version: int | None,
        expected_base_release_id: str | None = None,
    ) -> AgentWorkingCopy:
        """``expected_base_release_id`` (optional) pins the live release the caller derived the
        draft from; a different live release inside this transaction is a conflict
        (``agent_working_copy_base_stale``) and nothing is written."""
        definition_json, definition_value = _canonical_document(definition, name="definition")
        listing_json, listing_value = _canonical_document(listing, name="listing")
        _names_from_listing(listing_value)
        audience_json = _canonical_json(audience.to_dict())
        timestamp = self._now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                aggregate = await self._owned_aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                await self._require_revisioned_active(aggregate)
                await self._validate_audience_conn(db, team_id=team_id, audience=audience, agent_id=agent_id)
                behavior_intent = self._behavior_authoring.validate_definition_intent(
                    definition_value,
                )
                await self._validate_knowledge_write(db, definition_value, team_id=team_id, actor_user_id=actor_user_id)
                await require_extension_policy_write(db, definition_value, audience_mode=audience.mode)
                validation_json = _canonical_json(
                    behavior_intent.working_copy_validation(),
                )
                base_release_id = str(aggregate["live_release_id"] or "")
                if not base_release_id:
                    raise AgentRevisionConflictError("agent_live_release_missing")
                if expected_base_release_id is not None and base_release_id != expected_base_release_id:
                    raise AgentRevisionConflictError("agent_working_copy_base_stale")
                existing = await self._working_copy_conn(db, team_id=team_id, agent_id=agent_id)
                if existing is not None:
                    require_policy_preserved(json.loads(str(existing["definition_json"])), definition_value)
                async with db.execute(
                    "SELECT definition_json FROM team_subagent_revision WHERE id=? AND team_id=? AND subagent_id=?",
                    (aggregate["live_revision_id"], team_id, agent_id),
                ) as cursor:
                    knowledge_preimage = await cursor.fetchone()
                if knowledge_preimage is not None:
                    require_policy_preserved(json.loads(str(knowledge_preimage[0])), definition_value)
                if "runtime_extensions" not in definition_value:
                    if existing is not None:
                        had_extensions = json.loads(str(existing["definition_json"])).get("runtime_extensions") is not None
                    else:
                        async with db.execute(
                            "SELECT json_type(definition_json,'$.runtime_extensions') FROM team_subagent_revision WHERE id=? AND team_id=? AND subagent_id=?",
                            (aggregate["live_revision_id"], team_id, agent_id),
                        ) as cursor:
                            previous_policy = await cursor.fetchone()
                        had_extensions = previous_policy is not None and previous_policy[0] not in {None, "null"}
                    if had_extensions:
                        raise AgentPublicationBlockedError("agent_runtime_extension_preservation_required")
                # Working-copy saves otherwise allow incomplete definitions.
                # This is only a new-selection guard, not a new publication or
                # runtime gate. Compare to the trusted preimage in this CAS txn.
                previous_definition = (
                    json.loads(str(existing["definition_json"])) if existing is not None else {"tool_refs_json": aggregate["tool_refs_json"]}
                )
                additions = _selection_tool_refs(definition_value) - _selection_tool_refs(previous_definition)
                if additions:
                    catalog = await SubagentAuthoringCatalogService().build(
                        store=UserStore(self.db_path),
                        team_id=team_id,
                        actor_user_id=actor_user_id,
                        actor_active=True,
                        # Licenses remain checked by publication/runtime;
                        # only approval availability is inspected here.
                        mcp_entitled=True,
                    )
                    if any(
                        tool.for_selection().locked_reason == APPROVAL_EXECUTOR_UNAVAILABLE
                        for ref, tool in catalog.tools_by_ref.items()
                        if ref in additions
                    ):
                        raise AgentPublicationBlockedError(APPROVAL_EXECUTOR_UNAVAILABLE)
                if existing is None:
                    if expected_row_version is not None:
                        raise AgentRevisionConflictError("agent_working_copy_version_conflict")
                    working_copy_id = _id("awc")
                    row_version = 1
                    await db.execute(
                        """
                        INSERT INTO team_subagent_working_copy(
                          id,team_id,subagent_id,base_release_id,definition_json,listing_json,
                          audience_json,validation_json,row_version,updated_by,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)
                        """,
                        (
                            working_copy_id,
                            team_id,
                            agent_id,
                            base_release_id,
                            definition_json,
                            listing_json,
                            audience_json,
                            validation_json,
                            actor_user_id,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    current_version = int(existing["row_version"])
                    if expected_row_version != current_version:
                        raise AgentRevisionConflictError("agent_working_copy_version_conflict")
                    if str(existing["base_release_id"] or "") != base_release_id:
                        raise AgentRevisionConflictError("agent_working_copy_base_stale")
                    if (
                        str(existing["definition_json"]) == definition_json
                        and str(existing["listing_json"]) == listing_json
                        and str(existing["audience_json"]) == audience_json
                    ):
                        await db.commit()
                        return _working_copy_from_row(existing)
                    working_copy_id = str(existing["id"])
                    row_version = current_version + 1
                    updated = await db.execute(
                        """
                        UPDATE team_subagent_working_copy
                        SET definition_json=?,listing_json=?,audience_json=?,
                            validation_json=?,row_version=?,
                            updated_by=?,updated_at=?
                        WHERE id=? AND row_version=?
                        """,
                        (
                            definition_json,
                            listing_json,
                            audience_json,
                            validation_json,
                            row_version,
                            actor_user_id,
                            timestamp,
                            working_copy_id,
                            current_version,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise AgentRevisionConflictError("agent_working_copy_version_conflict")
                    await invalidate_working_copy_projection_conn(
                        db,
                        team_id=team_id,
                        working_copy_id=working_copy_id,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return AgentWorkingCopy(
            id=working_copy_id,
            team_id=team_id,
            agent_id=agent_id,
            base_release_id=base_release_id,
            definition=definition_value,
            listing=listing_value,
            audience=audience,
            row_version=row_version,
            updated_at=timestamp,
        )

    async def publication_receipt(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        idempotency_key: str,
    ) -> AgentPublicationResult | None:
        """Read-only lookup of a still-valid historical publication receipt (no replay, no
        request-hash check); None when that publish never committed or its receipt expired."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT result_json,expires_at FROM agent_command_receipt
                WHERE team_id=? AND actor_user_id=? AND command_scope=? AND idempotency_hash=?
                """,
                (team_id, actor_user_id, f"agent.publish:{agent_id}", _hash_text(idempotency_key)),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None or str(row["expires_at"]) <= self._now():
            return None
        return _publication_result_from_json(str(row["result_json"]))

    async def discard_working_copy(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_working_copy_id: str,
        expected_row_version: int,
    ) -> bool:
        """Owner-only compare-and-delete of an exact working-copy version.

        Used to undo a working copy a caller created itself when the follow-up publish
        failed; any newer edit (row version moved) is left untouched.
        """
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._owned_aggregate_conn(db, team_id=team_id, agent_id=agent_id, actor_user_id=actor_user_id)
                deleted = await db.execute(
                    "DELETE FROM team_subagent_working_copy WHERE id=? AND team_id=? AND subagent_id=? AND row_version=?",
                    (expected_working_copy_id, team_id, agent_id, expected_row_version),
                )
                if deleted.rowcount == 1:
                    await invalidate_working_copy_projection_conn(db, team_id=team_id, working_copy_id=expected_working_copy_id)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return deleted.rowcount == 1

    async def prepare_working_copy(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_working_copy_id: str,
        expected_working_copy_version: int,
        expected_eval_policy_revision_id: str | None,
        workspace_id: str,
        idempotency_key: str,
        release_note: str | None = None,
        automation: AutomationPreparation | None = None,
        coach_delegation_id: str | None = None,
        replace_candidate: CandidateReplacement | None = None,
    ) -> AgentPreparationResult:
        """Freeze one exact candidate and its first Evaluation Plan atomically.

        `replace_candidate` is the Owner's "Prepare from latest draft": the named prepared candidate is
        withdrawn (`owner_replaced`) in the same transaction, so either both happen or neither does.
        CI and Coach can't replace a candidate.
        """
        if replace_candidate is not None and (automation is not None or coach_delegation_id is not None):
            raise AgentRevisionForbiddenError("agent_release_candidate_replace_owner_only")
        _validate_idempotency_key(idempotency_key)
        release_note = _release_note(release_note)
        _require_consumer_safe_release_note(release_note)
        if not workspace_id or len(workspace_id) > 200:
            raise ValueError("Agent Evaluation workspace identity is invalid")
        await self._require_agent_owner(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )

        async def check_coach(db: aiosqlite.Connection) -> None:
            if coach_delegation_id is not None:
                from bowhead_core.coach_ci_delegation import CoachCIError
                from bowhead_core.coach_ci_delegation import check_prepare_conn as check_coach_prepare

                if automation is not None:
                    raise CoachCIError("coach_ci_origin_mismatch")
                await check_coach_prepare(
                    db,
                    coach_delegation_id,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                    working_copy_id=expected_working_copy_id,
                    working_copy_version=expected_working_copy_version,
                    policy_revision_id=expected_eval_policy_revision_id,
                    workspace_id=workspace_id,
                )

        if coach_delegation_id is not None:
            async with open_shared_ctx(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN")
                await check_coach(db)
        await self._prepare_gate_for_activation(
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )
        request_hash = _hash_json(
            {
                **({"automation": asdict(automation)} if automation is not None else {}),
                **({"coach_delegation_id": coach_delegation_id} if coach_delegation_id is not None else {}),
                "agent_id": agent_id,
                "expected_eval_policy_revision_id": expected_eval_policy_revision_id,
                "expected_working_copy_id": expected_working_copy_id,
                "expected_working_copy_version": expected_working_copy_version,
                "release_note": release_note,
                **({"replace_candidate": asdict(replace_candidate)} if replace_candidate is not None else {}),
                "workspace_id": workspace_id,
            },
        )
        scope = f"agent.prepare:{agent_id}"
        timestamp = self._now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                await check_coach(db)
                association = None
                if automation is not None:
                    if automation.authority.effective_owner_user_id != actor_user_id:
                        raise AgentRevisionError("agent_owner_required")
                    current_grant = await automation.authority.require_conn(db, team_id=team_id, agent_id=agent_id, scope="candidate:prepare")
                    if current_grant["evaluation_workspace_id"] is not None:
                        await require_workspace_conn(
                            db, team_id=team_id, owner_id=actor_user_id, workspace_id=current_grant["evaluation_workspace_id"]
                        )
                    association = await preparation_conn(
                        db, team_id=team_id, agent_id=agent_id, authority=automation.authority, key_hash=operation_key_hash(idempotency_key)
                    )
                    if association is not None and (
                        association["request_hash"] != request_hash
                        or association["grant_id"] != automation.authority.grant_id
                        or association["proposal_id"] != automation.proposal_id
                    ):
                        raise AgentRevisionConflictError("agent_command_idempotency_conflict")
                aggregate = await self._owned_aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                replay = await self._preparation_receipt_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    await self._require_replayed_preparation_current_conn(
                        db,
                        aggregate=aggregate,
                        replay=replay,
                    )
                    await db.commit()
                    return replay
                if association is not None:
                    raise AgentRevisionConflictError("agent_release_candidate_state_conflict")
                automation_grant = None
                if automation is not None:
                    automation_grant = await check_prepare_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        preparation=automation,
                        working_copy_id=expected_working_copy_id,
                        working_copy_version=expected_working_copy_version,
                        workspace_id=workspace_id,
                        policy_revision_id=expected_eval_policy_revision_id,
                    )
                await self._require_revisioned_active(aggregate)
                active_candidate = await self._active_candidate_conn(db, team_id=team_id, agent_id=agent_id)
                if (
                    active_candidate is not None
                    and str(active_candidate["state"]) in {"prepared", "pending"}
                    and active_candidate["base_live_release_id"] is not None
                    and active_candidate["base_live_release_id"] != aggregate["live_release_id"]
                ):
                    # Live moved under the candidate (e.g. an audience contraction) without staling it.
                    # Activation and review would reject it anyway, so retire it here instead of leaving
                    # the Owner unable to prepare a replacement.
                    await self._mark_stale_conn(
                        db,
                        aggregate=aggregate,
                        candidate=active_candidate,
                        actor_user_id=actor_user_id,
                        reason_codes=("base_release_changed",),
                        timestamp=timestamp,
                    )
                    active_candidate = None
                replaced_candidate_id: str | None = None
                if replace_candidate is not None:
                    if active_candidate is None or str(active_candidate["id"]) != replace_candidate.candidate_id:
                        raise AgentRevisionConflictError("agent_release_candidate_version_conflict")
                    if str(active_candidate["state"]) != "prepared":
                        # A candidate in review is frozen for the reviewer; withdraw it explicitly instead.
                        raise AgentRevisionConflictError("agent_release_candidate_not_replaceable")
                    if await self._candidate_has_active_comparison_conn(db, candidate_id=replace_candidate.candidate_id):
                        raise AgentRevisionConflictError("agent_eval_comparison_active")
                    await self._withdraw_conn(
                        db,
                        team_id=team_id,
                        candidate=active_candidate,
                        actor_user_id=actor_user_id,
                        expected_row_version=replace_candidate.row_version,
                        expected_candidate_hash=replace_candidate.candidate_hash,
                        timestamp=timestamp,
                        reason_code="owner_replaced",
                    )
                    replaced_candidate_id = replace_candidate.candidate_id
                    active_candidate = None
                if active_candidate is not None:
                    raise candidate_already_active_error(active_candidate)
                working = await self._working_copy_conn(db, team_id=team_id, agent_id=agent_id)
                if working is None or str(working["id"]) != expected_working_copy_id or int(working["row_version"]) != expected_working_copy_version:
                    raise AgentRevisionConflictError("agent_working_copy_version_conflict")
                if working["base_release_id"] != aggregate["live_release_id"]:
                    raise AgentRevisionConflictError("agent_working_copy_base_stale")
                definition = _decode_object(working["definition_json"], name="definition")
                listing = _decode_object(working["listing_json"], name="listing")
                listing = await materialize_confirmed_agent_localizations_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    working_copy_id=expected_working_copy_id,
                    listing=listing,
                )
                audience = AgentAudience.from_mapping(
                    _decode_object(working["audience_json"], name="audience"),
                )
                _live_projection_values(definition, listing)
                await self._validate_audience_conn(db, team_id=team_id, audience=audience, agent_id=agent_id)
                working_definition = definition
                # The frozen revision records each Skill at its current approved
                # revision; the working copy hash still describes the Owner's draft.
                definition, validation = await self._validate_with_current_skills(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    owner_user_id=self._owner_id(aggregate),
                    routing_name=str(aggregate["name"]),
                    definition=definition,
                    listing=listing,
                    audience=audience,
                )
                self._require_publishable(validation, audience=audience)
                source_working_copy_hash = _working_copy_hash(
                    working=working,
                    definition=working_definition,
                    listing=listing,
                    audience=audience,
                )
                frozen = await self._freeze_candidate_conn(
                    db,
                    aggregate=aggregate,
                    working=working,
                    definition=definition,
                    listing=listing,
                    audience=audience,
                    validation=validation,
                    actor_user_id=actor_user_id,
                    release_note=release_note,
                    eval_json=None,
                    timestamp=timestamp,
                    initial_state="prepared",
                )
                live_release_id = aggregate["live_release_id"]
                if live_release_id is None:
                    raise AgentRevisionConflictError("agent_live_release_missing")
                baseline_release = await self._release_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    release_id=str(live_release_id),
                )
                baseline_revision_id = str(baseline_release["revision_id"])
                if not await _compatibility_exists_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    revision_id=baseline_revision_id,
                ):
                    await self._compatibility_store.compile_and_store_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        revision_id=baseline_revision_id,
                        base_revision_id=None,
                        created_at=timestamp,
                    )
                compatibility = await self._compatibility_store.compile_and_store_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    revision_id=frozen.revision_id,
                    base_revision_id=baseline_revision_id,
                    created_at=timestamp,
                )
                policy = await self._eval_policy_store.resolve_for_prepare_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                    expected_revision_id=expected_eval_policy_revision_id,
                    created_at=timestamp,
                )
                plan, control = await self._eval_plan_store.create_initial_plan_conn(
                    db,
                    candidate=frozen.candidate,
                    policy=policy,
                    actor_user_id=actor_user_id,
                    workspace_id=workspace_id,
                    source_working_copy_id=expected_working_copy_id,
                    source_working_copy_version=expected_working_copy_version,
                    source_working_copy_hash=source_working_copy_hash,
                    created_at=timestamp,
                    preparation_actor=(
                        AgentPreparationActor("automation", automation.authority.principal_id, actor_user_id, automation.authority.grant_id)
                        if automation is not None
                        else None
                    ),
                )
                await self._write_audit_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    action=AuditAction.AGENT_RELEASE_CANDIDATE_PREPARED,
                    agent_id=agent_id,
                    payload={
                        "candidate_hash": frozen.candidate_hash,
                        "candidate_id": frozen.candidate_id,
                        "compatibility_major": compatibility.compatibility_major,
                        "compatibility_verdict": compatibility.verdict,
                        "plan_hash": plan.plan_hash,
                        "plan_id": plan.id,
                        **({"replaced_candidate_id": replaced_candidate_id} if replaced_candidate_id is not None else {}),
                    },
                )
                await self._enqueue_version_owner_event_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    owner_user_id=actor_user_id,
                    candidate_id=frozen.candidate_id,
                    plan_id=plan.id,
                    candidate_hash=frozen.candidate_hash,
                    plan_hash=plan.plan_hash,
                    timestamp=timestamp,
                )
                result = AgentPreparationResult(
                    state="prepared",
                    agent_id=agent_id,
                    candidate_id=frozen.candidate_id,
                    candidate_hash=frozen.candidate_hash,
                    candidate_row_version=frozen.candidate_row_version,
                    revision_id=frozen.revision_id,
                    compatibility_major=compatibility.compatibility_major,
                    compatibility_verdict=compatibility.verdict,
                    policy_revision_id=policy.id,
                    policy_hash=policy.policy_hash,
                    plan_id=plan.id,
                    plan_hash=plan.plan_hash,
                    control_row_version=control.row_version,
                    source_working_copy_hash=source_working_copy_hash,
                    aggregate_row_version=int(aggregate["row_version"]),
                )
                if automation is not None:
                    pinned = automation_grant["evaluation_workspace_id"] is not None
                    await db.execute(
                        """INSERT INTO agent_version_automation_preparation(team_id,agent_id,principal_id,grant_id,proposal_id,operation_key_hash,request_hash,context_hash,protocol_version,candidate_id,initial_plan_id,initial_plan_hash,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            team_id,
                            agent_id,
                            automation.authority.principal_id,
                            automation.authority.grant_id,
                            automation.proposal_id,
                            operation_key_hash(idempotency_key),
                            request_hash,
                            automation.expected_context_hash if pinned else None,
                            2 if pinned else 1,
                            result.candidate_id,
                            result.plan_id,
                            result.plan_hash,
                            timestamp,
                        ),
                    )
                if coach_delegation_id is not None:
                    from bowhead_core.coach_ci_delegation import bind_preparation_conn

                    await bind_preparation_conn(
                        db,
                        coach_delegation_id,
                        result,
                        accepted_working_copy_hash=_working_copy_hash(
                            working=working,
                            definition=working_definition,
                            listing=_decode_object(working["listing_json"], name="listing"),
                            audience=audience,
                        ),
                    )
                await self._store_preparation_receipt_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result=result,
                    timestamp=timestamp,
                )
                await db.commit()
                return result
            except (AgentCompatibilityError, AgentEvalPlanError, AgentEvalPolicyError) as exc:
                await db.rollback()
                raise _preparation_dependency_error(exc.code, details=getattr(exc, "details", None)) from exc
            except BaseException:
                await db.rollback()
                raise

    async def approve_candidate(
        self,
        *,
        team_id: str,
        candidate_id: str,
        actor_user_id: str,
        expected_row_version: int,
        expected_candidate_hash: str,
    ) -> AgentPublicationResult:
        timestamp = self._now()
        if self._eval_gate is not None:
            # Same ordering rule as publish: the reviewer check comes first, so a
            # non-reviewer still gets `agent_release_review_forbidden` rather than a
            # 500 out of the gate's own authorization.  Only a still-pending
            # candidate can be activated, so only that one needs a suite.
            async with open_shared_ctx(self.db_path) as probe:
                probe.row_factory = aiosqlite.Row
                if not await self._qualified_reviewer_conn(probe, team_id=team_id, actor_user_id=actor_user_id):
                    raise AgentRevisionForbiddenError("agent_release_review_forbidden")
                async with probe.execute(
                    "SELECT subagent_id FROM team_subagent_release_candidate WHERE team_id=? AND id=? AND state='pending'",
                    (team_id, candidate_id),
                ) as cursor:
                    candidate_row = await cursor.fetchone()
            if candidate_row is not None:
                await self._prepare_gate_for_activation(
                    team_id=team_id,
                    agent_id=str(candidate_row["subagent_id"]),
                    actor_user_id=actor_user_id,
                )
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            stale_error: AgentReleaseStaleError | None = None
            try:
                candidate = await self._candidate_conn(db, team_id=team_id, candidate_id=candidate_id)
                if not await self._qualified_reviewer_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                ):
                    raise AgentRevisionForbiddenError("agent_release_review_forbidden")
                if str(candidate["state"]) == "activated":
                    aggregate = await self._aggregate_conn(
                        db,
                        team_id=team_id,
                        agent_id=str(candidate["subagent_id"]),
                    )
                    result = self._activated_result(candidate, aggregate=aggregate)
                    await db.commit()
                    return result
                if str(candidate["state"]) != "pending":
                    raise AgentRevisionConflictError("agent_release_candidate_not_pending")
                self._require_candidate_cas(
                    candidate,
                    expected_row_version=expected_row_version,
                    expected_candidate_hash=expected_candidate_hash,
                )
                aggregate = await self._aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=str(candidate["subagent_id"]),
                )
                await self._require_revisioned_active(aggregate)
                if aggregate["live_release_id"] != candidate["base_live_release_id"]:
                    stale_error = await self._mark_stale_conn(
                        db,
                        aggregate=aggregate,
                        candidate=candidate,
                        actor_user_id=actor_user_id,
                        reason_codes=("base_release_changed",),
                        timestamp=timestamp,
                    )
                elif self._owner_id(aggregate) != str(candidate["submitted_by"]):
                    stale_error = await self._mark_stale_conn(
                        db,
                        aggregate=aggregate,
                        candidate=candidate,
                        actor_user_id=actor_user_id,
                        reason_codes=("agent_owner_changed",),
                        timestamp=timestamp,
                    )
                else:
                    await self._require_owner_active_conn(db, aggregate=aggregate)
                    revision = await self._revision_conn(db, candidate=candidate)
                    definition = _decode_object(revision["definition_json"], name="definition")
                    listing = _decode_object(revision["listing_json"], name="listing")
                    if _hash_text(str(revision["definition_json"])) != str(revision["definition_hash"]):
                        raise AgentPublicationBlockedError("agent_revision_integrity_failed")
                    audience = AgentAudience.from_mapping(
                        _decode_object(candidate["target_audience_json"], name="audience"),
                    )
                    await self._validate_audience_conn(db, team_id=team_id, audience=audience, agent_id=str(candidate["subagent_id"]))
                    validation = await self._validator.validate(
                        db,
                        team_id=team_id,
                        agent_id=str(candidate["subagent_id"]),
                        owner_user_id=self._owner_id(aggregate),
                        routing_name=str(aggregate["name"]),
                        definition=definition,
                        listing=listing,
                        audience=audience,
                    )
                    drift_reasons = await self._candidate_drift_reasons_conn(
                        db,
                        candidate=candidate,
                        definition=definition,
                        listing=listing,
                        audience=audience,
                        validation=validation,
                    )
                    if drift_reasons:
                        stale_error = await self._mark_stale_conn(
                            db,
                            aggregate=aggregate,
                            candidate=candidate,
                            actor_user_id=actor_user_id,
                            reason_codes=drift_reasons,
                            timestamp=timestamp,
                        )
                    else:
                        self._require_publishable(validation, audience=audience)
                        result = await self._activate_conn(
                            db,
                            aggregate=aggregate,
                            candidate=candidate,
                            definition=definition,
                            listing=listing,
                            audience=audience,
                            validation=validation,
                            actor_user_id=actor_user_id,
                            self_review=self._owner_id(aggregate) == actor_user_id,
                            decision_reason="admin_approved",
                            timestamp=timestamp,
                        )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        if stale_error is not None:
            raise stale_error
        return result

    async def reject_candidate(
        self,
        *,
        team_id: str,
        candidate_id: str,
        actor_user_id: str,
        expected_row_version: int,
        expected_candidate_hash: str,
        reason_code: str,
    ) -> AgentPublicationResult:
        if not isinstance(reason_code, str) or not _REASON_CODE_RE.fullmatch(reason_code):
            raise ValueError("candidate rejection reason code is invalid")
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                candidate = await self._candidate_conn(db, team_id=team_id, candidate_id=candidate_id)
                if not await self._qualified_reviewer_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                ):
                    raise AgentRevisionForbiddenError("agent_release_review_forbidden")
                aggregate = await self._aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=str(candidate["subagent_id"]),
                )
                if str(candidate["state"]) == "rejected":
                    await db.commit()
                    return self._terminal_result(candidate, "rejected", aggregate=aggregate)
                if str(candidate["state"]) != "pending":
                    raise AgentRevisionConflictError("agent_release_candidate_not_pending")
                self._require_candidate_cas(
                    candidate,
                    expected_row_version=expected_row_version,
                    expected_candidate_hash=expected_candidate_hash,
                )
                timestamp = self._now()
                updated = await db.execute(
                    """
                    UPDATE team_subagent_release_candidate
                    SET state='rejected',row_version=row_version+1,decided_by=?,
                        decision_reason_code=?,self_review=?,decided_at=?,updated_at=?
                    WHERE id=? AND state='pending' AND row_version=?
                    """,
                    (
                        actor_user_id,
                        reason_code,
                        int(self._owner_id(aggregate) == actor_user_id),
                        timestamp,
                        timestamp,
                        candidate_id,
                        expected_row_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentRevisionConflictError("agent_release_candidate_version_conflict")
                await self._name_store.release_names_conn(
                    db,
                    team_id=team_id,
                    agent_id=str(candidate["subagent_id"]),
                    candidate_id=candidate_id,
                    timestamp=timestamp,
                )
                event_id = await self._insert_event_conn(
                    db,
                    team_id=team_id,
                    agent_id=str(candidate["subagent_id"]),
                    candidate_id=candidate_id,
                    release_id=None,
                    event_type="candidate.rejected",
                    actor_user_id=actor_user_id,
                    payload={"reason_code": reason_code},
                    timestamp=timestamp,
                )
                await self._enqueue_owner_conn(
                    db,
                    team_id=team_id,
                    agent_id=str(candidate["subagent_id"]),
                    event_id=event_id,
                    notification_type="agent.release.rejected",
                    timestamp=timestamp,
                )
                await self._write_audit_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    action=AuditAction.AGENT_RELEASE_CANDIDATE_REJECTED,
                    agent_id=str(candidate["subagent_id"]),
                    payload={"candidate_id": candidate_id, "reason_code": reason_code},
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        refreshed = dict(candidate)
        refreshed["row_version"] = expected_row_version + 1
        return self._terminal_result(refreshed, "rejected", aggregate=aggregate)

    async def withdraw_candidate(
        self,
        *,
        team_id: str,
        candidate_id: str,
        actor_user_id: str,
        expected_row_version: int,
        expected_candidate_hash: str,
    ) -> AgentPublicationResult:
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                candidate = await self._candidate_conn(db, team_id=team_id, candidate_id=candidate_id)
                aggregate = await self._owned_aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=str(candidate["subagent_id"]),
                    actor_user_id=actor_user_id,
                )
                if str(candidate["state"]) == "withdrawn":
                    await db.commit()
                    return self._terminal_result(candidate, "withdrawn", aggregate=aggregate)
                await self._withdraw_conn(
                    db,
                    team_id=team_id,
                    candidate=candidate,
                    actor_user_id=actor_user_id,
                    expected_row_version=expected_row_version,
                    expected_candidate_hash=expected_candidate_hash,
                    timestamp=self._now(),
                    reason_code="owner_withdrawn",
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        refreshed = dict(candidate)
        refreshed["row_version"] = expected_row_version + 1
        return self._terminal_result(refreshed, "withdrawn", aggregate=aggregate)

    async def _withdraw_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        candidate: aiosqlite.Row,
        actor_user_id: str,
        expected_row_version: int,
        expected_candidate_hash: str,
        timestamp: str,
        reason_code: Literal["owner_withdrawn", "owner_replaced"],
    ) -> None:
        """Withdraw one prepared or pending candidate inside the caller's transaction.

        `owner_replaced` is the Owner's "Prepare from latest draft": the same transaction prepares the
        replacement, so the Owner is not notified of their own withdrawal.
        """
        candidate_id = str(candidate["id"])
        agent_id = str(candidate["subagent_id"])
        candidate_state = str(candidate["state"])
        if candidate_state not in {"prepared", "pending"}:
            raise AgentRevisionConflictError("agent_release_candidate_not_active")
        self._require_candidate_cas(
            candidate,
            expected_row_version=expected_row_version,
            expected_candidate_hash=expected_candidate_hash,
        )
        updated = await db.execute(
            """
            UPDATE team_subagent_release_candidate
            SET state='withdrawn',row_version=row_version+1,decided_by=?,
                decision_reason_code=?,self_review=1,
                decided_at=?,updated_at=?
            WHERE id=? AND state=? AND row_version=?
            """,
            (actor_user_id, reason_code, timestamp, timestamp, candidate_id, candidate_state, expected_row_version),
        )
        if updated.rowcount != 1:
            raise AgentRevisionConflictError("agent_release_candidate_version_conflict")
        await self._name_store.release_names_conn(db, team_id=team_id, agent_id=agent_id, candidate_id=candidate_id, timestamp=timestamp)
        event_id = await self._insert_event_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
            release_id=None,
            event_type="candidate.withdrawn",
            actor_user_id=actor_user_id,
            payload={"reason_code": reason_code},
            timestamp=timestamp,
        )
        if reason_code == "owner_withdrawn":
            await self._enqueue_owner_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                event_id=event_id,
                notification_type="agent.release.withdrawn",
                timestamp=timestamp,
            )
        await self._write_audit_conn(
            db,
            team_id=team_id,
            actor_user_id=actor_user_id,
            action=AuditAction.AGENT_RELEASE_CANDIDATE_WITHDRAWN,
            agent_id=agent_id,
            payload={"candidate_id": candidate_id, "reason_code": reason_code},
        )

    async def create_rollback_working_copy(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        source_release_id: str,
        expected_aggregate_row_version: int,
        expected_working_copy_version: int | None,
    ) -> AgentWorkingCopy:
        """Copy an old definition into a new draft without restoring old access.

        The draft always keeps the current live Audience.  Publishing it still
        recompiles current dependencies and follows the ordinary approval path.
        """

        timestamp = self._now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                aggregate = await self._owned_aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                await self._require_revisioned_active(aggregate)
                if int(aggregate["row_version"]) != expected_aggregate_row_version:
                    raise AgentRevisionConflictError("agent_aggregate_version_conflict")
                source_release = await self._release_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    release_id=source_release_id,
                )
                source_revision = await self._revision_for_release_conn(
                    db,
                    release=source_release,
                )
                live_release = await self._release_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    release_id=str(aggregate["live_release_id"]),
                )
                definition = _decode_object(
                    source_revision["definition_json"],
                    name="definition",
                )
                if "capability_ids" in definition:
                    async with db.execute("SELECT id FROM team_capability WHERE team_id=?", (team_id,)) as cursor:
                        available = {str(row[0]) for row in await cursor.fetchall()}
                    definition["capability_ids"] = [item for item in selected_plugin_ids(definition) if item in available]
                listing = _decode_object(
                    source_revision["listing_json"],
                    name="listing",
                )
                audience = AgentAudience.from_mapping(
                    _decode_object(live_release["audience_snapshot_json"], name="audience"),
                )
                validation_json = _canonical_json(
                    {
                        "change_kind": "rollback",
                        "rollback_source_release_id": source_release_id,
                        "status": "not_validated",
                    },
                )
                existing = await self._working_copy_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                )
                if existing is None:
                    if expected_working_copy_version is not None:
                        raise AgentRevisionConflictError(
                            "agent_working_copy_version_conflict",
                        )
                    working_copy_id = _id("awc")
                    row_version = 1
                    await db.execute(
                        """
                        INSERT INTO team_subagent_working_copy(
                          id,team_id,subagent_id,base_release_id,definition_json,listing_json,
                          audience_json,validation_json,row_version,updated_by,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)
                        """,
                        (
                            working_copy_id,
                            team_id,
                            agent_id,
                            aggregate["live_release_id"],
                            _canonical_json(definition),
                            str(source_revision["listing_json"]),
                            str(live_release["audience_snapshot_json"]),
                            validation_json,
                            actor_user_id,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    current_version = int(existing["row_version"])
                    if expected_working_copy_version != current_version:
                        existing_validation = _decode_object(
                            existing["validation_json"],
                            name="working_copy_validation",
                        )
                        is_network_replay = (
                            expected_working_copy_version is None
                            and existing_validation.get("change_kind") == "rollback"
                            and existing_validation.get("rollback_source_release_id") == source_release_id
                            and existing["base_release_id"] == aggregate["live_release_id"]
                        )
                        if not is_network_replay:
                            raise AgentRevisionConflictError(
                                "agent_working_copy_version_conflict",
                            )
                        await db.commit()
                        return _working_copy_from_row(existing)
                    working_copy_id = str(existing["id"])
                    row_version = current_version + 1
                    updated = await db.execute(
                        """
                        UPDATE team_subagent_working_copy
                        SET base_release_id=?,definition_json=?,listing_json=?,audience_json=?,
                            validation_json=?,row_version=?,updated_by=?,updated_at=?
                        WHERE id=? AND row_version=?
                        """,
                        (
                            aggregate["live_release_id"],
                            _canonical_json(definition),
                            str(source_revision["listing_json"]),
                            str(live_release["audience_snapshot_json"]),
                            validation_json,
                            row_version,
                            actor_user_id,
                            timestamp,
                            working_copy_id,
                            current_version,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise AgentRevisionConflictError(
                            "agent_working_copy_version_conflict",
                        )
                    await invalidate_working_copy_projection_conn(
                        db,
                        team_id=team_id,
                        working_copy_id=working_copy_id,
                    )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return AgentWorkingCopy(
            id=working_copy_id,
            team_id=team_id,
            agent_id=agent_id,
            base_release_id=str(aggregate["live_release_id"]),
            definition=definition,
            listing=listing,
            audience=audience,
            row_version=row_version,
            updated_at=timestamp,
        )

    async def archive_agent(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_aggregate_row_version: int,
    ) -> AgentLifecycleResult:
        """Archive one Agent without deleting its immutable release history."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                aggregate = await self._owned_aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                if str(aggregate["definition_mode"]) != "revisioned":
                    raise AgentRevisionConflictError("agent_release_baseline_required")
                if int(aggregate["row_version"]) != expected_aggregate_row_version:
                    raise AgentRevisionConflictError("agent_aggregate_version_conflict")
                if str(aggregate["lifecycle_state"] or "active") == "archived":
                    await db.commit()
                    return AgentLifecycleResult(
                        state="archived",
                        agent_id=agent_id,
                        aggregate_row_version=expected_aggregate_row_version,
                        paused_schedule_count=0,
                        pending_candidate_withdrawn=False,
                    )
                if str(aggregate["lifecycle_state"] or "active") != "active":
                    raise AgentRevisionConflictError("agent_lifecycle_not_active")

                timestamp = self._now()
                # Archive releases every open candidate, not only `pending`: a `prepared` candidate
                # would otherwise keep the active-candidate slot after restore, blocking a new
                # preparation with `already_active` or letting the Owner activate a stale snapshot.
                pending = await self._withdrawable_candidate_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                )
                pending_withdrawn = pending is not None
                if pending is not None:
                    updated_candidate = await db.execute(
                        """
                        UPDATE team_subagent_release_candidate
                        SET state='withdrawn',row_version=row_version+1,decided_by=?,
                            decision_reason_code='agent_archived',self_review=1,
                            decided_at=?,updated_at=?
                        WHERE id=? AND state=? AND row_version=?
                        """,
                        (
                            actor_user_id,
                            timestamp,
                            timestamp,
                            str(pending["id"]),
                            str(pending["state"]),
                            int(pending["row_version"]),
                        ),
                    )
                    if updated_candidate.rowcount != 1:
                        raise AgentRevisionConflictError(
                            "agent_release_candidate_version_conflict",
                        )
                    await self._name_store.release_names_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        candidate_id=str(pending["id"]),
                        timestamp=timestamp,
                    )
                    event_id = await self._insert_event_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        candidate_id=str(pending["id"]),
                        release_id=None,
                        event_type="candidate.withdrawn",
                        actor_user_id=actor_user_id,
                        payload={"reason_code": "agent_archived"},
                        timestamp=timestamp,
                    )
                    await self._enqueue_owner_conn(
                        db,
                        team_id=team_id,
                        agent_id=agent_id,
                        event_id=event_id,
                        notification_type="agent.release.withdrawn",
                        timestamp=timestamp,
                    )
                    await self._write_audit_conn(
                        db,
                        team_id=team_id,
                        actor_user_id=actor_user_id,
                        action=AuditAction.AGENT_RELEASE_CANDIDATE_WITHDRAWN,
                        agent_id=agent_id,
                        payload={
                            "candidate_id": str(pending["id"]),
                            "reason_code": "agent_archived",
                        },
                    )

                paused_count = await self._pause_agent_schedules_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    routing_name=str(aggregate["name"]),
                    timestamp=timestamp,
                )
                revoked = await self._access_store._revoke_grants_for_resource_conn(
                    db,
                    team_id,
                    "team_subagent",
                    agent_id,
                    effects=("allow",),
                    subagent_transaction_token=self._transaction_token,
                )
                if revoked:
                    await self._access_store._bump_policy_epoch_conn(db, team_id)
                await db.execute(
                    """
                    UPDATE agent_assignment
                    SET access_state='revoked',access_state_changed_at=?,
                        row_version=row_version+1,updated_at=?
                    WHERE team_id=? AND agent_id=? AND personal_state<>'removed'
                      AND access_state<>'revoked'
                    """,
                    (timestamp, timestamp, team_id, agent_id),
                )
                await db.execute(
                    """
                    UPDATE agent_default_policy
                    SET enabled=0,policy_version=policy_version+1,row_version=row_version+1,
                        changed_by=?,updated_at=?
                    WHERE team_id=? AND agent_id=? AND enabled=1
                    """,
                    (actor_user_id, timestamp, team_id, agent_id),
                )
                await self._name_store.release_agent_names_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    timestamp=timestamp,
                )
                updated = await db.execute(
                    """
                    UPDATE team_subagent
                    SET status='disabled',lifecycle_state='archived',archived_at=?,archived_by=?,
                        row_version=row_version+1,updated_at=?
                    WHERE team_id=? AND id=? AND row_version=? AND lifecycle_state='active'
                    """,
                    (
                        timestamp,
                        actor_user_id,
                        timestamp,
                        team_id,
                        agent_id,
                        expected_aggregate_row_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentRevisionConflictError("agent_aggregate_version_conflict")
                await db.execute(
                    "DELETE FROM agent_catalog_projection WHERE team_id=? AND agent_id=?",
                    (team_id, agent_id),
                )
                await self._write_audit_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    action=AuditAction.AGENT_ARCHIVED,
                    agent_id=agent_id,
                    payload={
                        "paused_schedule_count": paused_count,
                        "pending_candidate_withdrawn": pending_withdrawn,
                        "revoked_allow_grant_count": len(revoked),
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return AgentLifecycleResult(
            state="archived",
            agent_id=agent_id,
            aggregate_row_version=expected_aggregate_row_version + 1,
            paused_schedule_count=paused_count,
            pending_candidate_withdrawn=pending_withdrawn,
        )

    async def restore_agent(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_aggregate_row_version: int,
    ) -> AgentLifecycleResult:
        """Restore the same live release after current-time safety revalidation."""
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                aggregate = await self._owned_aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                if str(aggregate["definition_mode"]) != "revisioned":
                    raise AgentRevisionConflictError("agent_release_baseline_required")
                if int(aggregate["row_version"]) != expected_aggregate_row_version:
                    raise AgentRevisionConflictError("agent_aggregate_version_conflict")
                if str(aggregate["lifecycle_state"] or "active") == "active":
                    await db.commit()
                    return AgentLifecycleResult(
                        state="active",
                        agent_id=agent_id,
                        aggregate_row_version=expected_aggregate_row_version,
                        paused_schedule_count=0,
                        pending_candidate_withdrawn=False,
                    )
                if str(aggregate["lifecycle_state"] or "active") != "archived":
                    raise AgentRevisionConflictError("agent_lifecycle_not_archived")
                release_id = str(aggregate["live_release_id"] or "")
                if not release_id:
                    raise AgentRevisionConflictError("agent_live_release_missing")
                release = await self._release_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    release_id=release_id,
                )
                revision = await self._revision_for_release_conn(db, release=release)
                raw_definition = _decode_object(
                    revision["definition_json"],
                    name="definition",
                )
                definition = await self._normalize_definition_for_revalidation(
                    raw_definition,
                    team_id=team_id,
                )
                listing = _decode_object(revision["listing_json"], name="listing")
                audience = AgentAudience.from_release_snapshot(
                    _decode_object(release["audience_snapshot_json"], name="audience"),
                )
                await self._validate_audience_conn(db, team_id=team_id, audience=audience, agent_id=agent_id)
                validation = await self._validator.validate(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    owner_user_id=actor_user_id,
                    routing_name=str(aggregate["name"]),
                    definition=definition,
                    listing=listing,
                    audience=audience,
                )
                self._require_publishable(validation, audience=audience)

                timestamp = self._now()
                names = _names_from_listing(listing)
                from bowhead_core.team_subagent_store import TeamSubagentStore

                await TeamSubagentStore(self.db_path).claim_effective_name_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    display_name=names["*"],
                    timestamp=timestamp,
                )
                await self._name_store.claim_agent_names_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    names_by_locale=names,
                    timestamp=timestamp,
                )
                await self._replace_audience_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    audience=audience,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                )
                await self._refresh_assignment_access_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    owner_user_id=actor_user_id,
                    timestamp=timestamp,
                )
                updated = await db.execute(
                    """
                    UPDATE team_subagent
                    SET status='enabled',lifecycle_state='active',archived_at=NULL,archived_by=NULL,
                        audience_mode=?,visibility=?,row_version=row_version+1,updated_at=?
                    WHERE team_id=? AND id=? AND row_version=? AND lifecycle_state='archived'
                    """,
                    (
                        audience.mode,
                        "team_wide" if audience.mode == "teamwide" else "creator_only",
                        timestamp,
                        team_id,
                        agent_id,
                        expected_aggregate_row_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentRevisionConflictError("agent_aggregate_version_conflict")
                await db.execute(
                    "DELETE FROM agent_catalog_projection WHERE team_id=? AND agent_id=?",
                    (team_id, agent_id),
                )
                await self._write_audit_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    action=AuditAction.AGENT_RESTORED,
                    agent_id=agent_id,
                    payload={
                        "audience_mode": audience.mode,
                        "release_id": release_id,
                        "schedules_resumed": 0,
                    },
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return AgentLifecycleResult(
            state="active",
            agent_id=agent_id,
            aggregate_row_version=expected_aggregate_row_version + 1,
            paused_schedule_count=0,
            pending_candidate_withdrawn=False,
        )

    async def contract_audience(
        self,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
        expected_aggregate_row_version: int,
        audience: AgentAudience,
        idempotency_key: str,
        reason_code: str,
    ) -> AgentPublicationResult:
        _validate_idempotency_key(idempotency_key)
        if not isinstance(reason_code, str) or not _REASON_CODE_RE.fullmatch(reason_code):
            raise ValueError("audience contraction reason code is invalid")
        request_hash = _hash_json(
            {
                "agent_id": agent_id,
                "expected_aggregate_row_version": expected_aggregate_row_version,
                "audience": audience.to_dict(),
                "reason_code": reason_code,
            },
        )
        scope = f"agent.contract-audience:{agent_id}"
        timestamp = self._now()
        async with open_shared_ctx(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            try:
                aggregate = await self._owned_aggregate_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    actor_user_id=actor_user_id,
                )
                replay = await self._receipt_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    await db.commit()
                    return replay
                await self._require_revisioned_active(aggregate)
                if int(aggregate["row_version"]) != expected_aggregate_row_version:
                    raise AgentRevisionConflictError("agent_aggregate_version_conflict")
                await self._validate_audience_conn(db, team_id=team_id, audience=audience, agent_id=agent_id)
                current_release = await self._release_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    release_id=str(aggregate["live_release_id"]),
                )
                current_audience = AgentAudience.from_mapping(
                    _decode_object(current_release["audience_snapshot_json"], name="audience"),
                )
                if not _is_strict_audience_contraction(current_audience, audience):
                    raise AgentPublicationBlockedError("audience_change_is_not_strict_contraction")
                release_id = _id("arl")
                release_sequence = next_release_sequence(int(aggregate["release_sequence"]))
                await db.execute(
                    """
                    INSERT INTO team_subagent_release(
                      id,team_id,subagent_id,release_sequence,revision_id,
                      predecessor_release_id,source_candidate_id,audience_snapshot_json,
                      audience_hash,capability_fingerprint,dependency_fingerprint,risk_json,
                      safety_report_id,eval_evidence_json,release_note,activated_by,activated_at
                    ) VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        release_id,
                        team_id,
                        agent_id,
                        release_sequence,
                        str(current_release["revision_id"]),
                        str(current_release["id"]),
                        _canonical_json(audience.to_dict()),
                        _hash_json(audience.to_dict()),
                        str(current_release["capability_fingerprint"]),
                        str(current_release["dependency_fingerprint"]),
                        str(current_release["risk_json"]),
                        str(current_release["safety_report_id"]),
                        current_release["eval_evidence_json"],
                        reason_code,
                        actor_user_id,
                        timestamp,
                    ),
                )
                await self._replace_audience_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    audience=audience,
                    actor_user_id=actor_user_id,
                    timestamp=timestamp,
                )
                event_id = await self._insert_event_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    candidate_id=None,
                    release_id=release_id,
                    event_type="audience.contracted",
                    actor_user_id=actor_user_id,
                    payload={
                        "from_mode": current_audience.mode,
                        "reason_code": reason_code,
                        "to_mode": audience.mode,
                    },
                    timestamp=timestamp,
                )
                updated = await db.execute(
                    """
                    UPDATE team_subagent
                    SET live_release_id=?,release_sequence=?,last_release_event_id=?,
                        audience_mode=?,visibility=?,row_version=row_version+1,updated_at=?
                    WHERE id=? AND team_id=? AND row_version=? AND live_release_id=?
                    """,
                    (
                        release_id,
                        release_sequence,
                        event_id,
                        audience.mode,
                        "team_wide" if audience.mode == "teamwide" else "creator_only",
                        timestamp,
                        agent_id,
                        team_id,
                        expected_aggregate_row_version,
                        str(current_release["id"]),
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentRevisionConflictError("agent_aggregate_version_conflict")
                await db.execute(
                    "DELETE FROM agent_catalog_projection WHERE team_id=? AND agent_id=?",
                    (team_id, agent_id),
                )
                await self._enqueue_assignments_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    event_id=event_id,
                    notification_type="agent.release.audience_contracted",
                    timestamp=timestamp,
                )
                await self._enqueue_owner_conn(
                    db,
                    team_id=team_id,
                    agent_id=agent_id,
                    event_id=event_id,
                    notification_type="agent.release.audience_contracted",
                    timestamp=timestamp,
                )
                await self._write_audit_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    action=AuditAction.AGENT_AUDIENCE_CONTRACTED,
                    agent_id=agent_id,
                    payload={
                        "from_mode": current_audience.mode,
                        "reason_code": reason_code,
                        "release_id": release_id,
                        "to_mode": audience.mode,
                    },
                )
                result = AgentPublicationResult(
                    state="activated",
                    agent_id=agent_id,
                    candidate_id=None,
                    revision_id=str(current_release["revision_id"]),
                    release_id=release_id,
                    candidate_row_version=None,
                    aggregate_row_version=expected_aggregate_row_version + 1,
                )
                await self._store_receipt_conn(
                    db,
                    team_id=team_id,
                    actor_user_id=actor_user_id,
                    scope=scope,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result=result,
                    timestamp=timestamp,
                )
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    async def _freeze_candidate_conn(
        self,
        db: aiosqlite.Connection,
        *,
        aggregate: aiosqlite.Row,
        working: aiosqlite.Row,
        definition: dict[str, Any],
        listing: dict[str, Any],
        audience: AgentAudience,
        validation: AgentPublicationValidation,
        actor_user_id: str,
        release_note: str | None,
        eval_json: str | None,
        timestamp: str,
        initial_state: Literal["prepared", "pending"] = "pending",
    ) -> _FrozenCandidate:
        team_id = str(aggregate["team_id"])
        agent_id = str(aggregate["id"])
        definition_json = _canonical_json(definition)
        listing_json = _canonical_json(listing)
        audience_json = _canonical_json(audience.to_dict())
        definition_hash = _hash_text(definition_json)
        content_hash = _hash_json({"definition": definition, "listing": listing})
        risk_json = _canonical_json(validation.compilation.risk.to_dict())
        deterministic_json = _canonical_json(validation.deterministic_dict())
        semantic_json = _canonical_json(validation.semantic_dict())
        existing = await self._active_candidate_conn(db, team_id=team_id, agent_id=agent_id)
        revision_id = str(existing["revision_id"]) if existing is not None else _id("arv")
        behavior = _compile_behavior_descriptor(
            materializer=self._behavior_materializer,
            revision_id=revision_id,
            definition=definition,
            compilation=validation.compilation,
        )
        _require_behavior_publishable(definition=definition, descriptor=behavior)
        candidate_hash = _candidate_hash(
            base_live_release_id=aggregate["live_release_id"],
            audience=audience,
            behavior=behavior,
            content_hash=content_hash,
            validation=validation,
            eval_evidence=json.loads(eval_json) if eval_json else None,
            release_note=release_note,
        )
        if existing is not None:
            if str(existing["state"]) != initial_state:
                raise candidate_already_active_error(existing)
            if str(existing["candidate_hash"]) != candidate_hash:
                code = "agent_release_candidate_already_pending" if initial_state == "pending" else "agent_release_candidate_already_active"
                raise candidate_already_active_error(existing, code=code)
            return _FrozenCandidate.from_row(existing, candidate_hash=candidate_hash)

        candidate_id = _id("arc")
        report_id = _id("asr")
        async with db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM team_subagent_revision WHERE subagent_id=?",
            (agent_id,),
        ) as cursor:
            sequence_row = await cursor.fetchone()
        revision_sequence = int(sequence_row[0])
        working_validation = _decode_object(
            working["validation_json"],
            name="working_copy_validation",
        )
        revision_kind = "rollback" if working_validation.get("change_kind") == "rollback" else "authored"
        await require_extension_policy_write(db, definition, audience_mode=audience.mode)
        await db.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
              definition_hash,dependency_fingerprint,capability_fingerprint,
              risk_compiler_version,risk_json,created_by,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                revision_id,
                team_id,
                agent_id,
                revision_sequence,
                revision_kind,
                definition_json,
                listing_json,
                definition_hash,
                validation.compilation.dependency_fingerprint,
                validation.compilation.capability_fingerprint,
                validation.compilation.risk.compiler_version,
                risk_json,
                actor_user_id,
                timestamp,
            ),
        )
        await db.executemany(
            "INSERT INTO team_subagent_capability_binding(team_id,subagent_id,revision_id,capability_id) VALUES (?,?,?,?)",
            [(team_id, agent_id, revision_id, plugin_id) for plugin_id in selected_plugin_ids(definition)],
        )
        await retain_extension_policy_in_connection(
            db, team_id=team_id, agent_id=agent_id, revision_ref=revision_id, definition=definition, audience_mode=audience.mode, created_at=timestamp
        )
        await self._behavior_materializer.persist_team_revision_conn(
            db,
            revision_id=revision_id,
            team_id=team_id,
            subagent_id=agent_id,
            descriptor=behavior,
            created_at=timestamp,
        )
        projection, edges = edges_from_compilation(
            team_id=team_id,
            subject_kind="agent_revision",
            subject_ref=revision_id,
            agent_id=agent_id,
            subject_fingerprint=definition_hash,
            compilation=validation.compilation,
        )
        await replace_subject_projection_conn(
            db,
            projection=projection,
            edges=edges,
            projected_at=timestamp,
        )
        await db.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,status,
              deterministic_json,semantic_json,restricted_evidence_ref,safe_summary_json,
              created_by,created_at
            ) VALUES (?,?,?,'revision',?,?,?,?,?,?,?,?,NULL,?,?,?)
            """,
            (
                report_id,
                team_id,
                agent_id,
                revision_id,
                content_hash,
                validation.compilation.dependency_fingerprint,
                validation.scanner_version,
                validation.semantic_scanner_version,
                validation.safety_status,
                deterministic_json,
                semantic_json,
                _canonical_json(validation.safe_summary_dict()),
                actor_user_id,
                timestamp,
            ),
        )
        from bowhead_core.release_plugin_versions import plugin_versions_conn

        # Plugins float after release; freeze the versions this candidate was validated with.
        plugin_versions_json = await plugin_versions_conn(db, team_id=team_id, definition=definition)
        await db.execute(
            """
            INSERT INTO team_subagent_release_candidate(
              id,team_id,subagent_id,revision_id,base_live_release_id,
              target_audience_json,target_audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_compiler_version,risk_json,safety_report_id,
              eval_evidence_json,release_note,candidate_hash,state,row_version,
              submitted_by,submitted_at,updated_at,plugin_versions_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)
            """,
            (
                candidate_id,
                team_id,
                agent_id,
                revision_id,
                aggregate["live_release_id"],
                audience_json,
                _hash_text(audience_json),
                validation.compilation.capability_fingerprint,
                validation.compilation.dependency_fingerprint,
                validation.compilation.risk.compiler_version,
                risk_json,
                report_id,
                eval_json,
                release_note,
                candidate_hash,
                initial_state,
                actor_user_id,
                timestamp,
                timestamp,
                plugin_versions_json,
            ),
        )
        await self._name_store.reserve_names_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
            names_by_locale=_names_from_listing(listing),
            timestamp=timestamp,
        )
        candidate = await self._candidate_conn(db, team_id=team_id, candidate_id=candidate_id)
        return _FrozenCandidate(
            candidate=candidate,
            candidate_id=candidate_id,
            revision_id=revision_id,
            candidate_hash=candidate_hash,
            state=initial_state,
            candidate_row_version=1,
            created=True,
            behavior_fingerprint=behavior.behavior_fingerprint,
            primary_capability_ref=behavior.primary_capability_ref,
        )

    async def _activate_conn(
        self,
        db: aiosqlite.Connection,
        *,
        aggregate: aiosqlite.Row,
        candidate: aiosqlite.Row,
        definition: dict[str, Any],
        listing: dict[str, Any],
        audience: AgentAudience,
        validation: AgentPublicationValidation,
        actor_user_id: str,
        self_review: bool,
        decision_reason: str,
        timestamp: str,
        candidate_from_state: Literal["prepared", "pending"] = "pending",
        eval_evidence_override: Mapping[str, Any] | None | object = _USE_PUBLICATION_EVAL_GATE,
        release_note: str | None = None,
    ) -> AgentPublicationResult:
        """Activate the candidate as the next release.

        `release_note` is the Owner's note given at release ("Release as v2"); it is stored on the release
        only. The candidate keeps the note it was prepared with, because that note is part of the candidate
        hash the evaluation evidence pins. Without one, the release takes the candidate's note.
        """
        candidate_id = str(candidate["id"])
        team_id = str(candidate["team_id"])
        agent_id = str(candidate["subagent_id"])
        await self._validate_knowledge_write(db, definition, team_id=team_id, actor_user_id=actor_user_id)
        current_candidate_version = int(candidate["row_version"])
        eval_evidence = (
            await self._eval_gate.evidence_for_activation(db, candidate=candidate, audience=audience)
            if eval_evidence_override is _USE_PUBLICATION_EVAL_GATE and self._eval_gate is not None
            else (None if eval_evidence_override is _USE_PUBLICATION_EVAL_GATE else eval_evidence_override)
        )
        eval_evidence_json = _canonical_json(eval_evidence) if eval_evidence is not None else candidate["eval_evidence_json"]
        approved = await db.execute(
            """
            UPDATE team_subagent_release_candidate
            SET state='approved',row_version=row_version+1,decided_by=?,decision_reason_code=?,
                self_review=?,decided_at=?,updated_at=?
            WHERE id=? AND state=? AND row_version=?
            """,
            (
                actor_user_id,
                decision_reason,
                int(self_review),
                timestamp,
                timestamp,
                candidate_id,
                candidate_from_state,
                current_candidate_version,
            ),
        )
        if approved.rowcount != 1:
            raise AgentRevisionConflictError("agent_release_candidate_version_conflict")
        await self._name_store.activate_names_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
            timestamp=timestamp,
        )
        release_id = _id("arl")
        release_sequence = next_release_sequence(int(aggregate["release_sequence"]))
        await db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,predecessor_release_id,
              source_candidate_id,audience_snapshot_json,audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_json,safety_report_id,eval_evidence_json,
              release_note,activated_by,activated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                release_id,
                team_id,
                agent_id,
                release_sequence,
                str(candidate["revision_id"]),
                candidate["base_live_release_id"],
                candidate_id,
                str(candidate["target_audience_json"]),
                str(candidate["target_audience_hash"]),
                validation.compilation.capability_fingerprint,
                validation.compilation.dependency_fingerprint,
                str(candidate["risk_json"]),
                str(candidate["safety_report_id"]),
                eval_evidence_json,
                release_note if release_note is not None else candidate["release_note"],
                actor_user_id,
                timestamp,
            ),
        )
        activated = await db.execute(
            """
            UPDATE team_subagent_release_candidate
            SET state='activated',row_version=row_version+1,activated_release_id=?,updated_at=?
            WHERE id=? AND state='approved' AND row_version=?
            """,
            (release_id, timestamp, candidate_id, current_candidate_version + 1),
        )
        if activated.rowcount != 1:
            raise AgentRevisionConflictError("agent_release_candidate_version_conflict")
        event_id = await self._insert_event_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            candidate_id=candidate_id,
            release_id=release_id,
            event_type="release.activated",
            actor_user_id=actor_user_id,
            payload={
                "audience_mode": audience.mode,
                "release_sequence": release_sequence,
                "risk_level": validation.compilation.risk.level,
            },
            timestamp=timestamp,
        )
        await self._project_live_conn(
            db,
            aggregate=aggregate,
            definition=definition,
            listing=listing,
            audience=audience,
            revision_id=str(candidate["revision_id"]),
            release_id=release_id,
            release_sequence=release_sequence,
            event_id=event_id,
            actor_user_id=actor_user_id,
            timestamp=timestamp,
        )
        # Personal tools are owner-scoped and separate from the shared definition.
        # Preserve the owner's explicit selection only when the new revision has
        # exactly the same definition and listing as the previous release.
        if aggregate["live_revision_id"] is not None:
            await db.execute(
                """
                INSERT OR IGNORE INTO personal_mcp_binding(agent_id,revision_id,owner_user_id,tool_ids_json,version)
                SELECT binding.agent_id,new_revision.id,binding.owner_user_id,binding.tool_ids_json,1
                FROM personal_mcp_binding binding
                JOIN team_subagent_revision old_revision
                  ON old_revision.id=binding.revision_id AND old_revision.subagent_id=binding.agent_id
                JOIN team_subagent_revision new_revision
                  ON new_revision.id=? AND new_revision.subagent_id=binding.agent_id
                WHERE binding.agent_id=? AND binding.revision_id=? AND binding.owner_user_id=?
                  AND old_revision.definition_hash=new_revision.definition_hash
                  AND old_revision.listing_json=new_revision.listing_json
                """,
                (str(candidate["revision_id"]), agent_id, str(aggregate["live_revision_id"]), self._owner_id(aggregate)),
            )
        if audience.mode != "private":
            await self._enqueue_assignments_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                event_id=event_id,
                notification_type=("agent.release.high_risk" if validation.compilation.risk.level == "high" else "agent.release.updated"),
                timestamp=timestamp,
            )
            await self._enqueue_owner_conn(
                db,
                team_id=team_id,
                agent_id=agent_id,
                event_id=event_id,
                notification_type=("agent.release.high_risk" if validation.compilation.risk.level == "high" else "agent.release.updated"),
                timestamp=timestamp,
            )
        await self._reconcile_working_copy_after_activation_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            previous_release_id=(str(candidate["base_live_release_id"]) if candidate["base_live_release_id"] is not None else None),
            activated_release_id=release_id,
            definition=definition,
            listing=listing,
            audience=audience,
            timestamp=timestamp,
        )
        await self._write_audit_conn(
            db,
            team_id=team_id,
            actor_user_id=actor_user_id,
            action=AuditAction.AGENT_RELEASE_ACTIVATED,
            agent_id=agent_id,
            payload={
                "candidate_id": candidate_id,
                "release_id": release_id,
                "release_sequence": release_sequence,
                "self_review": self_review,
            },
        )
        return AgentPublicationResult(
            state="activated",
            agent_id=agent_id,
            candidate_id=candidate_id,
            revision_id=str(candidate["revision_id"]),
            release_id=release_id,
            candidate_row_version=current_candidate_version + 2,
            aggregate_row_version=int(aggregate["row_version"]) + 1,
        )

    async def _mark_stale_conn(
        self,
        db: aiosqlite.Connection,
        *,
        aggregate: aiosqlite.Row,
        candidate: aiosqlite.Row,
        actor_user_id: str,
        reason_codes: tuple[str, ...],
        timestamp: str,
    ) -> AgentReleaseStaleError:
        # The Owner-eval flow detects drift at submit and at private activation, while the candidate
        # is still `prepared`; the legacy flow only ever had `pending`. Matching `pending` alone
        # turned every drifted submit/activation into a version-conflict 409 that rolled back, so
        # the candidate was never marked stale and the Owner was never told to prepare again.
        candidate_id = str(candidate["id"])
        primary_reason = reason_codes[0]
        updated = await db.execute(
            """
            UPDATE team_subagent_release_candidate
            SET state='stale',row_version=row_version+1,decided_by=?,decision_reason_code=?,
                self_review=?,decided_at=?,updated_at=?
            WHERE id=? AND state IN ('prepared','pending') AND row_version=?
            """,
            (
                actor_user_id,
                primary_reason,
                int(self._owner_id(aggregate) == actor_user_id),
                timestamp,
                timestamp,
                candidate_id,
                int(candidate["row_version"]),
            ),
        )
        if updated.rowcount != 1:
            raise AgentRevisionConflictError("agent_release_candidate_version_conflict")
        await self._name_store.release_names_conn(
            db,
            team_id=str(candidate["team_id"]),
            agent_id=str(candidate["subagent_id"]),
            candidate_id=candidate_id,
            timestamp=timestamp,
        )
        event_id = await self._insert_event_conn(
            db,
            team_id=str(candidate["team_id"]),
            agent_id=str(candidate["subagent_id"]),
            candidate_id=candidate_id,
            release_id=None,
            event_type="candidate.stale",
            actor_user_id=actor_user_id,
            payload={"reason_codes": list(reason_codes)},
            timestamp=timestamp,
        )
        await self._enqueue_owner_conn(
            db,
            team_id=str(candidate["team_id"]),
            agent_id=str(candidate["subagent_id"]),
            event_id=event_id,
            notification_type="agent.release.stale",
            timestamp=timestamp,
        )
        await self._write_audit_conn(
            db,
            team_id=str(candidate["team_id"]),
            actor_user_id=actor_user_id,
            action=AuditAction.AGENT_RELEASE_CANDIDATE_STALE,
            agent_id=str(candidate["subagent_id"]),
            payload={"candidate_id": candidate_id, "reason_codes": list(reason_codes)},
        )
        return AgentReleaseStaleError(candidate_id, reason_codes=reason_codes)

    async def _candidate_drift_reasons_conn(
        self,
        db: aiosqlite.Connection,
        *,
        candidate: aiosqlite.Row,
        definition: dict[str, Any],
        listing: dict[str, Any],
        audience: AgentAudience,
        validation: AgentPublicationValidation,
    ) -> tuple[str, ...]:
        reasons: set[str] = set()
        from bowhead_core.agent_runtime_extension_store import read_published_extension_policy_in_connection
        from bowhead_core.agent_runtime_extensions import AgentRuntimeExtensionError

        try:
            await read_published_extension_policy_in_connection(
                db,
                team_id=str(candidate["team_id"]),
                agent_id=str(candidate["subagent_id"]),
                revision_ref=str(candidate["revision_id"]),
                definition=definition,
            )
        except AgentRuntimeExtensionError:
            reasons.add("agent_runtime_extension_unproven")
        behavior: AgentBehaviorDescriptor | None = None
        async with db.execute(
            """
            SELECT descriptor_json
            FROM team_subagent_revision_behavior
            WHERE revision_id=? AND team_id=? AND subagent_id=?
            """,
            (
                candidate["revision_id"],
                candidate["team_id"],
                candidate["subagent_id"],
            ),
        ) as cursor:
            behavior_row = await cursor.fetchone()
        if behavior_row is None:
            reasons.add("behavior_descriptor_missing")
        else:
            try:
                behavior = AgentBehaviorDescriptor.model_validate_json(
                    str(behavior_row["descriptor_json"]),
                )
                expected_behavior = _compile_behavior_descriptor(
                    materializer=self._behavior_materializer,
                    revision_id=str(candidate["revision_id"]),
                    definition=definition,
                    compilation=validation.compilation,
                )
            except (AgentBehaviorAuthoringError, ValueError):
                reasons.add("behavior_descriptor_invalid")
            else:
                if behavior.canonical_json() != expected_behavior.canonical_json():
                    reasons.add("behavior_fingerprint_changed")
                behavior = expected_behavior
        if validation.compilation.compiler_version != AGENT_DEPENDENCY_COMPILER_VERSION:
            reasons.add("dependency_compiler_changed")
        if str(candidate["risk_compiler_version"]) != validation.compilation.risk.compiler_version:
            reasons.add("risk_compiler_changed")
        if str(candidate["dependency_fingerprint"]) != validation.compilation.dependency_fingerprint:
            reasons.add("dependency_fingerprint_changed")
        if str(candidate["capability_fingerprint"]) != validation.compilation.capability_fingerprint:
            reasons.add("capability_fingerprint_changed")
        if str(candidate["risk_json"]) != _canonical_json(validation.compilation.risk.to_dict()):
            reasons.add("risk_assessment_changed")
        audience_json = _canonical_json(audience.to_dict())
        if str(candidate["target_audience_json"]) != audience_json or str(candidate["target_audience_hash"]) != _hash_text(
            audience_json,
        ):
            reasons.add("candidate_audience_integrity_failed")
        try:
            if behavior is None:
                raise ValueError("behavior descriptor unavailable")
            eval_evidence = json.loads(str(candidate["eval_evidence_json"])) if candidate["eval_evidence_json"] is not None else None
            expected_candidate_hash = _candidate_hash(
                base_live_release_id=candidate["base_live_release_id"],
                audience=audience,
                behavior=behavior,
                content_hash=_hash_json({"definition": definition, "listing": listing}),
                validation=validation,
                eval_evidence=eval_evidence,
                release_note=(str(candidate["release_note"]) if candidate["release_note"] is not None else None),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons.add("candidate_payload_malformed")
        else:
            if str(candidate["candidate_hash"]) != expected_candidate_hash:
                reasons.add("candidate_hash_changed")
        async with db.execute(
            "SELECT * FROM agent_safety_report WHERE id=? AND team_id=? AND agent_id=?",
            (candidate["safety_report_id"], candidate["team_id"], candidate["subagent_id"]),
        ) as cursor:
            report = await cursor.fetchone()
        content_hash = _hash_json({"definition": definition, "listing": listing})
        if report is None:
            reasons.add("safety_report_missing")
        else:
            expected = {
                "content_hash": content_hash,
                "dependency_fingerprint": validation.compilation.dependency_fingerprint,
                "compiler_version": validation.scanner_version,
                "semantic_scanner_version": validation.semantic_scanner_version,
                "status": validation.safety_status,
                "deterministic_json": _canonical_json(validation.deterministic_dict()),
                "semantic_json": _canonical_json(validation.semantic_dict()),
            }
            if any(str(report[key]) != str(value) for key, value in expected.items()):
                reasons.add("safety_report_changed")
        if validation.safety_status in {"blocked", "unknown"} or not validation.compilation.publishable:
            reasons.add("publication_no_longer_safe")
        return tuple(sorted(reasons))

    async def _project_live_conn(
        self,
        db: aiosqlite.Connection,
        *,
        aggregate: aiosqlite.Row,
        definition: dict[str, Any],
        listing: dict[str, Any],
        audience: AgentAudience,
        revision_id: str,
        release_id: str,
        release_sequence: int,
        event_id: str,
        actor_user_id: str,
        timestamp: str,
    ) -> None:
        projection = _live_projection_values(definition, listing)
        await self._replace_audience_conn(
            db,
            team_id=str(aggregate["team_id"]),
            agent_id=str(aggregate["id"]),
            audience=audience,
            actor_user_id=actor_user_id,
            timestamp=timestamp,
        )
        updated = await db.execute(
            """
            UPDATE team_subagent
            SET display_name=?,description=?,system_prompt=?,tool_refs_json=?,model=?,
                max_iterations=?,skills_json=?,output_schema_json=?,audience_mode=?,
                visibility=?,live_revision_id=?,live_release_id=?,release_sequence=?,
                last_release_event_id=?,definition_mode='revisioned',row_version=row_version+1,
                updated_at=?
            WHERE id=? AND team_id=? AND row_version=? AND live_release_id IS ?
            """,
            (
                projection.display_name,
                projection.description,
                projection.system_prompt,
                _canonical_json(projection.tool_refs),
                projection.model,
                projection.max_iterations,
                _canonical_json(projection.skill_names),
                _canonical_json(projection.output_schema) if projection.output_schema is not None else None,
                audience.mode,
                "team_wide" if audience.mode == "teamwide" else "creator_only",
                revision_id,
                release_id,
                release_sequence,
                event_id,
                timestamp,
                str(aggregate["id"]),
                str(aggregate["team_id"]),
                int(aggregate["row_version"]),
                aggregate["live_release_id"],
            ),
        )
        if updated.rowcount != 1:
            raise AgentRevisionConflictError("agent_aggregate_version_conflict")
        await db.execute(
            "DELETE FROM agent_catalog_projection WHERE team_id=? AND agent_id=?",
            (str(aggregate["team_id"]), str(aggregate["id"])),
        )

    async def _replace_audience_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        audience: AgentAudience,
        actor_user_id: str,
        timestamp: str,
    ) -> None:
        if audience.mode == "teamwide":
            principals = (("team", team_id),)
        elif audience.mode == "custom":
            principals = tuple(("user", value) for value in audience.user_ids) + tuple(("group", value) for value in audience.group_ids)
        else:
            principals = ()
        await self._access_store._replace_team_subagent_audience_conn(
            db,
            team_id=team_id,
            resource_id=agent_id,
            principals=tuple(sorted(principals)),
            created_by=actor_user_id,
            timestamp=timestamp,
            subagent_transaction_token=self._transaction_token,
        )
        await self._access_store._bump_policy_epoch_conn(db, team_id)

    async def _normalize_definition_for_revalidation(
        self,
        definition: Mapping[str, Any],
        *,
        team_id: str,
    ) -> dict[str, Any]:
        if not {"tool_refs_json", "skills_json", "output_schema_json"}.intersection(
            definition,
        ):
            return dict(definition)
        normalizer = getattr(self._validator, "normalize_legacy_definition", None)
        if normalizer is None:
            raise AgentPublicationBlockedError("agent_legacy_definition_requires_upgrade")
        normalized = await normalizer(definition, team_id=team_id)
        if not isinstance(normalized, dict):
            raise AgentPublicationBlockedError("agent_legacy_definition_requires_upgrade")
        return normalized

    async def _pause_agent_schedules_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        routing_name: str,
        timestamp: str,
    ) -> int:
        async with db.execute("PRAGMA table_info(scheduled_jobs)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        if "recipe_ref" not in columns or "pause_reason" not in columns:
            return 0
        updated = await db.execute(
            """
            UPDATE scheduled_jobs
            SET status='paused',pause_reason='agent_archived',updated_at=?
            WHERE status='active' AND json_valid(recipe_ref)
              AND EXISTS(
                SELECT 1 FROM workspaces schedule_workspace
                WHERE schedule_workspace.id=scheduled_jobs.workspace_id
                  AND schedule_workspace.team_id=?
              )
              AND json_extract(recipe_ref,'$.form')='team_subagent'
              AND json_extract(recipe_ref,'$.id_or_name') IN (?,?)
            """,
            (timestamp, team_id, agent_id, routing_name),
        )
        return max(updated.rowcount, 0)

    async def _refresh_assignment_access_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        owner_user_id: str,
        timestamp: str,
    ) -> None:
        await db.execute(
            """
            UPDATE agent_assignment
            SET access_state=CASE
                  WHEN EXISTS(
                    SELECT 1 FROM users assignment_user
                    JOIN team_members assignment_member
                      ON assignment_member.user_id=assignment_user.id
                     AND assignment_member.team_id=agent_assignment.team_id
                     AND assignment_member.status='active'
                    WHERE assignment_user.id=agent_assignment.user_id
                      AND assignment_user.status='active'
                  )
                  AND NOT EXISTS(
                    SELECT 1 FROM resource_grants deny_grant
                    WHERE deny_grant.team_id=agent_assignment.team_id
                      AND deny_grant.resource_kind='team_subagent'
                      AND deny_grant.resource_id=agent_assignment.agent_id
                      AND deny_grant.permission IN ('use','write','admin')
                      AND deny_grant.effect='deny' AND deny_grant.revoked_at IS NULL
                      AND (deny_grant.expires_at IS NULL OR deny_grant.expires_at>?)
                      AND (
                        (deny_grant.principal_type='team' AND deny_grant.principal_id=agent_assignment.team_id)
                        OR (deny_grant.principal_type='user' AND deny_grant.principal_id=agent_assignment.user_id)
                        OR (deny_grant.principal_type='group' AND EXISTS(
                          SELECT 1 FROM group_memberships deny_membership
                          JOIN groups deny_group
                            ON deny_group.id=deny_membership.group_id
                           AND deny_group.team_id=deny_membership.team_id
                           AND deny_group.status='active'
                          WHERE deny_membership.team_id=agent_assignment.team_id
                            AND deny_membership.group_id=deny_grant.principal_id
                            AND deny_membership.principal_type='user'
                            AND deny_membership.principal_id=agent_assignment.user_id
                            AND deny_membership.status='active'
                        ))
                      )
                  )
                  AND (
                    agent_assignment.user_id=?
                    OR EXISTS(
                      SELECT 1 FROM resource_grants allow_grant
                      WHERE allow_grant.team_id=agent_assignment.team_id
                        AND allow_grant.resource_kind='team_subagent'
                        AND allow_grant.resource_id=agent_assignment.agent_id
                        AND allow_grant.permission IN ('use','write','admin')
                        AND allow_grant.effect='allow' AND allow_grant.revoked_at IS NULL
                        AND (allow_grant.expires_at IS NULL OR allow_grant.expires_at>?)
                        AND (
                          (allow_grant.principal_type='team' AND allow_grant.principal_id=agent_assignment.team_id)
                          OR (allow_grant.principal_type='user' AND allow_grant.principal_id=agent_assignment.user_id)
                          OR (allow_grant.principal_type='group' AND EXISTS(
                            SELECT 1 FROM group_memberships allow_membership
                            JOIN groups allow_group
                              ON allow_group.id=allow_membership.group_id
                             AND allow_group.team_id=allow_membership.team_id
                             AND allow_group.status='active'
                            WHERE allow_membership.team_id=agent_assignment.team_id
                              AND allow_membership.group_id=allow_grant.principal_id
                              AND allow_membership.principal_type='user'
                              AND allow_membership.principal_id=agent_assignment.user_id
                              AND allow_membership.status='active'
                          ))
                        )
                    )
                  )
                  THEN 'granted' ELSE 'revoked' END,
                access_state_changed_at=?,row_version=row_version+1,updated_at=?
            WHERE team_id=? AND agent_id=? AND personal_state<>'removed'
            """,
            (
                timestamp,
                owner_user_id,
                timestamp,
                timestamp,
                timestamp,
                team_id,
                agent_id,
            ),
        )

    async def _validate_audience_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        audience: AgentAudience,
        agent_id: str | None = None,
    ) -> None:
        if audience.user_ids:
            placeholders = ",".join("?" for _ in audience.user_ids)
            async with db.execute(
                f"""
                SELECT u.id FROM users u
                JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
                WHERE u.status='active' AND u.id IN ({placeholders})
                """,  # noqa: S608 -- placeholders derive from a bounded tuple
                (team_id, *audience.user_ids),
            ) as cursor:
                valid_users = {str(row[0]) for row in await cursor.fetchall()}
            if valid_users != set(audience.user_ids):
                raise AgentPublicationBlockedError("agent_audience_user_invalid")
        if audience.group_ids:
            placeholders = ",".join("?" for _ in audience.group_ids)
            async with db.execute(
                f"SELECT id FROM groups WHERE team_id=? AND status='active' AND id IN ({placeholders})",  # noqa: S608
                (team_id, *audience.group_ids),
            ) as cursor:
                valid_groups = {str(row[0]) for row in await cursor.fetchall()}
            if valid_groups != set(audience.group_ids):
                raise AgentPublicationBlockedError("agent_audience_group_invalid")

    async def _owned_aggregate_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        actor_user_id: str,
    ) -> aiosqlite.Row:
        aggregate = await self._aggregate_conn(db, team_id=team_id, agent_id=agent_id)
        if self._owner_id(aggregate) != actor_user_id or not await self._active_member_conn(
            db,
            team_id=team_id,
            actor_user_id=actor_user_id,
        ):
            raise AgentRevisionForbiddenError("agent_owner_required")
        return aggregate

    async def _aggregate_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
    ) -> aiosqlite.Row:
        async with db.execute(
            "SELECT * FROM team_subagent WHERE team_id=? AND id=?",
            (team_id, agent_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentRevisionError("agent_not_found")
        return row

    async def _require_revisioned_active(self, aggregate: aiosqlite.Row) -> None:
        if str(aggregate["definition_mode"]) != "revisioned":
            raise AgentRevisionConflictError("agent_release_baseline_required")
        if str(aggregate["lifecycle_state"] or "active") != "active":
            raise AgentPublicationBlockedError("agent_not_active")

    async def _active_member_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
    ) -> bool:
        async with db.execute(
            """
            SELECT 1 FROM users u
            JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
            WHERE u.id=? AND u.status='active'
            """,
            (team_id, actor_user_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _require_owner_active_conn(
        self,
        db: aiosqlite.Connection,
        *,
        aggregate: Mapping[str, Any],
    ) -> None:
        if not await self._active_member_conn(
            db,
            team_id=str(aggregate["team_id"]),
            actor_user_id=self._owner_id(aggregate),
        ):
            raise AgentPublicationBlockedError("agent_owner_inactive")

    async def _has_qualified_reviewer_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
    ) -> bool:
        async with db.execute(
            """
            SELECT 1 FROM users u
            JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
            LEFT JOIN teams t ON t.id=?
            WHERE u.status='active' AND (u.id=t.created_by_user_id OR tm.role='team_admin')
            LIMIT 1
            """,
            (team_id, team_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _require_agent_owner(self, *, team_id: str, agent_id: str, actor_user_id: str) -> None:
        """Advisory ownership probe, so gate writes never precede authorization.

        The authoritative check is still ``_owned_aggregate_conn`` inside the
        publication transaction; this one only fixes the ordering.
        """
        async with open_shared_ctx(self.db_path) as probe:
            probe.row_factory = aiosqlite.Row
            await self._owned_aggregate_conn(
                probe,
                team_id=team_id,
                agent_id=agent_id,
                actor_user_id=actor_user_id,
            )

    async def _prepare_gate_for_activation(self, *, team_id: str, agent_id: str, actor_user_id: str) -> None:
        """Let the gate install what it needs, outside the activation transaction.

        ``Protocol`` method bodies are not runtime defaults -- a gate that satisfies
        the protocol structurally (``AgentEvalReleaseGate`` does not inherit it) need
        not define this hook, so bare attribute access would be an AttributeError.
        """
        prepare = getattr(self._eval_gate, "prepare_for_activation", None)
        if prepare is None:
            return
        await prepare(
            self.db_path,
            team_id=team_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )

    async def _qualified_reviewer_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
    ) -> bool:
        async with db.execute(
            """
            SELECT 1 FROM users u
            JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
            JOIN teams t ON t.id=?
            WHERE u.id=? AND u.status='active'
              AND (u.id=t.created_by_user_id OR tm.role='team_admin')
            """,
            (team_id, team_id, actor_user_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _candidate_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        candidate_id: str,
    ) -> aiosqlite.Row:
        async with db.execute(
            "SELECT * FROM team_subagent_release_candidate WHERE team_id=? AND id=?",
            (team_id, candidate_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentRevisionError("agent_release_candidate_not_found")
        return row

    async def _candidate_has_active_comparison_conn(self, db: aiosqlite.Connection, *, candidate_id: str) -> bool:
        async with db.execute(
            "SELECT 1 FROM agent_eval_comparison_execution WHERE candidate_id=? AND status IN ('queued','running','finalizing') LIMIT 1",
            (candidate_id,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _withdrawable_candidate_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
    ) -> aiosqlite.Row | None:
        async with db.execute(
            """
            SELECT * FROM team_subagent_release_candidate
            WHERE team_id=? AND subagent_id=? AND state IN ('prepared','pending')
            LIMIT 1
            """,
            (team_id, agent_id),
        ) as cursor:
            return await cursor.fetchone()

    async def _active_candidate_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
    ) -> aiosqlite.Row | None:
        async with db.execute(
            """
            SELECT * FROM team_subagent_release_candidate
            WHERE team_id=? AND subagent_id=?
              AND state IN ('prepared','pending','approved')
            LIMIT 1
            """,
            (team_id, agent_id),
        ) as cursor:
            return await cursor.fetchone()

    async def _working_copy_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
    ) -> aiosqlite.Row | None:
        async with db.execute(
            "SELECT * FROM team_subagent_working_copy WHERE team_id=? AND subagent_id=?",
            (team_id, agent_id),
        ) as cursor:
            return await cursor.fetchone()

    async def _revision_conn(
        self,
        db: aiosqlite.Connection,
        *,
        candidate: aiosqlite.Row,
    ) -> aiosqlite.Row:
        async with db.execute(
            """
            SELECT * FROM team_subagent_revision
            WHERE team_id=? AND subagent_id=? AND id=?
            """,
            (candidate["team_id"], candidate["subagent_id"], candidate["revision_id"]),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentPublicationBlockedError("agent_revision_missing")
        return row

    async def _release_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        release_id: str,
    ) -> aiosqlite.Row:
        async with db.execute(
            """
            SELECT * FROM team_subagent_release
            WHERE team_id=? AND subagent_id=? AND id=?
            """,
            (team_id, agent_id, release_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentRevisionConflictError("agent_live_release_missing")
        return row

    async def _revision_for_release_conn(
        self,
        db: aiosqlite.Connection,
        *,
        release: aiosqlite.Row,
    ) -> aiosqlite.Row:
        async with db.execute(
            """
            SELECT * FROM team_subagent_revision
            WHERE team_id=? AND subagent_id=? AND id=?
            """,
            (release["team_id"], release["subagent_id"], release["revision_id"]),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise AgentPublicationBlockedError("agent_revision_missing")
        return row

    async def _reconcile_working_copy_after_activation_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        previous_release_id: str | None,
        activated_release_id: str,
        definition: dict[str, Any],
        listing: dict[str, Any],
        audience: AgentAudience,
        timestamp: str,
    ) -> None:
        """Delete the submitted draft or rebase a newer draft onto the new live.

        A pending candidate is immutable, but its Owner may keep editing the
        one working copy for the next release.  Approval must not strand that
        newer draft on the candidate's former base release.
        """
        working = await self._working_copy_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
        )
        if working is None:
            return
        definition_json = _canonical_json(definition)
        audience_json = _canonical_json(audience.to_dict())
        # Compare like with like.  What was activated is the *materialized* listing,
        # while the working copy holds what the Owner actually typed, so the raw two
        # differ on every publish and the draft would never be reclaimed -- the Owner
        # would see a permanent "unpublished draft" they never wrote.  Materializing
        # the draft is not enough on its own: a candidate frozen before a
        # `source_locale` and would compare unequal forever, and the flip itself is
        # what puts every pending candidate in that position.  So drop the
        # materializer's own bookkeeping from *both* sides.
        working_listing = _decode_object(working["listing_json"], name="listing")
        working_listing = await materialize_confirmed_agent_localizations_conn(
            db,
            team_id=team_id,
            agent_id=agent_id,
            working_copy_id=str(working["id"]),
            listing=working_listing,
        )
        comparable_working_listing = _canonical_json(without_server_derived_listing_keys(working_listing))
        comparable_activated_listing = _canonical_json(without_server_derived_listing_keys(listing))
        # A Skill rebased to its current approved revision at prepare time is
        # still the draft the Owner wrote; compare without Skill revisions.
        comparable_working_definition = _canonical_json(
            _without_skill_revisions(_decode_object(working["definition_json"], name="definition")),
        )
        if (
            (
                str(working["definition_json"]) == definition_json
                or comparable_working_definition == _canonical_json(_without_skill_revisions(definition))
            )
            and comparable_working_listing == comparable_activated_listing
            and str(working["audience_json"]) == audience_json
        ):
            await invalidate_working_copy_projection_conn(
                db,
                team_id=team_id,
                working_copy_id=str(working["id"]),
            )
            await db.execute(
                "DELETE FROM team_subagent_working_copy WHERE id=? AND row_version=?",
                (working["id"], working["row_version"]),
            )
            return
        working_base = str(working["base_release_id"]) if working["base_release_id"] is not None else None
        if working_base != previous_release_id:
            return
        await invalidate_working_copy_projection_conn(
            db,
            team_id=team_id,
            working_copy_id=str(working["id"]),
        )
        await db.execute(
            """
            UPDATE team_subagent_working_copy
            SET base_release_id=?,validation_json='{"status":"not_validated"}',
                row_version=row_version+1,updated_at=?
            WHERE id=? AND row_version=?
            """,
            (
                activated_release_id,
                timestamp,
                working["id"],
                working["row_version"],
            ),
        )

    async def _insert_event_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        candidate_id: str | None,
        release_id: str | None,
        event_type: str,
        actor_user_id: str,
        payload: Mapping[str, Any],
        timestamp: str,
    ) -> str:
        event_id = _id("are")
        await db.execute(
            """
            INSERT INTO team_subagent_release_event(
              id,team_id,subagent_id,release_id,candidate_id,event_type,
              actor_user_id,payload_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                team_id,
                agent_id,
                release_id,
                candidate_id,
                event_type,
                actor_user_id,
                _canonical_json(payload),
                timestamp,
            ),
        )
        return event_id

    async def _enqueue_reviewers_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        event_id: str,
        candidate_id: str,
        timestamp: str,
    ) -> None:
        payload_json = _canonical_json({"candidate_id": candidate_id})
        await db.execute(
            """
            INSERT INTO team_subagent_notification_outbox(
              id,release_event_id,recipient_user_id,notification_type,dedupe_key,
              payload_json,state,attempt_count,next_attempt_at,created_at
            )
            SELECT 'ano_' || lower(hex(randomblob(16))), ?, u.id, 'agent.release.approval_required',
                   'agent-release:' || ? || ':approval_required:' || u.id,
                   ?, 'pending', 0, ?, ?
            FROM users u
            JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
            JOIN teams t ON t.id=?
            WHERE u.status='active' AND (u.id=t.created_by_user_id OR tm.role='team_admin')
            """,
            (event_id, event_id, payload_json, timestamp, timestamp, team_id, team_id),
        )

    async def _enqueue_owner_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        event_id: str,
        notification_type: str,
        timestamp: str,
    ) -> None:
        aggregate = await self._aggregate_conn(db, team_id=team_id, agent_id=agent_id)
        owner_id = self._owner_id(aggregate)
        await db.execute(
            """
            INSERT INTO team_subagent_notification_outbox(
              id,release_event_id,recipient_user_id,notification_type,dedupe_key,
              payload_json,state,attempt_count,next_attempt_at,created_at
            )
            SELECT 'ano_' || lower(hex(randomblob(16))), ?, u.id, ?,
                   'agent-release:' || ? || ':' || ? || ':' || u.id,
                   ?, 'pending', 0, ?, ?
            FROM users u
            JOIN team_members tm ON tm.user_id=u.id AND tm.team_id=? AND tm.status='active'
            WHERE u.id=? AND u.status='active'
              AND NOT EXISTS (
                SELECT 1 FROM team_subagent_notification_outbox existing
                WHERE existing.release_event_id=?
                  AND existing.recipient_user_id=u.id
                  AND existing.notification_type=?
              )
            """,
            (
                event_id,
                notification_type,
                event_id,
                notification_type,
                _canonical_json({"agent_id": agent_id}),
                timestamp,
                timestamp,
                team_id,
                owner_id,
                event_id,
                notification_type,
            ),
        )

    async def _enqueue_assignments_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        event_id: str,
        notification_type: str,
        timestamp: str,
    ) -> None:
        await db.execute(
            """
            INSERT INTO team_subagent_notification_outbox(
              id,release_event_id,recipient_user_id,notification_type,dedupe_key,
              payload_json,state,attempt_count,next_attempt_at,created_at
            )
            SELECT 'ano_' || lower(hex(randomblob(16))), ?, aa.user_id, ?,
                   'agent-release:' || ? || ':' || ? || ':' || aa.user_id,
                   ?, 'pending', 0, ?, ?
            FROM agent_assignment aa
            JOIN users u ON u.id=aa.user_id AND u.status='active'
            WHERE aa.team_id=? AND aa.agent_id=? AND aa.agent_source_kind='team'
              AND aa.personal_state<>'removed' AND aa.access_state='granted'
            """,
            (
                event_id,
                notification_type,
                event_id,
                notification_type,
                _canonical_json({"agent_id": agent_id}),
                timestamp,
                timestamp,
                team_id,
                agent_id,
            ),
        )

    async def _enqueue_version_owner_event_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        owner_user_id: str,
        candidate_id: str,
        plan_id: str,
        candidate_hash: str,
        plan_hash: str,
        timestamp: str,
    ) -> None:
        await db.execute(
            """
            INSERT INTO agent_version_notification_outbox(
              id,team_id,agent_id,recipient_user_id,event_type,candidate_id,plan_id,
              snapshot_id,release_id,dedupe_key,safe_payload_json,status,
              lease_generation,attempt_count,available_at,created_at
            ) VALUES(?,?,?,?,'candidate.prepared',?,?,NULL,NULL,?,?,'pending',0,0,?,?)
            """,
            (
                _id("aevn"),
                team_id,
                agent_id,
                owner_user_id,
                candidate_id,
                plan_id,
                f"agent-version:{candidate_id}:prepared:{owner_user_id}",
                _canonical_json(
                    {
                        "candidate_hash": candidate_hash,
                        "candidate_id": candidate_id,
                        "plan_hash": plan_hash,
                        "plan_id": plan_id,
                        "schema_version": 1,
                    },
                ),
                timestamp,
                timestamp,
            ),
        )

    async def _write_audit_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        action: str,
        agent_id: str,
        payload: dict[str, Any],
    ) -> None:
        await UserStore(self.db_path).write_admin_audit_log(
            actor_user_id=actor_user_id,
            team_id=team_id,
            action=action,
            target_kind="team_subagent",
            target_id=agent_id,
            target_team_id=team_id,
            scope="team",
            payload=payload,
            conn=db,
            audit_id=_id("aud"),
        )

    async def _receipt_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> AgentPublicationResult | None:
        key_hash = _hash_text(idempotency_key)
        async with db.execute(
            """
            SELECT request_hash,result_json,expires_at FROM agent_command_receipt
            WHERE team_id=? AND actor_user_id=? AND command_scope=? AND idempotency_hash=?
            """,
            (team_id, actor_user_id, scope, key_hash),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise AgentRevisionConflictError("agent_command_idempotency_conflict")
        if str(row["expires_at"]) <= self._now():
            await db.execute(
                """
                DELETE FROM agent_command_receipt
                WHERE team_id=? AND actor_user_id=? AND command_scope=? AND idempotency_hash=?
                """,
                (team_id, actor_user_id, scope, key_hash),
            )
            return None
        return _publication_result_from_json(str(row["result_json"]))

    async def _store_receipt_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: AgentPublicationResult,
        timestamp: str,
    ) -> None:
        expires_at = (self._clock.now().astimezone(UTC) + _RECEIPT_TTL).isoformat()
        await db.execute(
            """
            INSERT INTO agent_command_receipt(
              team_id,actor_user_id,command_scope,idempotency_hash,request_hash,
              result_json,created_at,expires_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                team_id,
                actor_user_id,
                scope,
                _hash_text(idempotency_key),
                request_hash,
                _canonical_json(_publication_result_dict(result)),
                timestamp,
                expires_at,
            ),
        )

    async def _preparation_receipt_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> AgentPreparationResult | None:
        key_hash = _hash_text(idempotency_key)
        async with db.execute(
            """
            SELECT request_hash,result_json,expires_at FROM agent_command_receipt
            WHERE team_id=? AND actor_user_id=? AND command_scope=? AND idempotency_hash=?
            """,
            (team_id, actor_user_id, scope, key_hash),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise AgentRevisionConflictError("agent_command_idempotency_conflict")
        if str(row["expires_at"]) <= self._now():
            await db.execute(
                """
                DELETE FROM agent_command_receipt
                WHERE team_id=? AND actor_user_id=? AND command_scope=? AND idempotency_hash=?
                """,
                (team_id, actor_user_id, scope, key_hash),
            )
            return None
        return _preparation_result_from_json(str(row["result_json"]))

    async def _require_replayed_preparation_current_conn(
        self,
        db: aiosqlite.Connection,
        *,
        aggregate: aiosqlite.Row,
        replay: AgentPreparationResult,
    ) -> None:
        """Refuse to replay a ``prepared`` receipt whose candidate has left that state.

        Unlike publish receipts, which record a finished outcome, a preparation receipt
        is a live handle (candidate, plan and control versions) that the caller acts on.
        Once the candidate was withdrawn (by the owner or by archive), went stale or moved
        on, the key stays consumed and the replay fails with a conflict instead of
        reporting a candidate that is no longer prepared. An archived Agent reports
        ``agent_not_active`` first, exactly as a fresh request would.
        """
        candidate = await self._candidate_conn(
            db,
            team_id=str(aggregate["team_id"]),
            candidate_id=replay.candidate_id,
        )
        if str(candidate["state"]) == "prepared":
            return
        await self._require_revisioned_active(aggregate)
        raise AgentRevisionConflictError("agent_release_candidate_state_conflict")

    async def _store_preparation_receipt_conn(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        actor_user_id: str,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        result: AgentPreparationResult,
        timestamp: str,
    ) -> None:
        expires_at = (self._clock.now().astimezone(UTC) + _RECEIPT_TTL).isoformat()
        await db.execute(
            """
            INSERT INTO agent_command_receipt(
              team_id,actor_user_id,command_scope,idempotency_hash,request_hash,
              result_json,created_at,expires_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                team_id,
                actor_user_id,
                scope,
                _hash_text(idempotency_key),
                request_hash,
                _canonical_json(_preparation_result_dict(result)),
                timestamp,
                expires_at,
            ),
        )

    async def _validate_knowledge_write(self, db, definition, *, team_id, actor_user_id) -> None:
        policy = require_supported_knowledge_access(definition)
        if policy is None:
            return
        if not source_bindings_enabled():
            raise KnowledgeAccessError("agent_knowledge_access_admission_closed")
        from bowhead_core.agent_knowledge_policy import require_knowledge_migration

        await require_knowledge_migration(db)
        if policy.file_ids:
            if self._knowledge_source_validator is None or not await self._knowledge_source_validator(policy.file_ids, team_id, actor_user_id):
                raise KnowledgeAccessError("agent_knowledge_source_unavailable")

    async def _validate_with_current_skills(
        self,
        db: aiosqlite.Connection,
        *,
        team_id: str,
        agent_id: str,
        owner_user_id: str,
        routing_name: str,
        definition: dict[str, Any],
        listing: dict[str, Any],
        audience: AgentAudience,
    ) -> tuple[dict[str, Any], AgentPublicationValidation]:
        """Validate, rebasing the definition's Skill grants to their current approved revision.

        Skills follow their latest approved revision (agent-skill-gates D2). The
        compiler rebases a stale grant instead of blocking it; this writes the
        rebased grant into the definition that is frozen and released, and
        validates that exact definition. Only Skill grant revisions differ, and
        the semantic scan does not read them, so the Safety check stored for the
        Owner's definition remains the semantic evidence for the rebased one.
        """
        await self._validate_knowledge_write(db, definition, team_id=team_id, actor_user_id=owner_user_id)
        arguments = {
            "team_id": team_id,
            "agent_id": agent_id,
            "owner_user_id": owner_user_id,
            "routing_name": routing_name,
            "listing": listing,
            "audience": audience,
        }
        validation = await self._validator.validate(db, definition=definition, **arguments)
        rebase = getattr(validation.compilation, "rebased_skill_grants", None)
        rebased = rebase(definition) if rebase is not None else None
        if rebased is None:
            return definition, validation
        rebased_definition = {**definition, "skill_grants": rebased}
        return rebased_definition, await self._validator.validate(
            db,
            definition=rebased_definition,
            semantic_evidence_definition=definition,
            **arguments,
        )

    def _requires_review(
        self,
        validation: AgentPublicationValidation,
        *,
        audience: AgentAudience,
    ) -> bool:
        return requires_admin_review(
            validation,
            audience=audience,
            eval_gate_installed=self._eval_gate is not None,
        )

    def _require_publishable(
        self,
        validation: AgentPublicationValidation,
        *,
        audience: AgentAudience,
    ) -> None:
        reasons = tuple(
            sorted(
                set(validation.compilation.reason_codes)
                | set(validation.deterministic_reason_codes)
                | set(validation.semantic_reason_codes)
                | set(validation.behavior_reason_codes),
            ),
        )
        blocked_items = tuple(getattr(validation.compilation, "blocked_items", ()))
        if validation.behavior_primary_requested and validation.behavior_status != "reviewed":
            raise AgentPublicationBlockedError(
                "primary_capability_unproven",
                reason_codes=reasons or ("primary_capability_unproven",),
                blocked_items=blocked_items,
            )
        if validation.behavior_status in {"blocked", "shadow"}:
            raise AgentPublicationBlockedError(
                "behavior_compile_blocked",
                reason_codes=reasons or ("behavior_compile_blocked",),
                blocked_items=blocked_items,
            )
        if not validation.compilation.publishable:
            raise AgentPublicationBlockedError("agent_dependency_blocked", reason_codes=reasons, blocked_items=blocked_items)
        if validation.safety_status in {"blocked", "unknown"}:
            raise AgentPublicationBlockedError("agent_safety_blocked", reason_codes=reasons, blocked_items=blocked_items)
        if audience.mode == "private" and validation.safety_status not in {"pass", "warning"}:
            raise AgentPublicationBlockedError("agent_private_safety_review_required", reason_codes=reasons, blocked_items=blocked_items)

    def _require_candidate_cas(
        self,
        candidate: aiosqlite.Row,
        *,
        expected_row_version: int,
        expected_candidate_hash: str,
    ) -> None:
        if int(candidate["row_version"]) != expected_row_version:
            raise AgentRevisionConflictError("agent_release_candidate_version_conflict")
        if str(candidate["candidate_hash"]) != expected_candidate_hash:
            raise AgentRevisionConflictError("agent_release_candidate_hash_conflict")

    def _owner_id(self, aggregate: Mapping[str, Any]) -> str:
        owner = aggregate["owner_user_id"] or aggregate["created_by"]
        if not owner:
            raise AgentPublicationBlockedError("agent_owner_missing")
        return str(owner)

    def _now(self) -> str:
        return self._clock.now().astimezone(UTC).isoformat()

    def _activated_result(
        self,
        candidate: aiosqlite.Row,
        *,
        aggregate: aiosqlite.Row,
    ) -> AgentPublicationResult:
        return AgentPublicationResult(
            state="activated",
            agent_id=str(candidate["subagent_id"]),
            candidate_id=str(candidate["id"]),
            revision_id=str(candidate["revision_id"]),
            release_id=str(candidate["activated_release_id"]),
            candidate_row_version=int(candidate["row_version"]),
            aggregate_row_version=int(aggregate["row_version"]),
        )

    def _terminal_result(
        self,
        candidate: Mapping[str, Any],
        state: Literal["rejected", "withdrawn", "stale"],
        *,
        aggregate: Mapping[str, Any],
    ) -> AgentPublicationResult:
        return AgentPublicationResult(
            state=state,
            agent_id=str(candidate["subagent_id"]),
            candidate_id=str(candidate["id"]),
            revision_id=str(candidate["revision_id"]),
            release_id=None,
            candidate_row_version=int(candidate["row_version"]),
            aggregate_row_version=int(aggregate["row_version"]),
        )


@dataclass(frozen=True, slots=True)
class _FrozenCandidate:
    candidate: aiosqlite.Row
    candidate_id: str
    revision_id: str
    candidate_hash: str
    state: Literal["prepared", "pending"]
    candidate_row_version: int
    created: bool
    behavior_fingerprint: str | None
    primary_capability_ref: str | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row, *, candidate_hash: str) -> _FrozenCandidate:
        state = str(row["state"])
        if state not in {"prepared", "pending"}:
            raise candidate_already_active_error(row)
        return cls(
            candidate=row,
            candidate_id=str(row["id"]),
            revision_id=str(row["revision_id"]),
            candidate_hash=candidate_hash,
            state=state,
            candidate_row_version=int(row["row_version"]),
            created=False,
            behavior_fingerprint=None,
            primary_capability_ref=None,
        )


def _working_copy_from_row(row: aiosqlite.Row) -> AgentWorkingCopy:
    return AgentWorkingCopy(
        id=str(row["id"]),
        team_id=str(row["team_id"]),
        agent_id=str(row["subagent_id"]),
        base_release_id=str(row["base_release_id"]),
        definition=_decode_object(row["definition_json"], name="definition"),
        listing=_decode_object(row["listing_json"], name="listing"),
        audience=AgentAudience.from_mapping(_decode_object(row["audience_json"], name="audience")),
        row_version=int(row["row_version"]),
        updated_at=str(row["updated_at"]),
    )


def _live_projection_values(
    definition: Mapping[str, Any],
    listing: Mapping[str, Any],
) -> _LiveProjection:
    require_supported_knowledge_access(definition)
    system_prompt = definition.get("system_prompt", "")
    tool_refs = definition.get("tool_refs", [])
    model = definition.get("model")
    max_iterations = definition.get("max_iterations", 10)
    output_schema = definition.get("output_schema")
    skill_grants = definition.get("skill_grants", [])
    if not isinstance(system_prompt, str) or not isinstance(tool_refs, list) or not all(isinstance(item, str) for item in tool_refs):
        raise AgentPublicationBlockedError("agent_definition_projection_invalid")
    if model is not None and not isinstance(model, str):
        raise AgentPublicationBlockedError("agent_definition_projection_invalid")
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or not 1 <= max_iterations <= 50:
        raise AgentPublicationBlockedError("agent_definition_projection_invalid")
    if output_schema is not None and not isinstance(output_schema, dict):
        raise AgentPublicationBlockedError("agent_definition_projection_invalid")
    if not isinstance(skill_grants, list):
        raise AgentPublicationBlockedError("agent_definition_projection_invalid")
    # Optional opt-in read by the runtime from the live revision; absent means off.
    if not isinstance(definition.get("capability_protocol_block", False), bool):
        raise AgentPublicationBlockedError("agent_definition_projection_invalid")
    skill_names: list[str] = []
    for grant in skill_grants:
        if not isinstance(grant, Mapping):
            raise AgentPublicationBlockedError("agent_definition_projection_invalid")
        ref = grant.get("skill_ref")
        permissions = grant.get("permissions")
        if (
            not isinstance(ref, Mapping)
            or not isinstance(ref.get("name"), str)
            or not str(ref["name"]).strip()
            or not isinstance(permissions, list)
            or not all(isinstance(permission, str) for permission in permissions)
        ):
            raise AgentPublicationBlockedError("agent_definition_projection_invalid")
        if "read" in permissions:
            skill_names.append(str(ref["name"]))
    display_name = listing.get("display_name")
    description = listing.get("description", listing.get("summary", ""))
    if not isinstance(display_name, str) or not display_name.strip() or not isinstance(description, str):
        raise AgentPublicationBlockedError("agent_listing_projection_invalid")
    return _LiveProjection(
        system_prompt=system_prompt,
        tool_refs=tuple(tool_refs),
        model=model,
        max_iterations=max_iterations,
        output_schema=output_schema,
        skill_names=tuple(sorted(set(skill_names))),
        display_name=display_name.strip(),
        description=description,
    )


def _listing_text_values(listing: Mapping[str, Any]) -> tuple[str, ...]:
    texts: list[str] = []
    pending: list[tuple[Any, str | None]] = [(listing, None)]
    while pending:
        value, parent_key = pending.pop()
        if isinstance(value, str):
            if parent_key == "team_tag_ids" and re.fullmatch(r"att_[0-9a-f]{32}", value):
                continue
            texts.append(value)
            if len(texts) > _MAX_LISTING_TEXT_VALUES:
                raise ValueError("Agent listing has too many text values")
        elif isinstance(value, Mapping):
            pending.extend((item, str(key)) for key, item in value.items() if key != "localization_metadata")
        elif isinstance(value, list):
            pending.extend((item, parent_key) for item in value)
    return tuple(texts)


def _prompt_data_safety_reason_codes(prompt: str) -> tuple[str, ...]:
    reasons: set[str] = set()
    try:
        _redacted, secret_hits = extract_secrets(prompt)
    except Exception:
        reasons.add("prompt_safety_check_failed")
    else:
        if secret_hits:
            reasons.add("credential_or_secret")
    try:
        _scrubbed, pii_counts = scrub_with_count(prompt)
    except Exception:
        reasons.add("prompt_safety_check_failed")
    else:
        reasons.update(_SCRUBBER_REASON_CODES.get(kind, "pii_unknown") for kind in pii_counts)
    return tuple(sorted(reasons))


def _selection_tool_refs(definition: Mapping[str, Any]) -> set[str]:
    """Read direct bindings in both supported formats; incomplete drafts stay editable."""
    refs = definition.get("tool_refs")
    if refs is None and "tool_refs_json" in definition:
        try:
            refs = json.loads(str(definition["tool_refs_json"]))
        except (TypeError, ValueError):
            return set()
    return {canonical_tool_name(ref) for ref in refs if isinstance(ref, str)} if isinstance(refs, list) else set()


def _canonical_document(value: Mapping[str, Any], *, name: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Agent {name} must be an object")
    if name == "definition":
        require_supported_knowledge_access(value)
    copied = dict(value)
    if name == "definition" and (policy := require_supported_knowledge_access(value)) is not None:
        copied["knowledge_access"] = policy.canonical_value()
    encoded = _canonical_json(copied)
    if len(encoded.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
        raise ValueError(f"Agent {name} exceeds {_MAX_DOCUMENT_BYTES} bytes")
    return encoded, json.loads(encoded)


def next_release_sequence(current_release_sequence: int) -> int:
    """Return the release sequence the next activation assigns after ``current_release_sequence``.

    Every activation path and every read that previews the number (workbench candidate label,
    review label) must use this so the preview cannot drift from the number activation writes.
    """
    return current_release_sequence + 1


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent publication payload is not canonical JSON") from exc


def _decode_object(value: Any, *, name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentPublicationBlockedError(f"agent_{name}_malformed") from exc
    if not isinstance(decoded, dict):
        raise AgentPublicationBlockedError(f"agent_{name}_malformed")
    return decoded


def _hash_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _hash_json(value: Any) -> str:
    return _hash_text(_canonical_json(value))


async def _compatibility_exists_conn(
    db: aiosqlite.Connection,
    *,
    team_id: str,
    agent_id: str,
    revision_id: str,
) -> bool:
    async with db.execute(
        """
        SELECT 1 FROM agent_revision_compatibility
        WHERE team_id=? AND agent_id=? AND revision_id=?
        """,
        (team_id, agent_id, revision_id),
    ) as cursor:
        return await cursor.fetchone() is not None


def working_copy_content_hash(working: Mapping[str, Any]) -> str:
    """Hash editable accepted content without mutable validation bookkeeping."""
    return _hash_json(
        {
            "schema_version": 1,
            "working_copy_id": working["id"],
            "base_release_id": working["base_release_id"],
            "definition": _decode_object(working["definition_json"], name="definition"),
            "listing": _decode_object(working["listing_json"], name="listing"),
            "audience": AgentAudience.from_mapping(_decode_object(working["audience_json"], name="audience")).to_dict(),
        }
    )


def _without_skill_revisions(definition: Mapping[str, Any]) -> dict[str, Any]:
    grants = definition.get("skill_grants")
    if not isinstance(grants, list):
        return dict(definition)
    return {
        **definition,
        "skill_grants": [{key: value for key, value in grant.items() if key != "revision"} if isinstance(grant, dict) else grant for grant in grants],
    }


def _working_copy_hash(
    *,
    working: Mapping[str, Any],
    definition: Mapping[str, Any],
    listing: Mapping[str, Any],
    audience: AgentAudience,
) -> str:
    return _hash_json(
        {
            "audience": audience.to_dict(),
            "base_release_id": working["base_release_id"],
            "definition": definition,
            "listing": listing,
            "row_version": int(working["row_version"]),
            "schema_version": 1,
            "validation": _decode_object(
                working["validation_json"],
                name="working_copy_validation",
            ),
            "working_copy_id": str(working["id"]),
        },
    )


def _preparation_dependency_error(code: str, *, details: Mapping[str, str] | None = None) -> AgentRevisionError:
    if code in {
        "agent_eval_plan_forbidden",
        "agent_eval_policy_forbidden",
        "agent_eval_workspace_execute_denied",
    }:
        return AgentRevisionForbiddenError(code)
    if code in {
        "agent_eval_control_version_conflict",
        "agent_eval_plan_frozen_for_review",
        "agent_eval_policy_plan_rotation_required",
        "agent_eval_policy_version_conflict",
        "agent_release_candidate_version_conflict",
    }:
        return AgentRevisionConflictError(code, details=details)
    # An expired case names itself (suite revision, case key, expiry) so the Owner can remove it.
    return AgentPublicationBlockedError(code, details=details if code == "agent_eval_fixture_expired" else None)


def _compile_behavior_preview(
    *,
    materializer: AgentBehaviorMaterializer,
    agent_id: str,
    definition: Mapping[str, Any],
    compilation: AgentDependencyCompilation,
    intent: AgentBehaviorIntentValidation,
) -> AgentBehaviorDescriptor:
    definition_value = dict(definition)
    definition_hash = _hash_text(_canonical_json(definition_value))
    return _compile_behavior_descriptor(
        materializer=materializer,
        revision_id=f"working-copy:{agent_id}:{definition_hash}",
        definition=definition_value,
        compilation=compilation,
        effective_model_role=intent.effective_model_role,
    )


def _compile_behavior_descriptor(
    *,
    materializer: AgentBehaviorMaterializer,
    revision_id: str,
    definition: dict[str, Any],
    compilation: AgentDependencyCompilation,
    effective_model_role: str = "analyst",
) -> AgentBehaviorDescriptor:
    try:
        return materializer.compile_team_revision(
            revision_id=revision_id,
            definition=definition,
            source_definition_hash=_hash_text(_canonical_json(definition)),
            dependency=compilation,
            effective_model_role=effective_model_role,
        )
    except (AgentBehaviorMaterializationError, TypeError, ValueError) as exc:
        raise AgentBehaviorAuthoringError(
            "behavior_compile_blocked",
            reason_codes=("behavior_composition_invalid",),
        ) from exc


def _require_behavior_publishable(
    *,
    definition: Mapping[str, Any],
    descriptor: AgentBehaviorDescriptor,
) -> None:
    reasons = descriptor.reason_codes or ("behavior_compile_blocked",)
    if definition.get("primary_capability_ref") is not None and (
        descriptor.primary_capability_ref is None or descriptor.verification_state != "reviewed"
    ):
        raise AgentPublicationBlockedError(
            "primary_capability_unproven",
            reason_codes=reasons,
        )
    if definition.get("capability_composition") is not None and descriptor.verification_state != "reviewed":
        raise AgentPublicationBlockedError(
            "behavior_compile_blocked",
            reason_codes=reasons,
        )


def _public_behavior_reason_codes(
    validation: AgentPublicationValidation,
) -> set[str]:
    reasons: set[str] = set()
    if validation.behavior_primary_requested and validation.behavior_status != "reviewed":
        reasons.add("primary_capability_unproven")
    if validation.behavior_status in {"blocked", "shadow"}:
        reasons.add("behavior_compile_blocked")
    return reasons


def _publication_scanner_version(descriptor: AgentBehaviorDescriptor) -> str:
    return f"{AGENT_PUBLICATION_SAFETY_VERSION}:behavior-{descriptor.behavior_fingerprint[-16:]}"


def _candidate_hash(
    *,
    base_live_release_id: Any,
    audience: AgentAudience,
    behavior: AgentBehaviorDescriptor,
    content_hash: str,
    validation: AgentPublicationValidation,
    eval_evidence: Any,
    release_note: str | None,
) -> str:
    return _hash_json(
        {
            "audience": audience.to_dict(),
            "base_live_release_id": base_live_release_id,
            "behavior_fingerprint": behavior.behavior_fingerprint,
            "behavior_verification_state": behavior.verification_state,
            "capability_fingerprint": validation.compilation.capability_fingerprint,
            "content_hash": content_hash,
            "dependency_fingerprint": validation.compilation.dependency_fingerprint,
            "deterministic": validation.deterministic_dict(),
            "eval_evidence": eval_evidence,
            "release_note": release_note,
            "risk": validation.compilation.risk.to_dict(),
            "safety_status": validation.safety_status,
            "semantic": validation.semantic_dict(),
        },
    )


def _effective_semantic_safety_status(
    semantic_status: SemanticStatus,
    *,
    audience: AgentAudience,
) -> SafetyStatus:
    if semantic_status == "blocked":
        return "blocked"
    if semantic_status == "pass":
        return "pass"
    if audience.mode == "private":
        return "warning"
    return "review_required"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def validated_release_note(value: str | None) -> str | None:
    """A release note as stored: trimmed, empty means none, and safe to show to the Agent's users."""
    note = _release_note(value)
    _require_consumer_safe_release_note(note)
    return note


def _release_note(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_RELEASE_NOTE_LENGTH:
        raise ValueError("Agent release note is empty or too long")
    return normalized


def _require_consumer_safe_release_note(value: str | None) -> None:
    if value is None:
        return
    result = scan_agent_listing(name="", display_name="", description=value)
    if result.blocked:
        raise AgentPublicationBlockedError(
            "agent_release_note_blocked",
            reason_codes=tuple(sorted(result.reason_codes)),
        )


def _validate_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Agent command idempotency key is invalid")


def _names_from_listing(listing: Mapping[str, Any]) -> dict[str, str]:
    display_name = listing.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 120:
        raise ValueError("Agent listing display_name is required and must be <= 120 characters")
    names = {"*": display_name.strip()}
    localized = listing.get("localized_display_names", {})
    if not isinstance(localized, Mapping) or len(localized) > 50:
        raise ValueError("Agent localized display names are malformed")
    for locale, name in localized.items():
        if not isinstance(locale, str) or not _LOCALE_RE.fullmatch(locale) or not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("Agent localized display name is invalid")
        canonical_locale = locale.lower()
        if canonical_locale in names:
            raise ValueError("Agent localized display name locale is duplicated")
        names[canonical_locale] = name.strip()
    return names


def _is_strict_audience_contraction(current: AgentAudience, target: AgentAudience) -> bool:
    if current == target:
        return False
    if current.mode == "teamwide":
        return target.mode in {"custom", "private"}
    if current.mode == "custom":
        if target.mode == "private":
            return True
        return (
            target.mode == "custom"
            and set(target.user_ids).issubset(current.user_ids)
            and set(
                target.group_ids,
            ).issubset(current.group_ids)
        )
    return False


def _preparation_result_dict(result: AgentPreparationResult) -> dict[str, Any]:
    return {
        "agent_id": result.agent_id,
        "aggregate_row_version": result.aggregate_row_version,
        "candidate_hash": result.candidate_hash,
        "candidate_id": result.candidate_id,
        "candidate_row_version": result.candidate_row_version,
        "compatibility_major": result.compatibility_major,
        "compatibility_verdict": result.compatibility_verdict,
        "control_row_version": result.control_row_version,
        "plan_hash": result.plan_hash,
        "plan_id": result.plan_id,
        "policy_hash": result.policy_hash,
        "policy_revision_id": result.policy_revision_id,
        "revision_id": result.revision_id,
        "source_working_copy_hash": result.source_working_copy_hash,
        "state": result.state,
    }


def _preparation_result_from_json(value: str) -> AgentPreparationResult:
    try:
        decoded = json.loads(value)
        verdict = decoded["compatibility_verdict"]
        if not isinstance(decoded, dict) or decoded.get("state") != "prepared" or verdict not in {"compatible", "breaking", "unknown"}:
            raise ValueError
        return AgentPreparationResult(
            state="prepared",
            agent_id=str(decoded["agent_id"]),
            candidate_id=str(decoded["candidate_id"]),
            candidate_hash=str(decoded["candidate_hash"]),
            candidate_row_version=int(decoded["candidate_row_version"]),
            revision_id=str(decoded["revision_id"]),
            compatibility_major=int(decoded["compatibility_major"]),
            compatibility_verdict=verdict,
            policy_revision_id=str(decoded["policy_revision_id"]),
            policy_hash=str(decoded["policy_hash"]),
            plan_id=str(decoded["plan_id"]),
            plan_hash=str(decoded["plan_hash"]),
            control_row_version=int(decoded["control_row_version"]),
            source_working_copy_hash=str(decoded["source_working_copy_hash"]),
            aggregate_row_version=int(decoded["aggregate_row_version"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentRevisionConflictError("agent_command_receipt_malformed") from exc


def _publication_result_dict(result: AgentPublicationResult) -> dict[str, Any]:
    return {
        "agent_id": result.agent_id,
        "aggregate_row_version": result.aggregate_row_version,
        "candidate_id": result.candidate_id,
        "candidate_row_version": result.candidate_row_version,
        "release_id": result.release_id,
        "revision_id": result.revision_id,
        "state": result.state,
    }


def _publication_result_from_json(value: str) -> AgentPublicationResult:
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or decoded.get("state") not in {
        "pending",
        "activated",
        "rejected",
        "withdrawn",
        "stale",
    }:
        raise AgentRevisionConflictError("agent_command_receipt_malformed")
    return AgentPublicationResult(
        state=decoded["state"],
        agent_id=str(decoded["agent_id"]),
        candidate_id=(str(decoded["candidate_id"]) if decoded.get("candidate_id") else None),
        revision_id=str(decoded["revision_id"]),
        release_id=(str(decoded["release_id"]) if decoded.get("release_id") else None),
        candidate_row_version=(int(decoded["candidate_row_version"]) if decoded.get("candidate_row_version") is not None else None),
        aggregate_row_version=int(decoded["aggregate_row_version"]),
    )


__all__ = [
    "AGENT_PUBLICATION_SAFETY_VERSION",
    "AgentAudience",
    "AgentPreparationResult",
    "AgentPublicationBlockedError",
    "AgentMcpEntitlementResolver",
    "AgentReferenceCoverageResolver",
    "AgentPublicationResult",
    "AgentPublicationValidation",
    "AgentPublicationValidator",
    "AgentReleaseStaleError",
    "AgentRevisionConflictError",
    "AgentRevisionError",
    "AgentRevisionForbiddenError",
    "AgentRevisionStore",
    "AgentWorkingCopy",
    "DeterministicAgentPublicationValidator",
]
