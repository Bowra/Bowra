import { apiFetch, readApiError } from "./api-client";
import { randomUuid } from "./random-id";
import { withoutSkillOperationTools } from "./agent-tool-selection";

export type AgentAssignmentStatus =
  | "not_hired"
  | "active"
  | "personal_disabled"
  | "access_revoked"
  | "removed"
  | "system_required";

export interface AgentCapability {
  id: string;
  label_key: string;
  label: string;
  category: string;
  verification_status?: "verified" | "derived_unverified";
}

export interface AgentCapabilityFacet extends AgentCapability {
  count: number;
}

export interface AgentTaxonomyFacet {
  id: string;
  count: number;
}

export interface AgentTeamTagFacet extends AgentTaxonomyFacet {
  label: string;
}

/** One Plugin in a change list or resource summary (Capability Center brief §4.11–4.12). */
export interface AgentPluginItem {
  kind: "builtin" | "team";
  /** `builtin:<name>` or a team Plugin id. */
  id: string;
  name: string;
  listing: Partial<Record<"zh-CN" | "en-US" | "de-DE" | "fr-FR", { name: string; description: string }>>;
  version: number | null;
  tools: string[];
  status?: string;
}

/** A Plugin updated since the release the Agent runs on. */
export interface AgentPluginUpdate {
  kind: "builtin" | "team";
  id: string;
  name: string;
  release_version: number;
  current_version: number;
  changed_at: string | null;
}

/** A Skill whose current approved revision differs from the one the release recorded. */
export interface AgentSkillUpdate {
  name: string;
  identity: string;
  release_revision: string;
  current_revision: string;
  changed_at: string | null;
}

export type AgentResourceUnavailableReason = "disabled_by_team" | "disabled_by_me";

/** Plugins and Tools a team Agent's live release uses (market details). */
export interface AgentConsumerResources {
  plugins: Array<AgentPluginItem & { unavailable_reason: AgentResourceUnavailableReason | null }>;
  tools: Array<{ ref: string; unavailable_reason: AgentResourceUnavailableReason | null }>;
}

export interface AgentCatalogItem {
  kind: "agent_catalog_item" | "agent_consumer_detail";
  schema_version: 1;
  agent_id: string;
  display_name: string;
  display_name_key?: string;
  summary: string;
  summary_key?: string;
  /** Lifecycle of the Agent itself; archived Agents still appear in "Created by me" (older servers omit it). */
  lifecycle_state?: "active" | "archived" | "quarantined";
  source: {
    kind: "team" | "system";
    owner_label: string | null;
    owner_user_id: string | null;
  };
  facets: {
    domains: string[];
    jobs: string[];
    outputs: string[];
    team_tags?: Array<{ id: string; label: string }>;
  };
  capabilities: AgentCapability[];
  /** Present on consumer detail for team Agents while the Capability Center switch is on. */
  resources?: AgentConsumerResources;
  risk: { level: string; reasons: string[] };
  safety: {
    status: string;
    checked_subject: string;
    reasons: string[];
  };
  listing_safety?: {
    effective_status: string;
    deterministic_status: string;
    semantic_status: string;
    listing_hash: string | null;
    reasons: string[];
  };
  assignment: {
    id?: string;
    status: AgentAssignmentStatus;
    source: string | null;
    row_version: number | null;
    updated_at?: string;
  };
  team_default: {
    enabled: boolean;
    policy_version: number | null;
    row_version: number | null;
  };
  availability: {
    discoverable: boolean;
    hireable: boolean;
    runnable: boolean;
    reasons: string[];
    /** The one reason shown to a member when not runnable (docs/design/有效权限.md §8); null when nothing refuses. */
    public_reason?: string | null;
    /** Runs, but a dependency is not available: the first one members may see named, `more` counts the rest. */
    limited?: { name: string | null; name_en: string | null; more: number } | null;
  };
  actions: {
    view_detail: boolean;
    hire: boolean;
    enable: boolean;
    disable: boolean;
    unhire: boolean;
    manage: boolean;
    manage_team_default: boolean;
    fork?: boolean;
    create: boolean;
  };
  action_reasons: Record<string, string | null>;
  relationship: string;
  updated_label: { kind: string; at: string | null };
  manager_url: string | null;
  fork_policy?: "not_allowed" | "recipients" | "team_members" | null;
  capability_explanations?: Array<{
    capability_id: string;
    description_key: string;
  }>;
  localization?: {
    requested_locale: string;
    effective_locale: string | null;
    source_locale: string | null;
    locale_fallback: boolean;
    generation_source: string | null;
  };
  quality_evidence?: AgentQualityEvidence;
}

export interface AgentQualityEvidenceSuite {
  suite_kind: string;
  name: string;
  state: string;
  stale_reason: string | null;
  sample_size: number;
  attempts: number;
  passed_cases: number;
  flaky_cases: number;
  evaluated_at: string | null;
}

export interface AgentQualityEvidence {
  kind: "agent_quality_evidence";
  schema_version: 1;
  status: string;
  suites: AgentQualityEvidenceSuite[];
}

export interface AgentCapabilityEvidence {
  id: string;
  definition_version: number | null;
  compatibility_major?: number;
  definition_status: string;
  definition: {
    lifecycle: "active" | "deprecated" | "retired" | "unknown";
    version: number | null;
  };
  category?: string;
  listing_revision?: number;
  name?: string;
  description?: string;
  task_examples?: string[];
  parameters_schema?: Record<string, unknown>;
  inputs?: Array<{ contract: string; required: boolean }>;
  outputs?: Array<{ contract: string; required: boolean }>;
  implementation_evidence: {
    status: "reviewed" | "shadow" | "missing";
    reviewed_at: string | null;
    evidence_level: "catalog_reviewed" | "derived_unverified";
  };
  readiness: {
    state: "ready" | "degraded" | "unavailable" | "unknown";
    checked_at: string | null;
    reason_codes: string[];
  };
  management?: {
    implementation_ref: string | null;
    implementation_lifecycle: "active" | "deprecated" | null;
    missing_dependencies: Array<{
      kind: "skill" | "tool";
      ref: string;
      repair_href?: string;
    }>;
    unresolved_dependencies: Array<{
      kind: "skill" | "connection";
      ref: string;
      reason_code: string;
    }>;
  };
  verification_status: "derived_unverified";
  availability: "degraded" | "unknown";
  evidence_reasons: string[];
}

export interface AgentCapabilityReport {
  kind: "agent_capability_report";
  schema_version: 2;
  compiler_version: string;
  locale: string;
  agent: {
    id: string;
    source_kind: "system" | "team";
    source_version: string;
    runtime_profile: "internal" | "marketplace";
  };
  verification_status: "derived_unverified";
  capabilities: AgentCapabilityEvidence[];
  legacy_direct_dependencies: {
    status: "derived_unverified";
    tool_count: number;
    skill_count: number;
  };
  drift: {
    status: "review_required" | "legacy_unverified";
    listing_vs_manifest: {
      status: "aligned" | "drift";
      listing_without_evidence: string[];
      dependency_without_listing: string[];
    };
    capability_vs_dependency: {
      status: "aligned" | "drift";
      reason_codes: string[];
    };
    prompt_duplication: { status: "clear" | "drift"; reason_codes: string[] };
    implicit_team_tool_overlay: { status: "clear" | "drift"; reason_codes: string[] };
    unverified_outputs: { status: "clear" | "drift"; formats: string[] };
  };
}

export interface AgentCatalogCollection {
  kind: "agent_catalog_collection";
  schema_version: 1;
  items: AgentCatalogItem[];
  collections: Array<{
    id: string;
    label_key: string;
    agent_ids: string[];
    items?: AgentCatalogItem[];
  }>;
  total_count?: number;
  facets?: {
    capabilities: AgentCapabilityFacet[];
    domains?: AgentTaxonomyFacet[];
    jobs?: AgentTaxonomyFacet[];
    team_tags?: AgentTeamTagFacet[];
  };
  actions: { create: boolean };
  next_cursor: string | null;
}

export interface AgentManagementCollection {
  kind: "agent_management_collection";
  schema_version: 1;
  view: AgentManagementView;
  items: AgentManagementItem[];
  total_count?: number;
  next_cursor?: string | null;
  views: Record<AgentManagementView, boolean>;
  actions: { create: boolean; manage_team_defaults: boolean };
}

export type AgentManagementView = "hired" | "created" | "shared" | "archived" | "team";

export type AgentRoleId = "data" | "compute" | "deck" | "knowledge" | "canvas_edit" | "image";

export interface AgentRolePreferenceCandidate {
  assignment_id: string;
  agent_id: string;
  revision_ref: string;
  release_id: string;
  behavior_fingerprint: string;
  display_name: string;
  description: string;
  is_primary: boolean;
}

export interface AgentRolePreferenceRole {
  role_id: AgentRoleId;
  label_key: string;
  description_key: string;
  row_version: number;
  saved_assignment_id: string | null;
  preference: AgentRolePreferenceCandidate | null;
  invalid_reason: string | null;
  availability: "ready" | "no_eligible_agent" | "behavior_unavailable";
  candidates: AgentRolePreferenceCandidate[];
  actions: { set: boolean; clear: boolean };
}

export interface AgentRolePreferences {
  kind: "agent_role_preferences";
  schema_version: 1;
  roles: AgentRolePreferenceRole[];
  candidates_truncated: boolean;
}

export interface AgentArchivedItem {
  kind: "agent_archived_management_item";
  schema_version: 1;
  agent_id: string;
  name: string;
  display_name: string;
  summary: string;
  status: "disabled";
  lifecycle_state: "archived";
  aggregate_row_version: number;
  live_release_id: string;
  release_sequence: number;
  has_pending_candidate: boolean;
  archived_at: string;
  updated_at: string;
  actions: { restore: boolean; view_history: boolean };
}

export type AgentManagementItem = AgentCatalogItem | AgentArchivedItem;

export interface AgentAudienceSnapshot {
  schema_version: 1;
  effective_mode: "private" | "custom" | "teamwide";
  user_ids: string[];
  group_ids: string[];
}

export interface AgentExactSkillGrant {
  distribution: "reference" | "embedded_agent_scope";
  permissions: Array<"read" | "invoke">;
  revision: string;
  skill_ref: {
    id: string;
    kind: "builtin" | "team" | "user";
    name: string;
  };
}

export interface AgentAuthoringTool {
  stable_ref: string;
  display_name: string;
  description: string;
  /** Built-in translations; raw names default to English and team names stay unchanged. */
  display_name_en?: string;
  description_en?: string;
  display_name_zh?: string;
  description_zh?: string;
  display_name_de?: string;
  description_de?: string;
  display_name_fr?: string;
  description_fr?: string;
  source_kind: "native" | "team_mcp";
  source_display_name: string | null;
  /** Stable team connection identity. Null for Bowhead-native Tools. */
  connection_id?: string | null;
  capability_classes: string[];
  risk: "read" | "compute" | "artifact_write" | "external_write" | "destructive" | "unknown";
  selectable: boolean;
  locked_reason: string | null;
  approval_required: boolean;
  confirmation_summary: string | null;
}

export interface AgentAuthoringSkill {
  name: string;
  display_name: string;
  display_name_zh?: string;
  /** Model-facing routing text; pickers show the localized `description_<lang>` for built-ins. */
  description: string;
  description_en?: string;
  description_zh?: string;
  description_de?: string;
  description_fr?: string;
  source_kind: "builtin" | "team" | "user";
  linked_tools: string[];
  required_skills?: string[];
  allowed_audiences: Array<"private" | "custom" | "teamwide">;
  /** Carries every permission the Skill grants: read, plus invoke for a runnable Skill (tool Skill with scripts). */
  grant: AgentExactSkillGrant;
  /** "none" without scripts; "disabled" when the Skill has scripts but this deployment does not run them. */
  script_runtime?: "none" | "enabled" | "disabled";
}

export interface AgentAuthoringPrimaryCapability {
  exact_ref: string;
  capability_id: string;
  version: number;
  category: string;
  display_name: string;
  description: string;
  display_name_en: string;
  description_en: string;
  display_name_zh?: string;
  description_zh?: string;
  display_name_de?: string;
  description_de?: string;
  display_name_fr?: string;
  description_fr?: string;
  selectable: boolean;
  eligible_role_ids: string[];
  verification_state: "blocked" | "shadow" | "reviewed";
  reason_codes: string[];
  output_contract_refs: string[];
}

export interface AgentAuthoringModelPolicy {
  allowed_roles: string[];
  effective_role: string;
  locked: boolean;
  policy_version: string;
}

export interface AgentAuthoringCatalog {
  kind: "agent_authoring_catalog";
  schema_version: 2;
  fingerprint: string;
  behavior_catalog_fingerprint: string;
  risk_registry_version: string;
  model_policy: AgentAuthoringModelPolicy;
  primary_capabilities: AgentAuthoringPrimaryCapability[];
  tools: AgentAuthoringTool[];
  skills: AgentAuthoringSkill[];
  /** Built-in Plugin selection follows Capability Center permissions. */
  plugins?: AgentAuthoringPluginSection;
}

export type AgentAuthoringPluginLockedReason =
  | "settings_assistant_only"
  | "disabled_by_team"
  | "disabled_by_me"
  | "not_ready";

interface AgentAuthoringPluginBase {
  id: string;
  name: string;
  version: number | null;
  tools: string[];
  ready: boolean;
  unavailable_tools: string[];
  selectable: boolean;
  locked_reason: AgentAuthoringPluginLockedReason | null;
  team_disabled: boolean;
  my_disabled: boolean;
}

export interface AgentAuthoringBuiltinPlugin extends AgentAuthoringPluginBase {
  family: "general" | "settings";
  /** Reviewed picker copy; `use_when` and `example` are set for every general Plugin. */
  listing: Partial<Record<string, { name: string; description: string; use_when?: string | null; example?: string | null }>>;
  prompt_modules: string[];
}

export interface AgentAuthoringTeamPlugin extends AgentAuthoringPluginBase {
  description: string;
  skill_count: number;
}

export interface AgentAuthoringPluginSection {
  builtin: AgentAuthoringBuiltinPlugin[];
  team: AgentAuthoringTeamPlugin[];
  /** Team administrators also see Plugins the team disabled or has not set up, with the reason. */
  viewer_can_manage?: boolean;
}

export interface AgentPlatformTaxonomyTerm {
  id: string;
  label: string;
}

export interface AgentTeamTag {
  id: string;
  display_name: string;
  status: "active" | "inactive" | "merged";
  merged_into_id: string | null;
  row_version: number;
  created_at: string;
  updated_at: string;
}

export interface AgentTaxonomySuggestion {
  id: string;
  source_hash: string;
  taxonomy_version: string;
  suggestion: {
    domains: string[];
    jobs: string[];
    team_tag_ids: string[];
    new_team_tags: string[];
    confidence: number;
    reasons: string[];
  };
  confidence: number;
  state: "pending" | "accepted" | "dismissed" | "stale";
  row_version: number;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentTaxonomyManager {
  kind: "agent_taxonomy_manager";
  schema_version: 1;
  agent_id: string;
  working_copy_id: string;
  working_copy_row_version: number;
  source_hash: string;
  source: { display_name: string; summary: string };
  platform: {
    version: string;
    domains: AgentPlatformTaxonomyTerm[];
    jobs: AgentPlatformTaxonomyTerm[];
  };
  selection: {
    version: string;
    domains: string[];
    jobs: string[];
    team_tag_ids: string[];
  };
  team_tags: AgentTeamTag[];
  suggestion: AgentTaxonomySuggestion | null;
}

export interface AgentTeamTagManager {
  kind: "agent_team_tag_manager";
  schema_version: 1;
  taxonomy_version: string;
  items: AgentTeamTag[];
  limits: { team: number; per_agent: number };
}

export interface AgentReleaseSnapshot {
  release_id: string;
  release_sequence: number;
  revision_id: string;
  revision_kind: string;
  definition: Record<string, unknown>;
  listing: Record<string, unknown>;
  audience: AgentAudienceSnapshot;
  activated_at: string;
  /** Plugins updated since this release (servers before the Capability Center omit it). */
  plugin_updates?: AgentPluginUpdate[];
  /** Skills updated since this release; runs already follow the latest approved Skill. */
  skill_updates?: AgentSkillUpdate[];
}

export interface AgentWorkingCopyValue {
  id: string;
  base_release_id: string;
  definition: Record<string, unknown>;
  listing: Record<string, unknown>;
  audience: AgentAudienceSnapshot;
  validation: Record<string, unknown>;
  change_kind: string;
  rollback_source_release_id: string | null;
  row_version: number;
  updated_at: string;
}

export interface AgentWorkingCopyDetail {
  kind: "agent_working_copy_detail";
  schema_version: 1;
  agent_id: string;
  aggregate_row_version: number;
  aggregate: {
    name: string;
    status: "enabled" | "disabled";
    lifecycle_state: "active" | "archived" | "quarantined";
  };
  live: AgentReleaseSnapshot;
  working_copy: AgentWorkingCopyValue | null;
  pending_candidate: {
    candidate_id: string;
    candidate_hash: string;
    candidate_row_version: number;
    submitted_at: string;
  } | null;
  actions: {
    edit: boolean;
    publish: boolean;
    enable: boolean;
    disable: boolean;
    archive: boolean;
    restore: boolean;
  };
}

export interface AgentWorkingCopyMutation {
  kind: "agent_working_copy";
  schema_version: 1;
  id: string;
  agent_id: string;
  base_release_id: string;
  definition: Record<string, unknown>;
  listing: Record<string, unknown>;
  audience: AgentAudienceSnapshot;
  row_version: number;
  updated_at: string;
}

export interface AgentSafetyCheckReport {
  kind: "agent_safety_check_report";
  schema_version: 1;
  id: string;
  agent_id: string;
  subject: {
    kind: string;
    ref: string;
    content_hash: string;
    working_copy_row_version: number | null;
  };
  status: "pass" | "warning" | "review_required" | "blocked" | "unknown";
  deterministic: { status: string; reason_codes: string[] };
  semantic: { status: string; reason_codes: string[] };
  summary: { status: string; reason_codes: string[] };
  created_at: string;
  actions: { admin_override: false; publish: boolean };
  /** Server's own answer to "will this publish go to review". Absent = not evaluated. */
  requires_admin_review?: boolean;
}

export interface AgentPublicationResult {
  kind: "agent_publication_result";
  schema_version: 1;
  state: "pending" | "activated" | "rejected" | "withdrawn" | "stale";
  agent_id: string;
  candidate_id: string | null;
  revision_id: string;
  release_id: string | null;
  candidate_row_version: number | null;
  aggregate_row_version: number;
}

export interface AgentVersionPreparation {
  kind: "agent_version_preparation";
  schema_version: 1;
  state: "prepared";
  agent_id: string;
  aggregate_row_version: number;
  candidate_id: string;
  candidate_hash: string;
  candidate_row_version: number;
  revision_id: string;
  compatibility_major: number;
  compatibility_verdict: string;
  control_row_version: number;
  plan_id: string;
  plan_hash: string;
  policy_revision_id: string;
  policy_hash: string;
}

export interface AgentVersionWorkbench {
  schema_version: 1;
  current_eval_policy_revision_id: string | null;
  live_identity: { release_id: string; release_sequence: number; revision_id: string };
  candidate_identity: null | {
    audience_mode: "private" | "custom" | "teamwide";
    /** Live release the candidate was prepared on; differs from `live_identity.release_id` once live moved. Absent on older servers. */
    base_release_id?: string | null;
    candidate_hash: string;
    candidate_id: string;
    /** Release sequence activation would assign right now (server-computed). Absent on older servers. */
    release_sequence_if_activated?: number;
    revision_id: string;
    row_version: number;
    state: "prepared" | "pending" | "approved";
  };
  next_draft_identity: null | { base_release_id: string; id: string; row_version: number };
  /**
   * The plan the next comparison runs. `policy_revision_id` is the evaluation policy it was compiled from and
   * `execution_workspace_id` the Space it runs in; both absent on older servers.
   */
  active_plan_identity: null | { id: string; hash: string; policy_revision_id?: string; execution_workspace_id?: string };
  /**
   * The prepared candidate's plan was compiled before the latest saved test cases (the evaluation policy moved on).
   * Releasing is refused until a comparison runs; the Owner's run recompiles the plan first (`refresh_plan`).
   */
  evaluation_plan_behind?: boolean;
  /**
   * The draft was saved again after the candidate was prepared. While true the server offers
   * `prepare_from_latest_draft`, which replaces the candidate in one step. Absent on older servers.
   */
  draft_ahead_of_candidate?: boolean;
  /** `execution_id` is the comparison that produced the snapshot; absent on servers before tech-design §4.4. */
  active_snapshot_identity: null | { id: string; hash: string; created_at: string; execution_id?: string | null };
  active_override_identity: null | { id: string; hash: string };
  /** Content-free batch overview of the active snapshot (eval credibility R15); absent on older servers. */
  comparison_overview?: AgentVersionComparisonOverview | null;
  active_execution_summary: null | {
    can_cancel: boolean;
    execution_id: string;
    failure_code: string | null;
    finished_at: string | null;
    row_version: number;
    started_at: string | null;
    status: string;
    /** Present only when run insights are enabled; absent means fall back to status plus elapsed time. */
    progress?: AgentEvalProgress;
    /** Who started it: the Owner (`user`), Coach after an edit (`internal_coach`) or an external pipeline. Absent on older servers. */
    origin_kind?: "user" | "internal_coach" | "automation";
  };
  control_row_version: number | null;
  display_state: string;
  draft_rebase_state: "current" | "required";
  readiness: "missing" | "ready" | "ready_with_risks" | "blocked" | "unknown" | "stale" | null;
  reason_codes: string[];
  stale_subject: string | null;
  primary_action: { kind: string; enabled: boolean; disabled_reason_codes: string[] };
  secondary_actions: Array<{ kind: string; enabled: boolean }>;
  change_proposals?: AgentChangeProposal[];
  /** Draft fields that differ from the live release or that Coach changed. Absent on older servers. */
  draft_changes?: AgentDraftChange[];
  /** Members' thumbs-downs on Turns they shared with the Owner, not yet handled (Pending). Absent on older servers. */
  member_feedback?: AgentMemberFeedback[];
  /**
   * Private Agents only: "Release as vN" is offered whatever the test says (the test is information, not a gate).
   * Null for shared Agents and when there is nothing to release. Absent on older servers.
   */
  private_release?: AgentPrivateRelease | null;
}

/** What the Owner is told about the test when releasing a private Agent; recorded on the release. */
export type AgentReleaseTestState =
  | "passed"
  | "passed_with_risks"
  | "regressed"
  | "safety_failed"
  | "failed"
  | "no_conclusion"
  | "could_not_run"
  | "outdated"
  | "not_tested";

export interface AgentPrivateRelease {
  /** False while a test of this change runs (`agent_release_test_running`) or the draft needs rebasing. */
  available: boolean;
  blocked_reason_code: string | null;
  candidate_covers_draft: boolean;
  release_sequence: number;
  test_state: AgentReleaseTestState;
  /** A real regression or a safety-contract failure: the dialog warns and the button reads "Release as vN anyway". */
  warning: boolean;
  reason_codes: string[];
}

/** A thumbs-down on an answer: the only score is -1. */
export interface AnswerFeedback {
  score: -1;
  reason_codes: string[];
  comment: string | null;
  given_at: string;
}

/** A member's shared thumbs-down waiting on the Agent Owner's Pending list. */
export interface AgentMemberFeedback {
  turn_id: string;
  shared_by: string;
  /** A short preview of the question; the answer isn't listed. */
  question: string;
  answered_at: string;
  feedback: AnswerFeedback;
}

/** Who last set a draft field: a source of an accepted proposal ("ci" for Git), otherwise "manual". */
export type AgentDraftChangeSource = "coach" | "ci" | "feedback" | "manual" | "regression" | "dependency" | "upstream";

export interface AgentDraftChange {
  /** Field pointer, the first two JSON Pointer segments, such as `/definition/system_prompt`. */
  path: string;
  live: unknown;
  draft: unknown;
  source: AgentDraftChangeSource;
  /** Present when Coach wrote this field in the current draft. */
  coach?: {
    state: "applied" | "undone";
    proposal_id: string;
    group_ids: string[];
    before: unknown;
    value: unknown;
    /** The field no longer holds what undo (or reapply) expects: someone changed it after Coach. */
    conflict: boolean;
  };
}

export interface AgentCoachChangeConflict {
  path: string;
  before: unknown;
  coach: unknown;
  current: unknown;
}

export interface AgentCoachChangeResult {
  kind: "agent_coach_change_result";
  schema_version: 1;
  action: "undo" | "reapply" | "keep" | "restore";
  fields: string[];
  decision_ids: string[];
  working_copy: AgentWorkingCopyValue;
}

export type AgentSuggestedCaseSpec = Omit<AgentEvalCase, "task" | "input">;

/** Where a CI (`git`) proposal came from. `key_name` is null once the Team key is deleted. */
export interface AgentChangeProposalOrigin {
  repository: string;
  ref: string;
  commit_sha: string;
  grant_id: string;
  key_name: string | null;
}

export interface AgentChangeProposal {
  kind: "agent_change_proposal";
  schema_version: 1;
  proposal_id: string;
  agent_id: string;
  source_type: "coach" | "feedback" | "regression" | "dependency" | "upstream" | "git";
  source_ref: string;
  rationale: string;
  status: "open" | "accepted" | "dismissed" | "stale" | "superseded";
  row_version: number;
  created_at: string;
  decided_at: string | null;
  operations: Array<{ op: "add" | "replace" | "remove"; path: string; value?: unknown }>;
  atomic_groups: Array<{ id: string; operation_indexes: number[]; depends_on: string[] }>;
  suggested_cases: Array<{
    id: string;
    task: string;
    covers_group_ids: string[];
    failure_mode: string;
    decision_status: "pending" | "accepted" | "dismissed";
    result_suite_revision_id?: string;
    case_spec?: AgentSuggestedCaseSpec;
    edit?: {
      case_revision_id: string;
      case_hash: string;
      case_key: string;
      before: { task: string; case_spec: AgentSuggestedCaseSpec };
      changed_fields: Array<"task" | "expected_contract" | "scorer_config" | "valid_until">;
    };
  }>;
  case_target_suite_revision_id?: string | null;
  base: { release_id: string | null; working_copy_id: string | null; working_copy_row_version: number | null; suite_revision_id?: string | null; initial_suite_name?: string | null };
  /** Set for `source_type === "git"`, null for every other source (always sent; optional for older fixtures). */
  origin?: AgentChangeProposalOrigin | null;
}

export interface AgentVersionComparison {
  kind: "agent_version_comparison";
  schema_version: 1;
  actor_kind: "owner" | "automation";
  can_cancel: boolean;
  candidate_id: string;
  execution_id: string;
  failure_code: string | null;
  finished_at: string | null;
  generation: number;
  plan_id: string;
  run_kind: "release" | "diagnostic";
  row_version: number;
  runs: Array<{ run_id: string; subject_side: "baseline" | "candidate"; suite_revision_id: string }>;
  status: string;
  progress?: AgentEvalProgress;
}

/** Usage only: tokens, model requests and duration. The contract carries no cost, credit or currency. */
export interface AgentEvalUsageRange {
  low: number;
  high: number;
}

export interface AgentEvalUsageEstimate {
  kind: "agent_eval_usage_estimate";
  schema_version: 1;
  status: "available" | "unavailable";
  unavailable_reason: "insufficient_history" | "plan_unavailable" | "plan_identity_changed" | null;
  cases_planned: number | null;
  attempts_planned: number | null;
  tokens: AgentEvalUsageRange | null;
  tokens_cached: AgentEvalUsageRange | null;
  model_calls: AgentEvalUsageRange | null;
  duration_ms: AgentEvalUsageRange | null;
  basis: { window_days: number; scope: "same_cases" | "same_suites" | "agent"; sample_attempts: number } | null;
  computed_at: string;
}

export interface AgentEvalProgress {
  schema_version: 1;
  cases: { completed: number; total: number };
  attempts: { completed: number; total: number; in_flight: number };
  model_calls: {
    used: number;
    basis: "successful_calls" | "dispatch_admissions";
    limit: number | null;
    delegation_used?: number | null;
    estimated_total: number | null;
  };
  usage: {
    tokens: number | null;
    tokens_input: number | null;
    tokens_output: number | null;
    tokens_cached: number | null;
    estimated_total_tokens: number | null;
    settled_only: boolean;
    partial: boolean;
  };
  elapsed_ms: number | null;
  eta_ms: AgentEvalUsageRange | null;
  as_of: string;
}

export interface AgentVersionCasePassCount {
  passed: number;
  runs: number;
  /** Attempts that failed; the rest of `runs` ended without a verdict. Absent on older servers. */
  failed?: number;
}

export interface AgentVersionCaseChange {
  suite_revision_id: string;
  suite_name: string | null;
  case_revision_id: string;
  case_key: string | null;
  case_label: string | null;
  change: "required_failure" | "worse" | "unstable" | "better";
  classification: string;
  reason_codes: string[];
  key_case: boolean;
  baseline: AgentVersionCasePassCount | null;
  candidate: AgentVersionCasePassCount;
}

export interface AgentVersionCaseChanges {
  kind: "agent_version_case_changes";
  schema_version: 1;
  execution_id: string;
  snapshot: { id: string; hash: string; created_at: string };
  baseline: { release_id: string; release_sequence: number | null } | null;
  candidate: { candidate_id: string; release_sequence: number | null };
  summary: { case_count: number; worse: number; unstable: number; better: number; not_comparable: number; unchanged_required_failures: number };
  items: AgentVersionCaseChange[];
  next_offset: number | null;
  /**
   * Cases left without a result by a time limit or a required Tool the evaluation can't run, named so the
   * card can say which case to split or which point to change. Absent on older servers.
   */
  unfinished?: AgentVersionUnfinishedCase[];
  /** Set when data cases read the Team's Datarc live: they used the Owner's Datarc access. */
  datarc_owner_access?: { owner_name: string | null } | null;
  /**
   * Unchanged cases whose attempts disagreed on a side but were decided by their majority (for example 2 of 3
   * passed), with both sides' attempts. Shown as information, never as unstable. Absent on older servers.
   */
  split?: AgentVersionSplitCase[];
}

export interface AgentVersionSplitCase {
  suite_revision_id: string;
  suite_name: string | null;
  case_revision_id: string;
  case_key: string | null;
  case_label: string | null;
  classification: string;
  baseline: AgentVersionCasePassCount | null;
  candidate: AgentVersionCasePassCount;
}

export interface AgentVersionUnfinishedCase {
  suite_revision_id: string;
  suite_name: string | null;
  case_revision_id: string;
  case_key: string | null;
  case_label: string | null;
  reason_codes: string[];
  /** Required Tools the evaluation couldn't run for this case. */
  tools: string[];
  /** Why the case's attempts ended without a verdict, one row per cause (servers since 2026-10-21). */
  failures?: AgentVersionAttemptFailure[];
  /** Each side's attempts (null when the released version didn't run the case). Absent on older servers. */
  baseline?: AgentVersionCasePassCount | null;
  candidate?: AgentVersionCasePassCount;
}

/** One cause of a case's attempts ending without a verdict; never raw provider text. */
export interface AgentVersionAttemptFailure {
  category: "model_unavailable" | "judge" | "agent_model" | "tool" | "cancelled" | "platform" | "data_connection";
  /** judge: unavailable | not_configured; agent_model: rate_limited, auth_failed...; cancelled: stopped | authority_lost | restart. */
  cause: string | null;
  tool: string | null;
  /** model_unavailable: the model role that had no usable model ("analyst" is the main model, "judge" the evaluation model). */
  role?: string | null;
  /** The release-check reason this cause reports. */
  reason_code: string;
  /** Diagnostic references of the attempts (their job ids); the server logs the error under the same one. */
  references: string[];
  /** data_connection owner_not_member: the Datarc workspace the Owner isn't a member of. */
  workspace?: string | null;
  /** data_connection starter_not_owner: the Agent's Owner, who must start the test. */
  owner_name?: string | null;
}

/** One point's verdict on one attempt; `met: null` is not graded (see `reason`). */
export interface AgentEvalGradedPoint {
  id: string;
  met: boolean | null;
  quote: string | null;
  applicable: boolean;
  blocking: boolean;
  reason: string | null;
}

export interface AgentVersionCaseAnswerAttempt {
  attempt: number;
  status: string;
  failure_code: string | null;
  duration_ms: number | null;
  answer: string | null;
  answer_truncated: boolean;
  /** Absent on servers before evaluation slice 5. */
  run_id?: string;
  points?: AgentEvalGradedPoint[];
  /** Figures in the answer with no direct source; an advisory hint that never decided the attempt. */
  unsourced_figures?: string[];
  /** Set when a time limit stopped the attempt: the limit it hit and how long it ran. Absent on older servers. */
  time_limit?: { limit_ms: number; observed_ms: number; scope: "attempt" | "run" } | null;
  /** Rule checks the attempt failed besides its required actions (which are listed in `points`). Absent on older servers. */
  failed_checks?: string[];
  /** Why an attempt without a verdict stopped, with its diagnostic reference. Absent on older servers and for verdicts. */
  failure?: {
    category: string;
    cause: string | null;
    tool: string | null;
    role?: string | null;
    reference: string;
    reason_code: string;
    workspace?: string | null;
    owner_name?: string | null;
  } | null;
  /**
   * Live Datarc reads (FEATURE_AGENT_EVAL_DATARC_READ_ONLY): the attempt's Datarc calls, row queries and rows read
   * (and shown to the Agent). Owner-only; never rows. Absent without the switch or Datarc calls.
   */
  datarc?: { calls: number; queries: number; rows: number; rows_shown?: number } | null;
  /** Tools the evaluation couldn't run, when that is why the attempt has no verdict. Absent on older servers. */
  tools_not_run?: string[];
  /**
   * Set when the stored answer is an internal instruction (the Agent stopped after repeated Tool failures),
   * which is withheld; the view says what happened in plain words instead. Absent on older servers.
   */
  answer_withheld?: "stopped_without_answer" | null;
}

export interface AgentEvalCasePointDefinition {
  id: string;
  kind: "data_fact" | "required_action" | "judgment";
  text: string;
  state: "suggested" | "confirmed";
  /** Set when the point failed calibration under its current text; Coach's rewording when it has one. */
  calibration_failure: null | { reason: string; rewording: string | null };
  /** A required action's Tools (any one meets it). */
  tools?: string[];
}

/** Whether a confirmed point counts in release checks right now (PRD R25). */
export interface AgentEvalPointStatus {
  counts: boolean;
  reason: null | "calibrating" | "kind_not_validated" | "judge_changed" | "judge_unavailable" | "calibration_failed";
  rewording: string | null;
}

export interface AgentVersionUnmetPoint {
  case_revision_id: string;
  case_key: string;
  case_label: string | null;
  point_id: string;
  kind: string;
  text: string;
  run_id: string;
  attempt: number;
  quote: string | null;
  attempts: number;
}

export interface AgentEvalPointFeedback {
  kind: "agent_eval_point_feedback";
  schema_version: 1;
  example_id: string;
  point_id: string;
  disputed_met: boolean;
  rewording: string | null;
}

export interface AgentVersionCaseAnswers {
  kind: "agent_version_case_answers";
  schema_version: 1;
  execution_id: string;
  suite_revision_id: string;
  case_revision_id: string;
  case_key: string;
  case_label: string | null;
  task: string | null;
  /** The case's expected points; absent on servers before evaluation slice 5. */
  points?: AgentEvalCasePointDefinition[];
  baseline: { attempts: AgentVersionCaseAnswerAttempt[] } | null;
  candidate: { attempts: AgentVersionCaseAnswerAttempt[] } | null;
}

export interface AgentVersionOverride {
  kind: "agent_eval_override";
  schema_version: 1;
  candidate_id: string;
  control_row_version: number;
  override_id: string;
  override_hash: string;
  reviewer_preview: Record<string, unknown>;
}

export interface AgentVersionReviewerProjection {
  schema_version: 2;
  actor_overlap: boolean;
  candidate_hash: string;
  candidate_id: string;
  candidate_label: string;
  decision_availability: {
    approve: boolean;
    reject: boolean;
    disabled_reason_codes: string[];
  };
  override: null | {
    id: string;
    hash: string;
    reason_overrides: Array<{ reason_code: string; reason_note: string }>;
    team_evidence: Record<string, unknown>;
  };
  plan_hash: string;
  plan_id: string;
  readiness: string;
  reviewer_evidence: Record<string, unknown>;
  snapshot_created_at: string;
  snapshot_hash: string;
  snapshot_id: string;
  submitted_at: string;
}

export interface AgentAudienceSummary {
  mode: "private" | "custom" | "teamwide" | "unknown";
  user_count: number;
  group_count: number;
}

export interface AgentReleaseInboxItem {
  kind: "agent_release_inbox_item";
  schema_version: 1;
  candidate_id: string;
  candidate_hash: string;
  candidate_row_version: number;
  agent_id: string;
  display_name: string;
  owner_user_id: string;
  change_kind: string;
  audience: AgentAudienceSummary;
  risk: { level?: string; reasons?: string[] };
  safety: { status?: string; reason_codes?: string[] };
  submitted_at: string;
  detail_url: string;
}

export interface AgentReleaseCandidateDetail {
  kind: "agent_release_candidate_detail";
  schema_version: 1;
  candidate_id: string;
  candidate_hash: string;
  candidate_row_version: number;
  state: AgentPublicationResult["state"];
  agent: { id: string; display_name: string; owner_user_id: string };
  base: { release_id: string; release_sequence: number; revision_id: string } | null;
  candidate: {
    revision_id: string;
    change_kind: string;
    listing: Record<string, unknown>;
    behavior: {
      model: string | null;
      max_iterations: number;
      has_output_contract: boolean;
      tool_count: number;
      skill_count: number;
      requested_primary_capability_ref: string | null;
      primary_capability_ref: string | null;
      primary_capability_name: string | null;
      primary_capability_name_en: string | null;
      verification_state: "blocked" | "shadow" | "reviewed";
      eligible_role_ids: string[];
      output_contract_refs: string[];
    };
    audience: AgentAudienceSummary;
    risk: { level?: string; reasons?: string[]; runtime_confirmation_required?: boolean };
    safety: {
      status: string;
      deterministic: { status: string; reason_codes: string[] };
      semantic: { status: string; reason_codes: string[] };
      summary: { status: string; reason_codes: string[] };
    };
  };
  diff: {
    listing_fields: string[];
    instructions_changed: boolean;
    model_changed: boolean;
    output_contract_changed: boolean;
    tool_set_changed: boolean;
    skill_set_changed: boolean;
    runtime_extensions_changed?: boolean;
    knowledge_access_changed?: boolean;
    knowledge_access_expanded?: boolean;
    primary_capability_changed: boolean;
    capability_protocol_block_changed?: boolean;
    behavior_changed: boolean;
    dependency_changed: boolean;
    capability_changed: boolean;
    risk_changed: boolean;
    plugin_set_changed?: boolean;
    plugin_changes?: { added: string[]; removed: string[] };
    plugin_items?: Array<AgentPluginItem & { change: "added" | "removed" }>;
    tool_changes?: { added: string[]; removed: string[] };
    skill_changes?: { added: string[]; removed: string[] };
    audience: {
      before: AgentAudienceSummary | null;
      after: AgentAudienceSummary;
      changed: boolean;
      expanded: boolean;
    };
  };
  release_note: string | null;
  submitted_by: string;
  submitted_at: string;
  plugin_updates?: AgentPluginUpdate[];
  skill_updates?: AgentSkillUpdate[];
  /** True once calls that need confirmation are confirmed at run time (tech design §9). */
  decision: Record<string, unknown> | null;
  actions: { approve: boolean; reject: boolean; withdraw: boolean };
  evaluation?: AgentVersionReviewerProjection | {
    release_gate: AgentEvalReleaseGate;
    suites: AgentEvalSuite[];
  };
}

export interface AgentEvalRunSummary {
  case_count?: number;
  attempts?: number;
  passed_attempts?: number;
  failed_attempts?: number;
  "pass@k_cases"?: number;
  flaky_cases?: number;
  critical_failure?: boolean;
  duration_ms?: number;
  cost_microusd?: number;
}

export interface AgentEvalQuality {
  state: "not_configured" | "ungraded" | "partial" | "graded";
  met: number;
  unmet: number;
  ungraded: number;
  point_assessments: number;
}

export interface AgentEvalRunCaseAnswers {
  run_id: string;
  revision_id: string;
  suite_revision_id: string;
  case_revision_id: string;
  case_key: string;
  task: string | null;
  points: AgentEvalCasePointDefinition[];
  attempts: Array<AgentVersionCaseAnswerAttempt & { quality: AgentEvalQuality }>;
}

export interface AgentEvalRun {
  kind: "agent_eval_run";
  schema_version: 1;
  run_id: string;
  suite_id?: string;
  suite_revision_id: string;
  target_kind: "candidate" | "live";
  revision_id: string;
  status: "queued" | "running" | "passed" | "failed" | "unknown" | "incomplete" | "cancelled";
  trigger_kind: string;
  summary: AgentEvalRunSummary | null;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  quality?: AgentEvalQuality;
  results?: Array<{
    task?: string;
    quality?: AgentEvalQuality;
    case_revision_id: string;
    case_key: string;
    attempt: number;
    status: string;
    deterministic: Record<string, unknown>;
    judge: Record<string, unknown> | null;
    duration_ms: number | null;
    cost_microusd: number | null;
    failure_code: string | null;
  }>;
}

export type AgentEvalPointKind = "data_fact" | "required_action" | "judgment";
export type AgentEvalPointSource = "owner" | "coach_task" | "coach_live_answer" | "coach_conversation" | "coach_alignment";

/** A plain-language expected point. Only confirmed points can count in release checks. */
export interface AgentEvalPoint {
  id: string;
  kind: AgentEvalPointKind;
  text: string;
  source: AgentEvalPointSource;
  state: "suggested" | "confirmed";
  /** One Tool, or alternatives of which any one meets the point. */
  action?: { tool: string } | { any_of: string[] };
  /** Where a point from the Coach alignment loop came from. Only the Owner's own words are ever shown. */
  origin?: { kind?: "owner_words" | "member_feedback"; words?: string | null; by?: string | null; at?: string | null; loop_id?: string | null } | null;
  /** The text hash at which the Owner chose "Keep as is" for Coach's point review. */
  review_dismissed_hash?: string | null;
}

export interface AgentEvalLiveAnswer {
  run_id: string;
  case_revision_id: string;
  attempt: number;
  answer_ref: string;
  answered_at: string;
  answer: string | null;
  answer_truncated: boolean;
  /** The answer comes from a held-out case (Coach sees only whether it passed); absent on older servers. */
  held_out?: boolean;
}

export interface AgentEvalPointPreview {
  kind: "agent_eval_point_preview";
  schema_version: 1;
  answer: AgentEvalLiveAnswer | null;
  points: Array<{ id: string; met: boolean | null; quote: string | null; value: string | null; reason: string | null }>;
}

export interface AgentEvalCase {
  case_key: string;
  /** The saved case revision; present on cases read from the server, never sent back. */
  case_revision_id?: string;
  task?: string;
  input?: { task?: string };
  fixture_ref?: Record<string, unknown>;
  expected_contract: Record<string, unknown>;
  scorer_config: Record<string, unknown>;
  effect_policy: Record<string, unknown>;
  valid_until?: string | null;
}

export interface AgentVersionComparisonOverview {
  cases: number;
  /** 0–1; null when no case reached a verdict on that side. */
  candidate_pass_rate: number | null;
  live_pass_rate: number | null;
  newly_failing: number;
  newly_passing: number;
}

export interface AgentEvalConversationCandidate {
  turn_id: string;
  /** A short preview of the question; the answer is never listed. */
  question: string;
  answered_at: string;
  /** Who shared this Turn with the Agent's Owner; null for the Owner's own conversation. */
  shared_by: string | null;
  /** The author's thumbs-down, if any (own Turns and shared Turns only). */
  feedback?: AnswerFeedback | null;
}

/** A Turn of the requester's that a team Agent answered: its share and thumbs-down state. */
export interface TurnEvalShare {
  turn_id: string;
  agent_id: string;
  agent_name: string;
  shared: boolean;
  /** False when the requester owns the Agent: no share needed. Absent on older servers (they listed shareable Turns only). */
  shareable?: boolean;
  /** The Agent owner's display name or email, shown with the share consent. */
  owner_name?: string | null;
  feedback?: AnswerFeedback | null;
}

export interface AgentEvalSuite {
  suite_id: string;
  suite_kind: "platform_contract" | "owner_quality" | "category_benchmark";
  name: string;
  visibility: "owner" | "reviewer" | "team_evidence";
  state: string;
  row_version: number;
  platform_managed: boolean;
  revision: {
    suite_revision_id: string;
    sequence: number;
    case_count: number;
    created_at: string;
    cases: AgentEvalCase[];
    /** Owner cases saved under the defaults before 2026-10-15 (R18); absent on older servers. */
    cases_on_old_defaults?: string[];
  } | null;
  evidence: {
    state: string;
    stale_reason: string | null;
    updated_at: string | null;
    run: AgentEvalRun | null;
  };
  /** Confirmed points of the latest revision by point id; absent on servers before evaluation slice 5. */
  point_status?: Record<string, AgentEvalPointStatus>;
}

export interface AgentEvalReleaseGate {
  required: boolean;
  ready: boolean;
  reason: string | null;
}

export interface AgentEvalManager {
  kind: "agent_eval_manager";
  schema_version: 1;
  agent_id: string;
  target: { kind: "candidate"; candidate_id: string; revision_id: string } | { kind: "live"; revision_id?: string | null };
  suites: AgentEvalSuite[];
  runs: AgentEvalRun[];
  trigger_policy: {
    enabled: boolean;
    on_definition_change: boolean;
    on_dependency_change: boolean;
    on_model_change: boolean;
    row_version: number;
  } | null;
  release_gate: AgentEvalReleaseGate;
}

export interface AgentLocalizedListing {
  id: string;
  team_id: string;
  resource_kind: "agent" | "skill" | "capability";
  resource_id: string;
  resource_revision: string;
  locale: string;
  source_locale: string;
  fields: {
    display_name: string;
    summary: string;
  };
  source_hash: string;
  generation_source: "manual" | "llm" | "platform";
  review_state: "draft" | "confirmed" | "stale";
  row_version: number;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface AgentLocalizationManager {
  kind: "catalog_localization_manager";
  schema_version: 1;
  resource_kind: "agent";
  resource_id: string;
  resource_revision: string;
  working_copy_row_version: number;
  source_locale: string;
  source: {
    display_name: string;
    summary: string;
  };
  source_hash: string;
  items: AgentLocalizedListing[];
}

export interface AgentReleaseHistoryItem {
  kind: "agent_release_history_item";
  schema_version: 1;
  release_id: string;
  release_sequence: number;
  revision_id: string;
  revision_sequence: number;
  change_kind: string;
  display_name: string;
  audience: AgentAudienceSummary;
  risk: { level?: string; reasons?: string[] };
  safety: { status: string; summary: { reason_codes?: string[] } };
  release_note: string | null;
  /** Private releases: what the test said when the Owner released. Absent on older servers and earlier releases. */
  release_test?: { state: AgentReleaseTestState | string; reason_codes: string[] } | null;
  activated_by: string;
  activated_at: string;
  review: Record<string, unknown> | null;
}

export interface AgentAssignmentMutation {
  kind: "agent_assignment_mutation";
  schema_version: 1;
  assignment: AgentCatalogItem["assignment"];
  runnable: boolean;
  public_reason: string | null;
  paused_schedule_ids?: string[];
  expired_schedule_ids?: string[];
}

export interface AgentUnhireImpact {
  kind: "agent_unhire_impact";
  schema_version: 1;
  assignment_id: string;
  assignment_row_version: number;
  impact_token: string;
  expires_in_seconds: number;
  active_count: number;
  paused_count: number;
  running_count: number;
  jobs: Array<{
    id: string;
    name: string;
    status: string;
    has_in_flight_run: boolean;
  }>;
}

export interface AgentTeamDefaultPreview {
  kind: "agent_team_default_preview";
  schema_version: 1;
  agent_id: string;
  action: "set" | "remove";
  active_member_count: number;
  assignment_count: number;
  opted_out_count: number;
  affected_count: number;
  policy: {
    enabled: boolean;
    policy_version: number;
    row_version: number;
  } | null;
}

export interface AgentForkDependencyGap {
  kind: "tool" | "skill";
  source_ref_digest: string;
  reason_codes: string[];
}

export interface AgentForkPreview {
  kind: "agent_fork_preview";
  schema_version: 1;
  source: {
    agent_id: string;
    revision_ref: string;
    release_id: string;
    display_name: string;
  };
  suggested_display_name: string;
  copied: {
    prompt: boolean;
    listing: boolean;
    model: boolean;
    output_contract: boolean;
    tool_count: number;
    skill_count: number;
  };
  dependency_gaps: AgentForkDependencyGap[];
  excluded: string[];
  will_start_disabled: boolean;
}

export interface AgentForkResult {
  kind: "agent_fork_result";
  schema_version: 1;
  agent_id: string;
  revision_id: string;
  release_id: string;
  lineage_id: string;
  dependency_gaps: AgentForkDependencyGap[];
  status: "enabled" | "disabled";
  audience: "private";
  replayed: boolean;
  manager_url: string;
}

export interface AgentForkSettings {
  kind: "agent_fork_settings";
  schema_version: 1;
  agent_id: string;
  policy: {
    policy: "not_allowed" | "recipients" | "team_members";
    row_version: number;
    updated_at: string | null;
  };
  lineage: null | {
    lineage_id: string;
    source_kind: "team" | "system";
    source_agent_id: string | null;
    source_display_name: string;
    source_revision_ref: string;
    fork_revision_ref: string;
    source_accessible: boolean;
    upstream: {
      current_revision_ref: string | null;
      update_available: boolean;
      applied_automatically: false;
      comparison: null | {
        upstream_changes: Record<string, boolean | number>;
        local_changed_since_fork: boolean;
      };
    };
    dependency_gaps: AgentForkDependencyGap[];
    created_at: string;
  };
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, {
    credentials: "include",
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) throw await readApiError(response);
  return response.json() as Promise<T>;
}

function requestKeyForApi(): string {
  return randomUuid();
}

export function listAgentCatalog(params?: {
  q?: string;
  source?: string;
  capability?: string;
  domain?: string;
  job?: string;
  tag?: string;
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
}) {
  const search = new URLSearchParams();
  if (params?.q) search.set("q", params.q);
  if (params?.source && params.source !== "all") search.set("source", params.source);
  if (params?.capability && params.capability !== "all") {
    search.set("capability", params.capability);
  }
  if (params?.domain && params.domain !== "all") search.set("domain", params.domain);
  if (params?.job && params.job !== "all") search.set("job", params.job);
  if (params?.tag && params.tag !== "all") search.set("tag", params.tag);
  if (params?.cursor) search.set("cursor", params.cursor);
  if (params?.limit) search.set("limit", String(params.limit));
  const suffix = search.size ? `?${search}` : "";
  return json<AgentCatalogCollection>(`/api/v1/agents/catalog${suffix}`, { signal: params?.signal });
}

export function getAgentDetail(agentId: string) {
  return json<AgentCatalogItem>(`/api/v1/agents/${encodeURIComponent(agentId)}`);
}

export function getAgentCapabilityReport(agentId: string, locale: string) {
  const search = new URLSearchParams({ locale });
  return json<AgentCapabilityReport>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/capabilities?${search}`,
  );
}

export function previewAgentFork(agentId: string) {
  return json<AgentForkPreview>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/fork-preview`,
    { method: "POST" },
  );
}

export function forkAgent(
  agentId: string,
  value: { sourceRevisionRef: string; displayName: string; idempotencyKey: string },
) {
  return json<AgentForkResult>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/fork`,
    {
      method: "POST",
      headers: { "Idempotency-Key": value.idempotencyKey },
      body: JSON.stringify({
        source_revision_ref: value.sourceRevisionRef,
        display_name: value.displayName,
      }),
    },
  );
}

export function getAgentForkSettings(agentId: string) {
  return json<AgentForkSettings>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/fork-settings`,
  );
}

export function updateAgentForkPolicy(
  agentId: string,
  policy: AgentForkSettings["policy"]["policy"],
  expectedRowVersion: number,
) {
  return json<AgentForkSettings["policy"] & { agent_id: string }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/fork-policy`,
    {
      method: "PUT",
      body: JSON.stringify({ policy, expected_row_version: expectedRowVersion }),
    },
  );
}

export function listManagedAgents(
  view: AgentManagementView,
  params?: { cursor?: string; limit?: number; signal?: AbortSignal },
) {
  const search = new URLSearchParams({ view });
  if (params?.cursor) search.set("cursor", params.cursor);
  if (params?.limit) search.set("limit", String(params.limit));
  return json<AgentManagementCollection>(
    `/api/v1/agents/manage?${search}`,
    { signal: params?.signal, cache: "no-store" },
  );
}

export function getAgentRolePreferences() {
  return json<AgentRolePreferences>("/api/v1/agents/role-preferences");
}

export function setAgentRolePreference(
  roleId: AgentRoleId,
  assignmentId: string,
  expectedRowVersion: number,
) {
  return json<AgentRolePreferences>(
    `/api/v1/agents/role-preferences/${encodeURIComponent(roleId)}`,
    {
      method: "PUT",
      body: JSON.stringify({
        assignment_id: assignmentId,
        expected_row_version: expectedRowVersion,
      }),
    },
  );
}

export function clearAgentRolePreference(
  roleId: AgentRoleId,
  expectedRowVersion: number,
) {
  return json<AgentRolePreferences>(
    `/api/v1/agents/role-preferences/${encodeURIComponent(roleId)}`,
    {
      method: "DELETE",
      body: JSON.stringify({ expected_row_version: expectedRowVersion }),
    },
  );
}

export function hireAgent(agentId: string) {
  return json<AgentAssignmentMutation>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/hire`,
    { method: "POST" },
  );
}

export function setAgentPersonalStatus(
  agentId: string,
  status: "active" | "disabled",
  rowVersion: number,
) {
  return json<AgentAssignmentMutation>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/personal-status`,
    {
      method: "POST",
      body: JSON.stringify({ status, row_version: rowVersion }),
    },
  );
}

export function getAgentUnhireImpact(agentId: string) {
  return json<AgentUnhireImpact>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/unhire-impact`,
  );
}

export function unhireAgent(
  agentId: string,
  impact: AgentUnhireImpact,
  scheduleAction?: "pause" | "expire",
) {
  return json<AgentAssignmentMutation>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/unhire`,
    {
      method: "POST",
      body: JSON.stringify({
        row_version: impact.assignment_row_version,
        impact_token: impact.impact_token,
        schedule_action: scheduleAction,
      }),
    },
  );
}

export function previewAgentTeamDefault(agentId: string, action: "set" | "remove") {
  return json<AgentTeamDefaultPreview>(
    `/api/v1/agents/team-defaults/${encodeURIComponent(agentId)}/preview`,
    { method: "POST", body: JSON.stringify({ action }) },
  );
}

export function setAgentTeamDefault(agentId: string) {
  return json<AgentTeamDefaultPreview>(
    `/api/v1/agents/team-defaults/${encodeURIComponent(agentId)}`,
    { method: "PUT" },
  );
}

export function removeAgentTeamDefault(agentId: string) {
  return json<AgentTeamDefaultPreview>(
    `/api/v1/agents/team-defaults/${encodeURIComponent(agentId)}`,
    { method: "DELETE" },
  );
}

export async function getAgentAuthoringCatalog() {
  const catalog = await json<AgentAuthoringCatalog>("/api/v1/agents/authoring-catalog");
  // Skill operations follow the selected Skills; an older server may still list them as Tools.
  return Array.isArray(catalog?.tools) ? { ...catalog, tools: withoutSkillOperationTools(catalog.tools) } : catalog;
}

export function getAgentOptionalSkillCatalog(agentId: string, query: string, offset = 0, signal?: AbortSignal) {
  const params = new URLSearchParams({ q: query, offset: String(offset) });
  return json<unknown>(`/api/v1/agents/${encodeURIComponent(agentId)}/optional-skill-catalog?${params}`, { signal });
}

export function getAgentWorkingCopy(agentId: string) {
  return json<AgentWorkingCopyDetail>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/working-copy`,
  );
}

export function getAgentTaxonomy(agentId: string) {
  return json<AgentTaxonomyManager>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/taxonomy`,
  );
}

export function suggestAgentTaxonomy(agentId: string, expectedSourceHash: string) {
  return json<AgentTaxonomySuggestion>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/taxonomy/suggest`,
    { method: "POST", body: JSON.stringify({ expected_source_hash: expectedSourceHash }) },
  );
}

export function saveAgentTaxonomy(
  agentId: string,
  value: {
    domains: string[];
    jobs: string[];
    team_tag_ids: string[];
    new_team_tags: string[];
    expected_working_copy_row_version: number;
    suggestion_id: string | null;
  },
) {
  return json<AgentTaxonomyManager>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/taxonomy`,
    { method: "PUT", body: JSON.stringify(value) },
  );
}

export function getAgentTeamTags() {
  return json<AgentTeamTagManager>("/api/v1/agents/taxonomy/tags");
}

export function createAgentTeamTag(displayName: string) {
  return json<AgentTeamTag>("/api/v1/agents/taxonomy/tags", {
    method: "POST",
    body: JSON.stringify({ display_name: displayName }),
  });
}

export function changeAgentTeamTagStatus(
  tagId: string,
  value: {
    action: "deactivate" | "reactivate" | "merge";
    row_version: number;
    target_tag_id?: string;
  },
) {
  return json<AgentTeamTag>(`/api/v1/agents/taxonomy/tags/${encodeURIComponent(tagId)}`, {
    method: "PATCH",
    body: JSON.stringify(value),
  });
}

export function getAgentEvalManager(agentId: string) {
  return json<AgentEvalManager>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-suites`,
  );
}

export function getAgentLocalizations(agentId: string) {
  return json<AgentLocalizationManager>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/localizations`,
  );
}

export function saveAgentLocalization(
  agentId: string,
  locale: string,
  value: {
    source_locale: string;
    fields: { display_name: string; summary: string };
    expected_source_hash: string;
    expected_row_version: number | null;
  },
) {
  return json<AgentLocalizedListing>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/localizations/${encodeURIComponent(locale)}`,
    { method: "PUT", body: JSON.stringify(value) },
  );
}

export function suggestAgentLocalization(
  agentId: string,
  value: {
    source_locale: string;
    target_locale: string;
    expected_source_hash: string;
    expected_row_version: number | null;
  },
) {
  return json<AgentLocalizedListing>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/localizations/suggest`,
    { method: "POST", body: JSON.stringify(value) },
  );
}

export function confirmAgentLocalization(
  agentId: string,
  locale: string,
  rowVersion: number,
) {
  return json<AgentLocalizedListing>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/localizations/${encodeURIComponent(locale)}/confirm`,
    { method: "POST", body: JSON.stringify({ row_version: rowVersion }) },
  );
}

export function saveAgentEvalSuite(
  agentId: string,
  value: {
    suite_id: string | null;
    name: string;
    visibility: "owner" | "reviewer" | "team_evidence";
    cases: AgentEvalCase[];
  },
) {
  return json<AgentEvalManager>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-suites`,
    { method: "POST", body: JSON.stringify(value) },
  );
}

export function suggestAgentEvalCases(agentId: string, goal: string) {
  return json<{ kind: "agent_eval_case_suggestions"; schema_version: 1; cases: AgentEvalCase[] }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-cases/suggest`,
    { method: "POST", body: JSON.stringify({ goal, examples: [] }) },
  );
}

/** Grade a case's unconfirmed data facts and judgment criteria on the newest Live answer; nothing is stored. */
export function previewAgentEvalPoints(
  agentId: string,
  value: { task: string; points: Array<{ id: string; kind: "data_fact" | "judgment"; text: string }> },
) {
  return json<AgentEvalPointPreview>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-points/preview`,
    { method: "POST", body: JSON.stringify(value) },
  );
}

export function listAgentEvalConversationCandidates(agentId: string) {
  return json<{ kind: "agent_eval_conversation_candidates"; schema_version: 1; max_selection: number; items: AgentEvalConversationCandidate[] }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-cases/conversation-candidates`,
  );
}

export function suggestAgentEvalCasesFromTurns(agentId: string, turnIds: string[]) {
  return json<{ kind: "agent_eval_case_suggestions"; schema_version: 1; cases: AgentEvalCase[] }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-cases/suggest`,
    { method: "POST", body: JSON.stringify({ from_turns: turnIds }) },
  );
}

/** The requester's Turns in one conversation they may share with the answering Agent's Owner. */
export function listTurnEvalShares(sessionId: string) {
  return json<{ kind: "turn_eval_shares"; schema_version: 1; items: TurnEvalShare[] }>(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/eval-shares`,
  );
}

/** Let the answering Agent's Owner draft an evaluation case from this Turn (`shared`), or withdraw that. */
export function setTurnEvalShare(turnId: string, shared: boolean) {
  return json<{ kind: "turn_eval_share"; schema_version: 1; turn_id: string; shared: boolean; agent_name?: string }>(
    `/api/v1/turns/${encodeURIComponent(turnId)}/eval-share`,
    { method: shared ? "PUT" : "DELETE" },
  );
}

/** Give or update a thumbs-down on the requester's team-Agent answer (`value`), or clear it (`null`). */
export function setTurnAnswerFeedback(turnId: string, value: { reason_codes: string[]; comment: string | null } | null) {
  return json<{ kind: "turn_answer_feedback"; schema_version: 1; turn_id: string; feedback: AnswerFeedback | null; shared?: boolean }>(
    `/api/v1/turns/${encodeURIComponent(turnId)}/feedback`,
    value ? { method: "PUT", body: JSON.stringify(value) } : { method: "DELETE" },
  );
}

/** Take a member's shared thumbs-down off the Owner's Pending list: dismissed, or opened in Coach. */
export function handleAgentMemberFeedback(agentId: string, turnId: string, action: "dismissed" | "coach") {
  return json<{ kind: "agent_member_feedback_handled"; schema_version: 1; turn_id: string; handled: string }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/member-feedback/${encodeURIComponent(turnId)}/handled`,
    { method: "POST", body: JSON.stringify({ action }) },
  );
}

export function runAgentEval(
  agentId: string,
  value: {
    suite_revision_id: string;
    workspace_id: string;
    target?: "auto" | "candidate" | "live";
    attempts?: number;
    expected_revision_id?: string;
  },
  idempotencyKey = randomUuid(),
) {
  return json<AgentEvalRun>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-runs`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(value),
    },
  );
}

export function getAgentEvalRun(agentId: string, runId: string) {
  return json<AgentEvalRun>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-runs/${encodeURIComponent(runId)}`,
  );
}

export function getAgentEvalRunCaseAnswers(agentId: string, runId: string, caseRevisionId: string) {
  return json<AgentEvalRunCaseAnswers>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseRevisionId)}/answers`,
  );
}

export function cancelAgentEvalRun(agentId: string, runId: string) {
  return json<AgentEvalRun>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-runs/${encodeURIComponent(runId)}/cancel`,
    { method: "POST" },
  );
}

export function updateAgentEvalTriggerPolicy(
  agentId: string,
  value: NonNullable<AgentEvalManager["trigger_policy"]>,
) {
  return json<NonNullable<AgentEvalManager["trigger_policy"]>>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-trigger-policy`,
    { method: "PUT", body: JSON.stringify(value) },
  );
}

export function saveAgentWorkingCopy(
  agentId: string,
  value: {
    definition: Record<string, unknown>;
    listing: Record<string, unknown>;
    audience: AgentAudienceSnapshot;
    expected_row_version: number | null;
  },
) {
  return json<AgentWorkingCopyMutation>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/working-copy`,
    { method: "PATCH", body: JSON.stringify(value) },
  );
}

export function runAgentSafetyCheck(
  agentId: string,
  workingCopyId: string,
  workingCopyRowVersion: number,
) {
  return json<AgentSafetyCheckReport>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/safety-checks`,
    {
      method: "POST",
      body: JSON.stringify({
        working_copy_id: workingCopyId,
        working_copy_row_version: workingCopyRowVersion,
      }),
    },
  );
}

export function publishAgentWorkingCopy(
  agentId: string,
  value: {
    working_copy_id: string;
    working_copy_row_version: number;
    release_note: string | null;
    expected_eval_policy_revision_id?: string | null;
    workspace_id?: string;
    /** "Prepare from latest draft": the prepared candidate to replace, withdrawn in the same transaction. */
    replace_candidate?: { candidate_id: string; candidate_row_version: number; candidate_hash: string };
  },
  idempotencyKey: string,
) {
  return json<AgentPublicationResult | AgentVersionPreparation>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(value),
    },
  );
}

export function getAgentVersionWorkbench(agentId: string) {
  return json<AgentVersionWorkbench>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/version-workbench`,
  );
}

export function createAgentCoachProposal(agentId: string, goal: string, idempotencyKey: string) {
  return json<AgentChangeProposal>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/change-proposals/coach`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ goal, evidence_excerpts: [] }),
    },
  );
}

/**
 * Merge the selected groups into the draft. `base` is the saved draft's row version, or, while the
 * draft still matches the released version (no saved draft), that release's id: the server opens the
 * draft from it and merges in one request.
 */
export function applyAgentChangeProposal(
  agentId: string,
  proposal: AgentChangeProposal,
  selectedGroupIds: string[],
  base: number | { liveReleaseId: string },
) {
  return json<{ status: AgentChangeProposal["status"] }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/change-proposals/${encodeURIComponent(proposal.proposal_id)}/apply`,
    {
      method: "POST",
      body: JSON.stringify({
        selected_group_ids: selectedGroupIds,
        expected_proposal_row_version: proposal.row_version,
        ...(typeof base === "number" ? { expected_working_copy_row_version: base } : { expected_live_release_id: base.liveReleaseId }),
      }),
    },
  );
}

/**
 * Undo or reapply Coach's changes per field, or settle an undo conflict: keep the current value, or
 * restore the value before Coach (one write that replaces the Owner's later change).
 * An undo conflict rejects with `agent_change_undo_conflict` and `details.conflicts`; nothing is written.
 */
export function changeAgentCoachFields(
  agentId: string,
  action: "undo" | "reapply" | "keep" | "restore",
  fields: string[],
  expectedWorkingCopyRowVersion: number,
) {
  return json<AgentCoachChangeResult>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/coach-changes/${action}`,
    {
      method: "POST",
      body: JSON.stringify({ fields, expected_working_copy_row_version: expectedWorkingCopyRowVersion }),
    },
  );
}

export function dismissAgentChangeProposal(agentId: string, proposal: AgentChangeProposal) {
  return json<AgentChangeProposal>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/change-proposals/${encodeURIComponent(proposal.proposal_id)}/dismiss`,
    {
      method: "POST",
      body: JSON.stringify({ expected_proposal_row_version: proposal.row_version }),
    },
  );
}

export function acceptAgentProposalSuggestedCases(
  agentId: string,
  proposal: AgentChangeProposal,
  suiteRevisionId: string | null,
  cases: Array<{ suggestedCaseId: string; caseKey: string; task: string; failureMode: string; caseSpec?: AgentSuggestedCaseSpec }>,
) {
  return json<{ decision_id: string; suite_revision_id: string }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/change-proposals/${encodeURIComponent(proposal.proposal_id)}/suggested-cases/accept`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_proposal_row_version: proposal.row_version,
        expected_active_suite_revision_id: suiteRevisionId,
        cases: cases.map((item) => ({
          suggested_case_id: item.suggestedCaseId,
          edited_case: {
            ...(item.caseSpec ?? {
              case_key: item.caseKey,
              expected_contract: { failure_mode: item.failureMode },
              fixture_ref: {},
              scorer_config: {},
              effect_policy: { mode: "no_effects" },
            }),
            task: item.task,
          },
        })),
      }),
    },
  );
}

export function dismissAgentProposalSuggestedCases(
  agentId: string,
  proposal: AgentChangeProposal,
  suggestedCaseIds: string[],
) {
  return json<{ decision_id: string }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/change-proposals/${encodeURIComponent(proposal.proposal_id)}/suggested-cases/dismiss`,
    {
      method: "POST",
      body: JSON.stringify({
        expected_proposal_row_version: proposal.row_version,
        suggested_case_ids: suggestedCaseIds,
      }),
    },
  );
}

export function startAgentVersionComparison(
  agentId: string,
  workbench: AgentVersionWorkbench,
  idempotencyKey: string,
) {
  const candidate = workbench.candidate_identity;
  const plan = workbench.active_plan_identity;
  if (!candidate || !plan || workbench.control_row_version === null) {
    throw new Error("agent_eval_identity_missing");
  }
  return json<AgentVersionComparison>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidate.candidate_id)}/comparisons`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        candidate_hash: candidate.candidate_hash,
        candidate_row_version: candidate.row_version,
        expected_control_row_version: workbench.control_row_version,
        plan_hash: plan.hash,
        plan_id: plan.id,
        run_kind: "release",
        // A plan behind the latest saved test cases is recompiled first, as a new plan on the record.
        ...(workbench.evaluation_plan_behind ? { refresh_plan: true } : {}),
      }),
    },
  );
}

/** D6 usage estimate for the exact active Plan; 404 when run insights are disabled. */
export function getAgentComparisonEstimate(agentId: string, candidateId: string, plan: { id: string; hash: string }) {
  const query = new URLSearchParams({ plan_id: plan.id, plan_hash: plan.hash });
  return json<AgentEvalUsageEstimate>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidateId)}/comparison-estimate?${query.toString()}`,
  );
}

/** D8 changed cases of a completed comparison (Owner only); 404 when disabled, 409 while not completed. */
export function getAgentVersionCaseChanges(agentId: string, candidateId: string, executionId: string, page: { offset?: number; limit?: number } = {}) {
  const query = new URLSearchParams({ offset: String(page.offset ?? 0), limit: String(page.limit ?? 50) });
  return json<AgentVersionCaseChanges>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidateId)}/comparisons/${encodeURIComponent(executionId)}/case-changes?${query.toString()}`,
  );
}

/** D8 both versions' answers for one case (Owner only). */
export function getAgentVersionCaseAnswers(agentId: string, candidateId: string, executionId: string, caseRevisionId: string) {
  return json<AgentVersionCaseAnswers>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidateId)}/comparisons/${encodeURIComponent(executionId)}/cases/${encodeURIComponent(caseRevisionId)}/answers`,
  );
}

/** R28: points the candidate failed in a comparison, with quotes (Owner only). */
export function getAgentVersionUnmetPoints(agentId: string, candidateId: string, executionId: string) {
  return json<{ kind: "agent_version_unmet_points"; schema_version: 1; execution_id: string; items: AgentVersionUnmetPoint[] }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidateId)}/comparisons/${encodeURIComponent(executionId)}/unmet-points`,
  );
}

/** Judged wrongly (R13): records the disputed verdict; the comparison's result doesn't change. */
export function reportAgentEvalPointJudgedWrongly(
  agentId: string,
  pointId: string,
  caseResult: { run_id: string; case_revision_id: string; attempt: number },
) {
  return json<AgentEvalPointFeedback>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-points/${encodeURIComponent(pointId)}/feedback`,
    { method: "POST", body: JSON.stringify({ case_result: caseResult }) },
  );
}

/** Mark as good answer: an evaluation answer becomes the point's good calibration example. */
export function markAgentEvalPointGoodAnswer(
  agentId: string,
  pointId: string,
  value: { case_revision_id: string; answer_ref: string },
) {
  return json<{ kind: "agent_eval_point_example"; schema_version: 1; id: string; suite_id: string; point_id: string; example_kind: "good" }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-points/${encodeURIComponent(pointId)}/examples`,
    { method: "POST", body: JSON.stringify({ kind: "good", ...value }) },
  );
}

/** Reference to one evaluation attempt's answer, as calibration examples store it. */
export function agentEvalAttemptAnswerRef(runId: string, caseRevisionId: string, attempt: number): string {
  return `attempt:${runId}:${caseRevisionId}:${attempt}`;
}

export function cancelAgentVersionComparison(agentId: string, workbench: AgentVersionWorkbench) {
  const candidate = workbench.candidate_identity;
  const execution = workbench.active_execution_summary;
  if (!candidate || !execution) throw new Error("agent_eval_identity_missing");
  return json<AgentVersionComparison>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidate.candidate_id)}/comparisons/${encodeURIComponent(execution.execution_id)}/cancel`,
    {
      method: "POST",
      body: JSON.stringify({ expected_execution_row_version: execution.row_version }),
    },
  );
}

export function withdrawAgentVersionCandidate(agentId: string, workbench: AgentVersionWorkbench) {
  const candidate = workbench.candidate_identity;
  if (!candidate) throw new Error("agent_eval_identity_missing");
  return json<AgentPublicationResult>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidate.candidate_id)}/withdraw`,
    {
      method: "POST",
      body: JSON.stringify({
        candidate_hash: candidate.candidate_hash,
        candidate_row_version: candidate.row_version,
      }),
    },
  );
}

export function createAgentVersionOverride(
  agentId: string,
  workbench: AgentVersionWorkbench,
  notesByReason: Record<string, string>,
) {
  const candidate = workbench.candidate_identity;
  const plan = workbench.active_plan_identity;
  const snapshot = workbench.active_snapshot_identity;
  if (!candidate || !plan || !snapshot || workbench.control_row_version === null) {
    throw new Error("agent_eval_identity_missing");
  }
  return json<AgentVersionOverride>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidate.candidate_id)}/overrides`,
    {
      method: "POST",
      body: JSON.stringify({
        candidate_row_version: candidate.row_version,
        expected_control_row_version: workbench.control_row_version,
        plan_hash: plan.hash,
        plan_id: plan.id,
        reason_overrides: workbench.reason_codes.map((reasonCode) => ({
          reason_code: reasonCode,
          reason_note: notesByReason[reasonCode]?.trim(),
        })),
        snapshot_hash: snapshot.hash,
        snapshot_id: snapshot.id,
        team_evidence_selection: { regression_category_ids: [], suite_revision_ids: [] },
      }),
    },
  );
}

export function completeAgentVersionRelease(
  agentId: string,
  workbench: AgentVersionWorkbench,
  action: "activate" | "submit",
  idempotencyKey: string,
  /** The note typed in the release dialog: stored on the release (activate) or on the submission (submit), never on the candidate. */
  releaseNote: string | null = null,
) {
  const candidate = workbench.candidate_identity;
  const plan = workbench.active_plan_identity;
  const snapshot = workbench.active_snapshot_identity;
  if (!candidate || !plan || !snapshot) throw new Error("agent_eval_identity_missing");
  return json<AgentPublicationResult>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidate.candidate_id)}/${action}`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        candidate_hash: candidate.candidate_hash,
        candidate_row_version: candidate.row_version,
        override_hash: workbench.active_override_identity?.hash ?? null,
        override_id: workbench.active_override_identity?.id ?? null,
        plan_hash: plan.hash,
        plan_id: plan.id,
        snapshot_hash: snapshot.hash,
        snapshot_id: snapshot.id,
        ...(releaseNote ? { release_note: releaseNote } : {}),
      }),
    },
  );
}

/**
 * "Release as vN" for a private Agent: the server releases the latest saved draft (preparing it when no tested
 * candidate covers it) and records the test state the dialog showed; a different current state is refused with
 * `agent_release_test_state_changed`.
 */
export function releasePrivateAgentDraft(
  agentId: string,
  workbench: AgentVersionWorkbench,
  options: { workspaceId: string; testState: AgentReleaseTestState; releaseNote: string | null; idempotencyKey: string },
) {
  const draft = workbench.next_draft_identity;
  return json<AgentPublicationResult>(`/api/v1/agents/${encodeURIComponent(agentId)}/releases`, {
    method: "POST",
    headers: { "Idempotency-Key": options.idempotencyKey },
    body: JSON.stringify({
      workspace_id: options.workspaceId,
      working_copy_id: draft?.id ?? null,
      working_copy_row_version: draft?.row_version ?? null,
      expected_test_state: options.testState,
      ...(options.releaseNote ? { release_note: options.releaseNote } : {}),
    }),
  });
}

export interface AgentVersionTestResult {
  kind: "agent_version_test";
  schema_version: 1;
  /** started: prepared (when needed) and comparing; running: a test was already running; current: the passing result still covers the draft. */
  step: "started" | "running" | "current" | "prepared";
  preparation: AgentVersionPreparation | null;
  comparison: AgentVersionComparison | null;
}

/**
 * "Test this change": one server action from the latest saved draft to a running release comparison.
 * The server reads every version itself, reuses or replaces the candidate and refreshes the plan; a retry
 * with the same key finishes whatever step is left.
 */
export function testAgentChange(agentId: string, workspaceId: string, idempotencyKey: string) {
  return json<AgentVersionTestResult>(`/api/v1/agents/${encodeURIComponent(agentId)}/tests`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ workspace_id: workspaceId }),
  });
}

export function listAgentReleaseInbox() {
  return json<{
    kind: "agent_release_inbox";
    schema_version: 1;
    items: AgentReleaseInboxItem[];
    count: number;
  }>("/api/v1/agents/release-inbox");
}

export function getAgentReleaseInboxCount(signal?: AbortSignal) {
  return json<{ kind: "agent_release_inbox_count"; schema_version: 1; count: number }>(
    "/api/v1/agents/release-inbox/count",
    { signal, cache: "no-store" },
  );
}

export function getAgentReleaseCandidate(agentId: string, candidateId: string) {
  return json<AgentReleaseCandidateDetail>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/release-candidates/${encodeURIComponent(candidateId)}`,
  );
}

export function approveAgentReleaseCandidate(value: AgentReleaseCandidateDetail) {
  const evaluation = value.evaluation && "schema_version" in value.evaluation && value.evaluation.schema_version === 2
    ? value.evaluation
    : null;
  return json<AgentPublicationResult>(
    `/api/v1/agents/release-candidates/${encodeURIComponent(value.candidate_id)}/approve`,
    {
      method: "POST",
      headers: evaluation ? { "Idempotency-Key": requestKeyForApi() } : undefined,
      body: JSON.stringify({
        candidate_hash: value.candidate_hash,
        candidate_row_version: value.candidate_row_version,
        override_hash: evaluation?.override?.hash ?? null,
        override_id: evaluation?.override?.id ?? null,
        plan_hash: evaluation?.plan_hash ?? null,
        plan_id: evaluation?.plan_id ?? null,
        snapshot_hash: evaluation?.snapshot_hash ?? null,
        snapshot_id: evaluation?.snapshot_id ?? null,
      }),
    },
  );
}

export function rejectAgentReleaseCandidate(
  value: AgentReleaseCandidateDetail,
  reasonCode: string,
) {
  const evaluation = value.evaluation && "schema_version" in value.evaluation && value.evaluation.schema_version === 2
    ? value.evaluation
    : null;
  return json<AgentPublicationResult>(
    `/api/v1/agents/release-candidates/${encodeURIComponent(value.candidate_id)}/reject`,
    {
      method: "POST",
      body: JSON.stringify({
        candidate_hash: value.candidate_hash,
        candidate_row_version: value.candidate_row_version,
        override_hash: evaluation?.override?.hash ?? null,
        override_id: evaluation?.override?.id ?? null,
        plan_hash: evaluation?.plan_hash ?? null,
        plan_id: evaluation?.plan_id ?? null,
        reason_code: reasonCode,
        snapshot_hash: evaluation?.snapshot_hash ?? null,
        snapshot_id: evaluation?.snapshot_id ?? null,
      }),
    },
  );
}

export function withdrawAgentReleaseCandidate(value: AgentReleaseCandidateDetail) {
  return json<AgentPublicationResult>(
    `/api/v1/agents/${encodeURIComponent(value.agent.id)}/release-candidates/${encodeURIComponent(value.candidate_id)}/withdraw`,
    {
      method: "POST",
      body: JSON.stringify({
        candidate_hash: value.candidate_hash,
        candidate_row_version: value.candidate_row_version,
      }),
    },
  );
}

export function listAgentReleases(agentId: string) {
  return json<{
    kind: "agent_release_history";
    schema_version: 1;
    agent_id: string;
    items: AgentReleaseHistoryItem[];
  }>(`/api/v1/agents/${encodeURIComponent(agentId)}/releases`);
}

export function createAgentRollback(
  agentId: string,
  sourceReleaseId: string,
  aggregateRowVersion: number,
  workingCopyRowVersion: number | null,
) {
  return json<AgentWorkingCopyMutation>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/rollback`,
    {
      method: "POST",
      body: JSON.stringify({
        source_release_id: sourceReleaseId,
        aggregate_row_version: aggregateRowVersion,
        working_copy_row_version: workingCopyRowVersion,
      }),
    },
  );
}

export function archiveAgent(agentId: string, aggregateRowVersion: number) {
  return json<{ state: "archived"; aggregate_row_version: number; paused_schedule_count: number }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/archive`,
    { method: "POST", body: JSON.stringify({ aggregate_row_version: aggregateRowVersion }) },
  );
}

export function restoreAgent(agentId: string, aggregateRowVersion: number) {
  return json<{ state: "active"; aggregate_row_version: number }>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/restore`,
    { method: "POST", body: JSON.stringify({ aggregate_row_version: aggregateRowVersion }) },
  );
}

export function setManagedAgentResourceStatus(
  agentId: string,
  status: "enabled" | "disabled",
  aggregateRowVersion: number,
) {
  return json<Record<string, unknown>>(
    `/api/v1/team-subagents/${encodeURIComponent(agentId)}/status`,
    {
      method: "PUT",
      headers: { "If-Match": `W/\"${agentId}:${aggregateRowVersion}\"` },
      body: JSON.stringify({ status, acknowledged_warning_codes: [] }),
    },
  );
}

export function getAgentKnowledgeSources(agentId: string, query: string, selected: string[], offset = 0, signal?: AbortSignal) {
  const params = new URLSearchParams({ q: query, offset: String(offset), limit: "20" });
  selected.forEach((id) => params.append("selected", id));
  return json<import("./agent-knowledge-sources").KnowledgeSourceCatalog>(`/api/v1/agents/${encodeURIComponent(agentId)}/knowledge-sources?${params}`, { signal });
}
