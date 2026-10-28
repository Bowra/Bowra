"""Static Stage 0A inventory for Subagent definition and grant write ingress."""

from __future__ import annotations

import ast
import hashlib
import platform
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_SYNTHETIC_SOURCE_EXCLUSIONS = frozenset(
    {
        "packages/bowhead_cli/src/bowhead_cli/subagent_access_spike.py",
        "packages/bowhead_cli/src/bowhead_cli/subagent_benchmark.py",
        "packages/bowhead_cli/src/bowhead_cli/subagent_dependency_spike.py",
        "packages/bowhead_cli/src/bowhead_cli/subagent_trigger_spike.py",
        "packages/bowhead_cli/src/bowhead_cli/subagent_write_ingress_spike.py",
    }
)
_MUTATOR_CALLEES = frozenset(
    {
        "add_grant",
        "compare_and_update_team_subagent",
        "create_or_update_resource_grant",
        "create_team_subagent",
        "delete_team_subagent",
        "_mutate_subagent",
        "prepare_member_deactivation_conn",
        "remove_grant",
        "replace_team_subagent_audience",
        "revoke_grants_for_resource",
        "revoke_resource_grant",
        "set_audience",
        "set_audience_tx",
        "update_team_subagent_meta",
    }
)
_SUBAGENT_ONLY_CALLEES = frozenset(
    {
        "compare_and_update_team_subagent",
        "create_team_subagent",
        "delete_team_subagent",
        "_mutate_subagent",
        "prepare_member_deactivation_conn",
        "replace_team_subagent_audience",
        "update_team_subagent_meta",
    }
)
_SQL_MUTATION_PATTERN = re.compile(
    r"\b(?P<operation>INSERT\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+(?P<table>team_subagent|resource_grants)\b",
    re.IGNORECASE,
)
_DYNAMIC_VISIBILITY_PATTERN = re.compile(
    r"\bUPDATE\s+\{visibility_table\}\s+SET\b",
    re.IGNORECASE,
)
_STABLE_REJECTION_GUARDS = frozenset(
    {
        "_reject_obsolete_team_subagent_write",
        "reject_obsolete_team_subagent_write",
    }
)
_TOKEN_GUARD = "require_team_subagent_transaction_token"
_TOKEN_ISSUER = "_issue_team_subagent_transaction_token"
# The two Stores that own a Subagent aggregate transaction.  TeamSubagentStore owns
# the legacy/definition aggregate; AgentRevisionStore owns M2 revision, release,
# archive/restore and audience-contraction transactions, and passes the token only
# to AccessControlStore's private ``_conn`` grant primitives after it has taken
# BEGIN IMMEDIATE and rechecked the owner or qualified reviewer.  Sorted by path so
# the comparison matches scan order.
_TOKEN_OWNERS: tuple[tuple[str, str], ...] = (
    ("packages/bowhead_core/src/bowhead_core/agent_revision_store.py", "AgentRevisionStore"),
    ("packages/bowhead_core/src/bowhead_core/team_subagent_store.py", "TeamSubagentStore"),
)
# Token instance attribute: only methods of the two owner classes above may read `self._transaction_token`; any external
# `x._transaction_token` access (e.g. AgentVersionService using `self._revision_store._transaction_token`
# to pass the token to generic authorization primitives) fails readiness.
_TOKEN_ATTRIBUTE = "_transaction_token"
_REVISION_STORE_PATH = "packages/bowhead_core/src/bowhead_core/agent_revision_store.py"
# Register each external access to AgentRevisionStore private members (`path:caller symbol:member`, an exact set — additions or
# removals fail readiness). Within its own BEGIN IMMEDIATE, AgentVersionService reuses the revision store's
# `_*_conn` primitives and checks (owner / qualified reviewer / owner still active / lifecycle; see _activate and
# submit_for_review; create_override only writes audit records, reviewer_projection only reads); AgentChangeProposalStore
# only reuses the behavior authoring service. These accesses neither handle the transaction token nor directly call AccessControlStore authorization primitives.
_REGISTERED_REVISION_STORE_PRIVATE_ACCESS: frozenset[str] = frozenset(
    {
        "packages/bowhead_core/src/bowhead_core/agent_change_proposal_store.py:AgentChangeProposalStore.__init__:_behavior_authoring",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_activate_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_aggregate_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_candidate_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_candidate_drift_reasons_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_mark_stale_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_owned_aggregate_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_owner_id",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_qualified_reviewer_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_receipt_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_require_owner_active_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_require_publishable",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_require_revisioned_active",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_revision_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_store_receipt_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_validate_audience_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService._activate:_validator",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.create_override:_write_audit_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.reviewer_projection:_qualified_reviewer_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_candidate_drift_reasons_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_enqueue_reviewers_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_has_qualified_reviewer_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_insert_event_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_mark_stale_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_owned_aggregate_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_owner_id",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_receipt_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_require_publishable",
        # Read-only lifecycle check (archived / non-revisioned Agents cannot be submitted), the same
        # gate _activate already registers; it runs inside submit_for_review's BEGIN IMMEDIATE and
        # neither touches the transaction token nor writes grants.
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_require_revisioned_active",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_revision_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_store_receipt_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_validate_audience_conn",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_validator",
        "packages/bowhead_core/src/bowhead_core/agent_version_service.py:AgentVersionService.submit_for_review:_write_audit_conn",
    }
)


@dataclass(frozen=True)
class _Disposition:
    current_boundary: str
    release_a_disposition: str
    rationale: str
    blocking: bool = True


@dataclass(frozen=True)
class WriteIngressFinding:
    path: str
    symbol: str
    line: int
    finding_kind: str
    target: str
    operation: str
    subagent_scope: str
    current_boundary: str
    release_a_disposition: str
    rationale: str
    blocking: bool
    classified: bool


@dataclass(frozen=True)
class SubagentWriteIngressReport:
    schema_version: int
    scope: str
    source_state: str
    methodology: dict[str, str]
    environment: dict[str, str]
    findings: list[dict[str, Any]]
    excluded_synthetic_sources: list[str]
    non_subagent_mutator_calls: int
    summary: dict[str, Any]
    release_a_readiness: dict[str, Any]
    checks: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SQL_DISPOSITIONS: dict[tuple[str, str, str, str], _Disposition] = {
    (
        "packages/bowhead_core/src/bowhead_core/account_deletion.py",
        "retire_account_conn",
        "resource_grants",
        "update",
    ): _Disposition(
        current_boundary="caller_owned_account_retirement_transaction",
        release_a_disposition="caller_owned_team_subagent_transaction_primitive",
        rationale="account retirement revokes every grant whose principal is the retired user after marking the user deleted and invalidating sessions; every caller (request_account_deletion, deleted-account adoption, ledger restore and admin member deletion) runs it inside its own BEGIN IMMEDIATE",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/agent_release_baseline.py",
        "AgentReleaseBaselineStore._quarantine_unsafe",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_release_baseline_transaction",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale=(
            "offline baseline import quarantines an Agent whose compiled definition fails the safety gate; "
            "owns its own BEGIN IMMEDIATE and is reachable only from the CLI/startup import, never from a subagent turn"
        ),
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/agent_release_baseline.py",
        "AgentReleaseBaselineStore._import_one_conn",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_release_baseline_transaction",
        release_a_disposition="caller_owned_team_subagent_transaction_primitive",
        rationale=(
            "private legacy-to-revisioned pointer attach; the only caller is AgentReleaseBaselineStore.apply, which requires "
            "writer_stopped, holds the run lease and BEGIN IMMEDIATE, and CAS-checks row_version/updated_at against the frozen "
            "inventory; reachable only from the admin CLI, never from a gateway route or subagent turn"
        ),
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/agent_revision_store.py",
        "AgentRevisionStore.archive_agent",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_agent_revision_transaction",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale=(
            "owner and active-membership recheck, aggregate row_version CAS, pending-candidate withdrawal, schedule pause, "
            "allow-grant revoke, policy epoch bump and AGENT_ARCHIVED audit share one BEGIN IMMEDIATE transaction"
        ),
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/agent_revision_store.py",
        "AgentRevisionStore.restore_agent",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_agent_revision_transaction",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale=(
            "owner and active-membership recheck, current-time safety revalidation of the live release, name claim, "
            "exact audience replacement, row_version CAS and AGENT_RESTORED audit share one BEGIN IMMEDIATE transaction"
        ),
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/agent_revision_store.py",
        "AgentRevisionStore.contract_audience",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_agent_revision_transaction",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale=(
            "owner recheck, idempotency receipt, strict-contraction check, new release row, exact audience replacement, "
            "row_version/live_release CAS and AGENT_AUDIENCE_CONTRACTED audit share one BEGIN IMMEDIATE transaction"
        ),
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/agent_revision_store.py",
        "AgentRevisionStore._project_live_conn",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_agent_revision_transaction",
        release_a_disposition="caller_owned_team_subagent_transaction_primitive",
        rationale=(
            "private live-projection primitive reachable only through AgentRevisionStore._activate_conn, whose callers are "
            "approve_candidate (qualified reviewer, owner-unchanged and "
            "owner-active recheck) under their own BEGIN IMMEDIATE, initialize_created_agent_conn inside "
            "TeamSubagentStore.create_team_subagent's transaction, and AgentVersionService._activate (approve_shared / "
            "activate_private) which under its own BEGIN IMMEDIATE applies the same "
            "revisioned-active lifecycle check on both paths plus the reviewer, owner-unchanged and owner-active rechecks; "
            "the UPDATE CAS-checks row_version and live_release_id"
        ),
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
        "TeamSubagentStore.prepare_member_deactivation_conn",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_member_deactivation_transaction",
        release_a_disposition="caller_owned_team_subagent_transaction_primitive",
        rationale="private SQL primitive is inventoried with every callsite; the approved UserStore caller must prove BEGIN IMMEDIATE before delegation",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
        "TeamSubagentStore.create_team_subagent",
        "team_subagent",
        "insert",
    ): _Disposition(
        current_boundary="canonical_team_subagent_store",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale="definition, initial audience and route audit share one BEGIN IMMEDIATE transaction",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
        "TeamSubagentStore.create_team_subagent",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_team_subagent_store",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale="new audience_mode and legacy visibility are synchronized in the create transaction when the additive column exists",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
        "TeamSubagentStore.update_team_subagent_meta",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_team_subagent_store",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale="legacy-compatible partial update and route audit share one transaction",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
        "TeamSubagentStore.compare_and_update_team_subagent",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_team_subagent_store",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale="version compare, semantic update and route audit share one BEGIN IMMEDIATE transaction",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
        "TeamSubagentStore.delete_team_subagent",
        "team_subagent",
        "delete",
    ): _Disposition(
        current_boundary="canonical_team_subagent_store",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale="definition, grant cleanup and route audit commit or roll back together",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
        "TeamSubagentStore.replace_team_subagent_audience",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_exact_audience_transaction",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale="Owner recheck, exact allow replacement, projections, version, policy epoch and audit share one BEGIN IMMEDIATE transaction",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/access_control.py",
        "AccessControlStore.create_or_update_resource_grant",
        "resource_grants",
        "update",
    ): _Disposition(
        current_boundary="token_guarded_generic_grant_repository",
        release_a_disposition="typed_internal_transaction_token_enforced",
        rationale="Subagent rows require the TeamSubagentStore capability before SQL executes",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/access_control.py",
        "AccessControlStore.create_or_update_resource_grant",
        "resource_grants",
        "insert",
    ): _Disposition(
        current_boundary="token_guarded_generic_grant_repository",
        release_a_disposition="typed_internal_transaction_token_enforced",
        rationale="Subagent rows require the TeamSubagentStore capability before SQL executes",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/access_control.py",
        "AccessControlStore.revoke_resource_grant",
        "resource_grants",
        "update",
    ): _Disposition(
        current_boundary="token_guarded_generic_grant_repository",
        release_a_disposition="typed_internal_transaction_token_enforced",
        rationale="grant-id revoke resolves resource kind before requiring the capability",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/access_control.py",
        "AccessControlStore._revoke_grants_for_resource_conn",
        "resource_grants",
        "update",
    ): _Disposition(
        current_boundary="token_guarded_private_grant_primitive",
        release_a_disposition="typed_internal_transaction_token_enforced",
        rationale="bulk revoke is private and rejects a missing Subagent transaction capability",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/access_control.py",
        "AccessControlStore._replace_team_subagent_audience_conn",
        "resource_grants",
        "insert",
    ): _Disposition(
        current_boundary="token_guarded_private_exact_audience_primitive",
        release_a_disposition="typed_internal_transaction_token_enforced",
        rationale="exact allow insertion is private, non-committing, and requires the TeamSubagentStore transaction capability",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/access_control.py",
        "AccessControlStore._append_team_subagent_audience_user_conn",
        "resource_grants",
        "insert",
    ): _Disposition(
        current_boundary="token_guarded_private_audience_append_primitive",
        release_a_disposition="typed_internal_transaction_token_enforced",
        rationale="single canonical use-allow insertion is private, non-committing, and requires the TeamSubagentStore transaction capability",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/team_subagent_store.py",
        "TeamSubagentStore.add_team_subagent_audience_user",
        "team_subagent",
        "update",
    ): _Disposition(
        current_boundary="canonical_audience_append_transaction",
        release_a_disposition="canonical_team_subagent_transaction",
        rationale="access-request approval: Owner recheck, member validation, one appended allow, projection, version, policy epoch and audit share one BEGIN IMMEDIATE transaction; refused after the release cutover",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/access_control.py",
        "AccessControlStore._set_audience_conn",
        "resource_grants",
        "update",
    ): _Disposition(
        current_boundary="token_guarded_private_audience_primitive",
        release_a_disposition="typed_internal_transaction_token_enforced",
        rationale="same-connection audience primitive requires the Subagent transaction capability",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/access_control.py",
        "AccessControlStore._set_audience_conn",
        "resource_grants",
        "insert",
    ): _Disposition(
        current_boundary="token_guarded_private_audience_primitive",
        release_a_disposition="typed_internal_transaction_token_enforced",
        rationale="same-connection audience primitive requires the Subagent transaction capability",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/access_control.py",
        "AccessControlStore._set_audience_conn",
        "team_subagent_via_visibility_table",
        "update",
    ): _Disposition(
        current_boundary="token_guarded_dynamic_visibility_projection",
        release_a_disposition="typed_internal_transaction_token_enforced",
        rationale="legacy visibility projection is reachable only from the aggregate transaction",
        blocking=False,
    ),
}

_CALL_DISPOSITIONS: dict[tuple[str, str, str], _Disposition] = {
    (
        "packages/bowhead_core/src/bowhead_core/account_deletion.py",
        "retire_account_conn",
        "prepare_member_deactivation_conn",
    ): _Disposition(
        current_boundary="canonical_member_deactivation_transaction",
        release_a_disposition="caller_owned_team_subagent_transaction_primitive",
        rationale="request-time retirement runs the shared-agent blocker scan and private quarantine on the caller's BEGIN IMMEDIATE connection and rejects with account_resource_transfer_required while an enabled shared Agent remains",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/user_store.py",
        "UserStore.admin_soft_delete_member_detailed",
        "prepare_member_deactivation_conn",
    ): _Disposition(
        current_boundary="canonical_member_deactivation_transaction",
        release_a_disposition="delegated_to_caller_owned_transaction",
        rationale="shared-agent blocker scan, private quarantine, allow cleanup, membership removal and audit run after BEGIN IMMEDIATE on the same connection",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/user_store.py",
        "UserStore.create_team_subagent",
        "create_team_subagent",
    ): _Disposition(
        current_boundary="legacy_user_store_compatibility_delegate",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale="legacy method signature remains while all write SQL is owned by TeamSubagentStore",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/user_store.py",
        "UserStore.update_team_subagent_meta",
        "update_team_subagent_meta",
    ): _Disposition(
        current_boundary="legacy_user_store_compatibility_delegate",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale="omitted-versus-null compatibility is translated before the canonical Store call",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/user_store.py",
        "UserStore.delete_team_subagent",
        "delete_team_subagent",
    ): _Disposition(
        current_boundary="legacy_user_store_compatibility_delegate",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale="legacy delete is contained by the canonical Store boundary",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/share_service.py",
        "set_audience",
        "set_audience_tx",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="mode-only Subagent API rejects before reaching the generic repository",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/share_service.py",
        "add_grant",
        "create_or_update_resource_grant",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="incremental Subagent allow rejects before reaching the generic repository",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/share_service.py",
        "remove_grant",
        "revoke_resource_grant",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="Subagent grant removal rejects before reaching the generic repository",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "create_subagent",
        "create_team_subagent",
    ): _Disposition(
        current_boundary="canonical_team_subagent_http_delegate",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale="create, initial audience and audit run through the canonical Store transaction",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagent_drafts.py",
        "create_from_draft",
        "create_team_subagent",
    ): _Disposition(
        current_boundary="canonical_team_subagent_http_delegate",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale="conversational-draft create runs through the same canonical Store transaction as the direct route, and adds require_active_owner plus the draft-consumption CAS (source_draft_id/expected_version/definition_fingerprint) so attribution is atomic with the insert",
        blocking=False,
    ),
    (
        "packages/bowhead_core/src/bowhead_core/agent_fork_service.py",
        "AgentForkService.fork",
        "create_team_subagent",
    ): _Disposition(
        current_boundary="canonical_team_subagent_fork_delegate",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale=(
            "same-team Fork creates a private copy through the canonical Store transaction with require_active_owner and "
            "a fork_commit that re-admits source status, live revision/release, owner, fork policy version, consume access "
            "and snapshot hash under the create lock before the AGENT_FORKED audit"
        ),
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "_write_through_team_wide",
        "set_audience",
    ): _Disposition(
        current_boundary="best_effort_post_create_side_write",
        release_a_disposition="remove_after_atomic_create_delegate",
        rationale="best-effort audience write can leave definition and grants inconsistent",
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "patch_subagent",
        "update_team_subagent_meta",
    ): _Disposition(
        current_boundary="canonical_team_subagent_http_delegate",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale="Release A preserves PATCH semantics while centralizing the write and audit",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "patch_subagent_configuration",
        "_mutate_subagent",
    ): _Disposition(
        current_boundary="versioned_team_subagent_http_delegate",
        release_a_disposition="delegated_to_versioned_team_subagent_mutation",
        rationale="configuration PATCH delegates authorization, If-Match and mutation to the shared adapter",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "put_subagent_status",
        "_mutate_subagent",
    ): _Disposition(
        current_boundary="versioned_team_subagent_http_delegate",
        release_a_disposition="delegated_to_versioned_team_subagent_mutation",
        rationale="status PUT delegates authorization, If-Match and mutation to the shared adapter",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "put_subagent_audience",
        "replace_team_subagent_audience",
    ): _Disposition(
        current_boundary="canonical_exact_audience_http_delegate",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale="Owner authorization and If-Match preconditions delegate one exact roster to the canonical aggregate transaction",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "patch_subagent",
        "_mutate_subagent",
    ): _Disposition(
        current_boundary="deprecated_versioned_team_subagent_http_delegate",
        release_a_disposition="delegated_to_versioned_team_subagent_mutation",
        rationale="deprecated combined PATCH preserves compatibility through the shared versioned adapter",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "_mutate_subagent",
        "compare_and_update_team_subagent",
    ): _Disposition(
        current_boundary="canonical_versioned_team_subagent_http_adapter",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale="authorized If-Match mutations use the canonical compare-and-update Store transaction",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "delete_subagent",
        "delete_team_subagent",
    ): _Disposition(
        current_boundary="canonical_team_subagent_http_delegate",
        release_a_disposition="delegated_to_team_subagent_store",
        rationale="delete, grant cleanup and audit run through the canonical Store transaction",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
        "delete_subagent",
        "revoke_grants_for_resource",
    ): _Disposition(
        current_boundary="best_effort_post_delete_cleanup",
        release_a_disposition="move_inside_lifecycle_transaction",
        rationale="best-effort cascade can leave active grants for a deleted object",
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py",
        "put_audience",
        "set_audience",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="generic request rejects Subagent after authorization and before mutation",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py",
        "create_share_grant",
        "add_grant",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="incremental Subagent add rejects after authorization and before mutation",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py",
        "revoke_share_grant",
        "set_audience",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="Subagent team-grant revoke rejects after authorization and before mutation",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py",
        "revoke_share_grant",
        "remove_grant",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="incremental Subagent revoke rejects after authorization and before mutation",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/access_control.py",
        "upsert_resource_grant",
        "set_audience",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="admin Subagent audience mutation rejects after canonical resource resolution",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/access_control.py",
        "upsert_resource_grant",
        "create_or_update_resource_grant",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="admin Subagent allow and deny reject before the generic repository",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/access_control.py",
        "revoke_resource_grant",
        "set_audience",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="admin Subagent audience revoke rejects after grant resolution",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/access_control.py",
        "revoke_resource_grant",
        "revoke_resource_grant",
    ): _Disposition(
        current_boundary="stable_obsolete_api_rejection",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="admin Subagent allow and deny revoke reject before the generic repository",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/access_requests.py",
        "_apply_grant",
        "add_grant",
    ): _Disposition(
        current_boundary="access_request_resource_grant",
        release_a_disposition="stable_rejection_before_generic_mutator",
        rationale="team_subagent approvals delegate to TeamSubagentStore.add_team_subagent_audience_user; the generic grant is guarded by the stable rejection",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/files.py",
        "share_file",
        "create_or_update_resource_grant",
    ): _Disposition(
        current_boundary="artifact_grant_scope",
        release_a_disposition="proven_non_subagent_scope",
        rationale="resource_kind is the files_sharing ARTIFACT_RESOURCE_KIND constant",
        blocking=False,
    ),
    (
        "packages/bowhead_gateway/src/bowhead_gateway/routes/files.py",
        "unshare_file",
        "revoke_resource_grant",
    ): _Disposition(
        current_boundary="artifact_grant_scope",
        release_a_disposition="proven_non_subagent_scope",
        rationale="grant is loaded from an artifact-kind filtered list before revoke",
        blocking=False,
    ),
}

_EXPECTED_RISK_SURFACES = frozenset(
    {
        (
            "packages/bowhead_core/src/bowhead_core/user_store.py",
            "UserStore.create_team_subagent",
        ),
        (
            "packages/bowhead_core/src/bowhead_core/user_store.py",
            "UserStore.update_team_subagent_meta",
        ),
        (
            "packages/bowhead_core/src/bowhead_core/user_store.py",
            "UserStore.delete_team_subagent",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
            "create_subagent",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
            "_mutate_subagent",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
            "patch_subagent_configuration",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
            "put_subagent_status",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
            "patch_subagent",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/team_subagents.py",
            "delete_subagent",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py",
            "put_audience",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py",
            "create_share_grant",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/shares.py",
            "revoke_share_grant",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/access_control.py",
            "upsert_resource_grant",
        ),
        (
            "packages/bowhead_gateway/src/bowhead_gateway/routes/access_control.py",
            "revoke_resource_grant",
        ),
    }
)


def audit_subagent_write_ingress(
    repo_root: Path | None = None,
) -> SubagentWriteIngressReport:
    """Scan repository writers and classify every Subagent-capable ingress."""
    root = (repo_root or _default_repo_root()).resolve()
    files = _source_files(root)
    findings: list[WriteIngressFinding] = []
    non_subagent_mutator_calls = 0
    parse_failures: list[str] = []
    enforcement_evidence_failures: list[str] = []
    token_issuers: list[tuple[str, str]] = []
    private_token_imports: list[tuple[str, str]] = []
    private_token_attribute_references: list[tuple[str, str]] = []
    token_attribute_escapes: list[str] = []
    revision_store_private_accesses: set[str] = set()

    for path in files:
        relative_path = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        scopes: list[tuple[int, int, str]] = []
        visitor: _PythonIngressVisitor | None = None
        if path.suffix == ".py":
            try:
                tree = ast.parse(source, filename=relative_path)
            except SyntaxError:
                parse_failures.append(relative_path)
                continue
            visitor = _PythonIngressVisitor(relative_path)
            visitor.visit(tree)
            scopes = visitor.scopes
            token_issuers.extend((relative_path, str(issuer["symbol"])) for issuer in visitor.token_issuers)
            private_token_imports.extend((relative_path, str(imported_name)) for imported_name in visitor.private_token_imports)
            owner_classes = {owner for owner_path, owner in _TOKEN_OWNERS if owner_path == relative_path}
            token_attribute_escapes.extend(
                f"{relative_path}:{reference['symbol']}:{reference['line']}"
                for reference in visitor.transaction_token_attribute_references
                if str(reference["symbol"]).split(".", 1)[0] not in owner_classes
            )
            if relative_path != _REVISION_STORE_PATH:
                revision_store_private_accesses.update(f"{relative_path}:{access}" for access in visitor.revision_store_private_accesses)
            private_token_attribute_references.extend(
                (relative_path, str(reference["symbol"])) for reference in visitor.private_token_attribute_references
            )
            for call in visitor.calls:
                if call["subagent_scope"] == "non_subagent":
                    non_subagent_mutator_calls += 1
                    continue
                key = (
                    relative_path,
                    str(call["symbol"]),
                    str(call["callee"]),
                )
                disposition = _CALL_DISPOSITIONS.get(key)
                if (
                    disposition is not None
                    and disposition.release_a_disposition == "stable_rejection_before_generic_mutator"
                    and not visitor.has_prior_guard(
                        str(call["symbol"]),
                        int(call["line"]),
                        _STABLE_REJECTION_GUARDS,
                    )
                ):
                    enforcement_evidence_failures.append(
                        f"{relative_path}:{call['symbol']}:{call['callee']}:stable_rejection",
                    )
                    disposition = None
                if (
                    disposition is not None
                    and disposition.release_a_disposition == "delegated_to_caller_owned_transaction"
                    and not visitor.has_prior_transaction_start(
                        str(call["symbol"]),
                        int(call["line"]),
                    )
                ):
                    enforcement_evidence_failures.append(
                        f"{relative_path}:{call['symbol']}:{call['callee']}:begin_immediate",
                    )
                    disposition = None
                findings.append(
                    _finding(
                        path=relative_path,
                        symbol=str(call["symbol"]),
                        line=int(call["line"]),
                        finding_kind="mutator_call",
                        target=str(call["callee"]),
                        operation="call",
                        subagent_scope=str(call["subagent_scope"]),
                        disposition=disposition,
                    )
                )

        for match in _SQL_MUTATION_PATTERN.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            operation = _normalize_sql_operation(match.group("operation"))
            table = match.group("table").lower()
            symbol = _symbol_for_line(scopes, line) if scopes else "<sql_file>"
            disposition = _SQL_DISPOSITIONS.get(
                (relative_path, symbol, table, operation),
            )
            disposition = _sql_disposition_with_evidence(
                disposition,
                visitor,
                symbol,
                line,
                relative_path,
                table,
                operation,
                enforcement_evidence_failures,
            )
            findings.append(
                _finding(
                    path=relative_path,
                    symbol=symbol,
                    line=line,
                    finding_kind="direct_sql",
                    target=table,
                    operation=operation,
                    subagent_scope=("subagent" if table == "team_subagent" else "potential_subagent"),
                    disposition=disposition,
                )
            )
        for match in _DYNAMIC_VISIBILITY_PATTERN.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            symbol = _symbol_for_line(scopes, line)
            disposition = _SQL_DISPOSITIONS.get(
                (
                    relative_path,
                    symbol,
                    "team_subagent_via_visibility_table",
                    "update",
                )
            )
            disposition = _sql_disposition_with_evidence(
                disposition,
                visitor,
                symbol,
                line,
                relative_path,
                "team_subagent_via_visibility_table",
                "update",
                enforcement_evidence_failures,
            )
            findings.append(
                _finding(
                    path=relative_path,
                    symbol=symbol,
                    line=line,
                    finding_kind="dynamic_sql",
                    target="team_subagent_via_visibility_table",
                    operation="update",
                    subagent_scope="potential_subagent",
                    disposition=disposition,
                )
            )

    findings.sort(
        key=lambda finding: (
            finding.path,
            finding.line,
            finding.finding_kind,
            finding.target,
        )
    )
    unknown = [finding for finding in findings if not finding.classified]
    blockers = [finding for finding in findings if finding.blocking]
    surfaces = {(finding.path, finding.symbol) for finding in findings}
    scripts_findings = [finding for finding in findings if finding.path.startswith("scripts/") and finding.subagent_scope != "non_subagent"]
    disposition_counts = Counter(finding.release_a_disposition for finding in findings)
    semantic_keys = [
        "|".join(
            (
                finding.path,
                finding.symbol,
                finding.finding_kind,
                finding.target,
                finding.operation,
                finding.release_a_disposition,
            )
        )
        for finding in findings
    ]
    checks = {
        "all_python_sources_parsed": not parse_failures,
        "all_relevant_findings_classified": not unknown,
        "known_bypass_surfaces_observed": surfaces >= _EXPECTED_RISK_SURFACES,
        "repository_scripts_have_no_subagent_writes": not scripts_findings,
        "synthetic_source_exclusions_are_exact": all((root / relative_path).is_file() for relative_path in _SYNTHETIC_SOURCE_EXCLUSIONS),
        "release_a_dispositions_are_explicit": all(finding.release_a_disposition not in {"", "unclassified"} for finding in findings),
        "enforcement_evidence_is_present": not enforcement_evidence_failures,
        "transaction_token_issuer_is_confined": token_issuers == [(path, f"{owner}.__init__") for path, owner in _TOKEN_OWNERS],
        "transaction_token_private_import_is_confined": private_token_imports == [(path, _TOKEN_ISSUER) for path, _owner in _TOKEN_OWNERS],
        "transaction_token_private_attribute_is_confined": (not private_token_attribute_references),
        "transaction_token_instance_attribute_is_confined": not token_attribute_escapes,
        "revision_store_private_access_is_registered": revision_store_private_accesses == _REGISTERED_REVISION_STORE_PRIVATE_ACCESS,
    }
    return SubagentWriteIngressReport(
        schema_version=2,
        scope="repository_static_write_ingress_inventory",
        source_state="working_tree_including_uncommitted_files",
        methodology={
            "source_scope": "packages/*/src Python plus repository scripts/schema/deploy SQL; tests excluded",
            "sql": "literal team_subagent/resource_grants mutations plus dynamic visibility-table update",
            "calls": "AST inventory of Subagent-specific and generic grant mutator callsites",
            "classification": "every Subagent-capable finding requires a disposition plus source-level enforcement evidence",
            "synthetic": "five named isolated benchmark/spike modules are excluded, never a wildcard",
            "production_mutation": "none",
        },
        environment={
            "python": platform.python_version(),
            "platform": platform.system(),
            "architecture": platform.machine(),
        },
        findings=[asdict(finding) for finding in findings],
        excluded_synthetic_sources=sorted(_SYNTHETIC_SOURCE_EXCLUSIONS),
        non_subagent_mutator_calls=non_subagent_mutator_calls,
        summary={
            "source_files_scanned": len(files),
            "relevant_findings": len(findings),
            "direct_sql_findings": sum(finding.finding_kind in {"direct_sql", "dynamic_sql"} for finding in findings),
            "mutator_call_findings": sum(finding.finding_kind == "mutator_call" for finding in findings),
            "blocking_findings": len(blockers),
            "unclassified_findings": len(unknown),
            "parse_failures": parse_failures,
            "enforcement_evidence_failures": enforcement_evidence_failures,
            "transaction_token_issuers": [{"path": path, "symbol": symbol} for path, symbol in token_issuers],
            "transaction_token_private_imports": [{"path": path, "name": name} for path, name in private_token_imports],
            "transaction_token_private_attribute_references": [
                {"path": path, "symbol": symbol} for path, symbol in private_token_attribute_references
            ],
            "transaction_token_attribute_escapes": sorted(token_attribute_escapes),
            "unregistered_revision_store_private_access": sorted(revision_store_private_accesses - _REGISTERED_REVISION_STORE_PRIVATE_ACCESS),
            "stale_revision_store_private_access": sorted(_REGISTERED_REVISION_STORE_PRIVATE_ACCESS - revision_store_private_accesses),
            "dispositions": dict(sorted(disposition_counts.items())),
            "inventory_fingerprint": hashlib.sha256("\n".join(semantic_keys).encode("utf-8")).hexdigest(),
        },
        release_a_readiness={
            "ready": (not blockers and not unknown and not parse_failures and all(checks.values())),
            "blocking_findings": len(blockers),
            "required_outcome": "all blockers delegated, rejected, or restricted to typed internal transactions",
        },
        checks=checks,
    )


class _PythonIngressVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scopes: list[tuple[int, int, str]] = []
        self.calls: list[dict[str, Any]] = []
        self.guards: list[dict[str, Any]] = []
        self.transaction_starts: list[dict[str, Any]] = []
        self.token_issuers: list[dict[str, Any]] = []
        self.private_token_imports: list[str] = []
        self.private_token_attribute_references: list[dict[str, Any]] = []
        self.transaction_token_attribute_references: list[dict[str, Any]] = []
        self.revision_store_private_accesses: set[str] = set()
        self._scope_names: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope_names.append(node.name)
        self.generic_visit(node)
        self._scope_names.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            if imported.name in {_TOKEN_ISSUER, "_TRANSACTION_TOKEN"}:
                self.private_token_imports.append(imported.name)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        symbol = ".".join(self._scope_names) or "<module>"
        if node.attr == _TOKEN_ATTRIBUTE:
            self.transaction_token_attribute_references.append({"symbol": symbol, "line": node.lineno})
        if node.attr.startswith("_") and not node.attr.startswith("__") and _mentions_revision_store(node.value):
            self.revision_store_private_accesses.add(f"{symbol}:{node.attr}")
        if node.attr == "_TRANSACTION_TOKEN":
            self.private_token_attribute_references.append(
                {
                    "symbol": ".".join(self._scope_names) or "<module>",
                    "line": node.lineno,
                }
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = _callee_name(node.func)
        symbol = ".".join(self._scope_names) or "<module>"
        if callee in _STABLE_REJECTION_GUARDS or callee == _TOKEN_GUARD:
            self.guards.append(
                {
                    "symbol": symbol,
                    "line": node.lineno,
                    "callee": callee,
                }
            )
        if callee == _TOKEN_ISSUER:
            self.token_issuers.append(
                {
                    "symbol": symbol,
                    "line": node.lineno,
                }
            )
        if (
            callee == "execute"
            and node.args
            and _literal_sql(node.args[0]).startswith(
                "BEGIN IMMEDIATE",
            )
        ):
            self.transaction_starts.append(
                {
                    "symbol": symbol,
                    "line": node.lineno,
                }
            )
        if callee in _MUTATOR_CALLEES:
            self.calls.append(
                {
                    "path": self.path,
                    "symbol": symbol,
                    "line": node.lineno,
                    "callee": callee,
                    "subagent_scope": _call_subagent_scope(node, callee),
                }
            )
        self.generic_visit(node)

    def has_prior_guard(
        self,
        symbol: str,
        line: int,
        guard_names: frozenset[str],
    ) -> bool:
        return any(guard["symbol"] == symbol and int(guard["line"]) < line and str(guard["callee"]) in guard_names for guard in self.guards)

    def has_prior_transaction_start(self, symbol: str, line: int) -> bool:
        return any(start["symbol"] == symbol and int(start["line"]) < line for start in self.transaction_starts)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._scope_names.append(node.name)
        symbol = ".".join(self._scope_names)
        self.scopes.append(
            (
                node.lineno,
                node.end_lineno or node.lineno,
                symbol,
            )
        )
        self.generic_visit(node)
        self._scope_names.pop()


def _sql_disposition_with_evidence(
    disposition: _Disposition | None,
    visitor: _PythonIngressVisitor | None,
    symbol: str,
    line: int,
    path: str,
    target: str,
    operation: str,
    failures: list[str],
) -> _Disposition | None:
    if disposition is None:
        return None
    if disposition.release_a_disposition == "typed_internal_transaction_token_enforced":
        if visitor is None or not visitor.has_prior_guard(
            symbol,
            line,
            frozenset({_TOKEN_GUARD}),
        ):
            failures.append(
                f"{path}:{symbol}:{target}:{operation}:transaction_token",
            )
            return None
    if disposition.release_a_disposition == "canonical_team_subagent_transaction":
        if visitor is None or not visitor.has_prior_transaction_start(symbol, line):
            failures.append(
                f"{path}:{symbol}:{target}:{operation}:begin_immediate",
            )
            return None
    return disposition


def _finding(
    *,
    path: str,
    symbol: str,
    line: int,
    finding_kind: str,
    target: str,
    operation: str,
    subagent_scope: str,
    disposition: _Disposition | None,
) -> WriteIngressFinding:
    if disposition is None:
        return WriteIngressFinding(
            path=path,
            symbol=symbol,
            line=line,
            finding_kind=finding_kind,
            target=target,
            operation=operation,
            subagent_scope=subagent_scope,
            current_boundary="unknown",
            release_a_disposition="unclassified",
            rationale="new or moved writer requires explicit review",
            blocking=True,
            classified=False,
        )
    return WriteIngressFinding(
        path=path,
        symbol=symbol,
        line=line,
        finding_kind=finding_kind,
        target=target,
        operation=operation,
        subagent_scope=subagent_scope,
        current_boundary=disposition.current_boundary,
        release_a_disposition=disposition.release_a_disposition,
        rationale=disposition.rationale,
        blocking=disposition.blocking,
        classified=True,
    )


def _source_files(root: Path) -> list[Path]:
    candidates: set[Path] = set()
    for pattern in (
        "packages/*/src/**/*.py",
        "scripts/**/*.py",
        "scripts/**/*.sql",
        "schema/**/*.sql",
        "deploy/**/*.sql",
    ):
        candidates.update(path for path in root.glob(pattern) if path.is_file())
    return sorted(
        path
        for path in candidates
        if path.relative_to(root).as_posix() not in _SYNTHETIC_SOURCE_EXCLUSIONS and "tests" not in path.relative_to(root).parts
    )


def _call_subagent_scope(node: ast.Call, callee: str) -> str:
    if callee in _SUBAGENT_ONLY_CALLEES:
        return "subagent"
    value: ast.AST | None = None
    keyword_name = {
        "add_grant": "kind",
        "create_or_update_resource_grant": "resource_kind",
        "set_audience": "kind",
        "set_audience_tx": "resource_kind",
    }.get(callee)
    if keyword_name is not None:
        value = next(
            (keyword.value for keyword in node.keywords if keyword.arg == keyword_name),
            None,
        )
    elif callee == "revoke_grants_for_resource" and len(node.args) >= 2:
        value = node.args[1]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return "subagent" if value.value == "team_subagent" else "non_subagent"
    return "potential_subagent"


def _mentions_revision_store(node: ast.AST) -> bool:
    return any(
        (isinstance(child, ast.Name) and "revision_store" in child.id) or (isinstance(child, ast.Attribute) and "revision_store" in child.attr)
        for child in ast.walk(node)
    )


def _callee_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _literal_sql(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return " ".join(node.value.upper().split())
    return ""


def _symbol_for_line(
    scopes: list[tuple[int, int, str]],
    line: int,
) -> str:
    containing = [(end - start, symbol) for start, end, symbol in scopes if start <= line <= end]
    return min(containing)[1] if containing else "<module>"


def _normalize_sql_operation(value: str) -> str:
    normalized = " ".join(value.upper().split())
    if normalized == "INSERT INTO":
        return "insert"
    if normalized == "DELETE FROM":
        return "delete"
    return "update"


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[4]
