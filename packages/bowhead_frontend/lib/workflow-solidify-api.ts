/**
 * Workflow solidification API wrapper.
 *
 * Backend: /api/v1/workflow-solidify/{distill,dry-run} + /api/v1/team-skills + /api/v1/skills/generate.
 * Cookie-authed (apiJson → fetchWithRefresh credentials:"include"); the BOWHEAD_CCA_WORKFLOW_CREATOR
 * flag + per-request multi-tenant resolution + cross-user 404 are enforced server-side (FE never
 * re-checks). The card calls these directly with the user's session.
 */

import { apiJson, apiVoid } from "./api-client";
import type { SkillFileContentDTO } from "./api";
import type { ResourceUsageResponse, ResourceUsageSummary } from "./resource-usage";
import type { SkillResourceChangeGuard, SkillResourceChangeImpact } from "./api";
import type { Audience } from "./content-share-api";

// ── response shapes (match the gateway) ──────────────────────────────────────

export interface DistillSlot {
  token: string;
  example?: string;
}
export interface DistillScript {
  name: string;
  content: string;
  from_seq?: number;
}
export interface DistillSubagentSpec {
  system_prompt: string;
  tool_refs: string[];
  output_schema: unknown | null;
}
/** POST /distill response (routes/workflow_solidify.distill + parse_distill_output). */
export interface DistillResult {
  ok: boolean;
  /** present on oral fallback: {ok:false, fallback:"oral", reason} */
  fallback?: string;
  reason?: string;
  reason_code?: string;
  diagnostics?: unknown;
  suggested_form: "skill" | "subagent";
  name: string;
  display_name: string;
  description: string;
  body_md: string;
  subagent_spec: DistillSubagentSpec;
  tool_refs: string[];
  slots: DistillSlot[];
  checkpoints: string[];
  scripts: DistillScript[];
  truncated_steps: number[];
  form_hint_applied?: boolean;
  /** "session" = distill the entire conversation (scheduled-recipe-runs S2); single-turn distillation omits this field. */
  source?: "session";
  /** Number of turns merged by whole-conversation distillation (count only turns that actually did work). */
  turn_count?: number;
  /** Stable code when a long session is windowed: disclose that only recent turns were covered, rather than imply the entire conversation. */
  truncated_notice_code?: string;
}

export interface DryRunCheckpoint {
  text: string;
  pass: boolean | null;
  reason?: string;
}
/** POST /dry-run response. */
export interface DryRunResult {
  ok: boolean;
  verdict: "pass" | "fail" | "skipped" | "error";
  checkpoints: DryRunCheckpoint[];
  scratch_session_id: string;
  final_answer_preview: string;
  dispatch_error?: string | null;
  dispatch_error_code?: string | null;
  dispatch_error_diagnostics?: unknown;
}

export interface TeamSkillPublic {
  id: string;
  name: string;
  display_name: string;
  description: string;
  visibility: string;
  status: string;
  verdict: string | null;
  size_bytes: number;
  created_by?: string;
  created_at?: string | null;
  updated_at?: string | null;
  // content-permissions: list-sharing contract (backend _public_enriched).
  audience?: Audience;
  share_count?: number;
  shared_by?: string | null;
  /**
   * Skills-page IA redesign: creator display name (batch lookup on list; used by the "来自 X" badge).
   * Null when the creator cannot be resolved; render it through `teamSkillOwnerName` (lib/team-skill-owner).
   */
  created_by_name?: string | null;
  // ── skill-team-governance: two approval slots (PRD §3.1; member-visible rows are always approved) ──
  approval?: "pending" | "approved" | "rejected" | string;
  // Approval/candidate details below appear only for authors/admins (backend include_review).
  submitted_by?: string | null;
  approved_by?: string | null;
  approved_at?: string | null;
  approved_sha256?: string | null;
  rejected_reason?: string | null;
  rejected_at?: string | null;
  /**
   * Track B batch 4 (cross-track ⑨): trace of promotion after the author overrides blockers (import_status=overridden).
   * Server computes this from the personal instance at promotion for reviewers; null = not promoted through this path.
   * `red_count` can be null: an unreadable report leaves the count unknown, not 0 (which would imply knowingly enabling with 0 blockers).
   */
  import_provenance?: {
    status: string;
    red_count: number | null;
    truncated: boolean;
    findings: { id?: string; code?: string; severity?: string; path?: string; line?: number; subject?: string; message_zh?: string }[];
  } | null;
  candidate_state?: "pending" | "rejected" | string | null;
  candidate_submitted_at?: string | null;
  candidate_sha256?: string | null;
  candidate_rejected_reason?: string | null;
  /** L1 pending approve/reject CAS key (source disk hash computed on read). */
  source_sha256?: string | null;
  /** Requester's current per-user enabled state (team-group switch in settings; write path = personal PATCH of the team leg). */
  my_enabled?: boolean;
  /** Risk report (list supplies it to authors/admins so chips are visible even on collapsed approval cards). */
  risk_report?: {
    /** Script count per entry extension, as the upload risk report stores it. */
    langs?: Record<string, number>;
    keywords?: string[];
    shebangs?: string[];
    [k: string]: unknown;
  } | null;
  /** Risk report of the pending update candidate (review view only); the update card shows these chips. */
  candidate_risk_report?: TeamSkillPublic["risk_report"];
  usage_summary?: ResourceUsageSummary | null;
}

/** GET /team-skills/{id} detail additions (drawer/approval card). */
export interface TeamSkillDetail extends TeamSkillPublic {
  content_sha256?: string | null;
  authoring_enabled?: boolean;
  script_execution?: "none" | "builtin" | "sandbox" | "disabled";
  eval_files?: {name: string; size: number}[];
  /** Display body (**without** frontmatter). */
  body_md?: string;
  /** For editing: complete on-disk SKILL.md, including frontmatter. */
  body_raw?: string;
  /** L2 approval card: candidate-content preview (author/admin view, when a candidate exists). */
  candidate_body_md?: string;
  /**
   * Bundle attachments. Field names match personal skill details (drawer reuses the same "文件" tab);
   * team copies always included references on disk (promote copies the whole directory), but previously lacked a read surface.
   * Optional: treat as empty when older backends omit it.
   */
  references?: { name: string; size: number }[];
  scripts?: { name: string; size: number }[];
  evals_count?: number;
  has_evals?: boolean;
}

// ── calls ─────────────────────────────────────────────────────────────────────

/** Distill a past turn's trace into a reusable recipe draft. Cross-user → 404 (anti-enumeration). */
export function distillWorkflow(turnId: string, formHint?: "skill" | "subagent"): Promise<DistillResult> {
  return apiJson<DistillResult>("/api/v1/workflow-solidify/distill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ turn_id: turnId, form_hint: formHint ?? null }),
  });
}

/**
 * Whole-conversation distillation (scheduled-recipe-runs S2). Backend makes turn_id/session_id mutually exclusive (both/neither → 422).
 *
 * Why: users often reach the correct approach **only after N attempts**: turn 1 uses wrong conventions, turn 2 overturns them, turn 3 concludes.
 * Solidifying just one turn captures either an overturned approach or a partial workflow missing retrieval. Backend merges the conversation
 * under the final-consensus-path rule, retaining only the latest approach.
 */
export function distillSession(sessionId: string, formHint?: "skill" | "subagent"): Promise<DistillResult> {
  return apiJson<DistillResult>("/api/v1/workflow-solidify/distill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, form_hint: formHint ?? null }),
  });
}

/** Dry-run a saved recipe in an isolated scratch session + judge its checkpoints. */
export function dryRunRecipe(args: {
  name: string;
  triggerQuery: string;
  form: "skill" | "team-skill";  // P2 (review): team-skill verdict persists to team_skill, not skill_instance.
  checkpoints: string[];
  workspaceId?: string;
}): Promise<DryRunResult> {
  return apiJson<DryRunResult>("/api/v1/workflow-solidify/dry-run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: args.name, trigger_query: args.triggerQuery, form: args.form,
      checkpoints: args.checkpoints, workspace_id: args.workspaceId ?? null,
    }),
  });
}

/** Save the distilled draft as an team-shared recipe (.data/team_skills/<team_id>/<name>/). */
export function createTeamSkill(args: {
  bodyMd: string;
  scripts: DistillScript[];
  displayName: string;
  description: string;
  visibility: string;
}): Promise<TeamSkillPublic> {
  return apiJson<TeamSkillPublic>("/api/v1/team-skills", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      body_md: args.bodyMd,
      scripts: args.scripts.map((s) => ({ name: s.name, content: s.content })),
      display_name: args.displayName, description: args.description, visibility: args.visibility,
    }),
  });
}

/** List the team's shared recipes (surface C management page · Team tab). Empty {items:[]} when no team / none saved. */
export function listTeamSkills(): Promise<{ items: TeamSkillPublic[]; impact_guard_enabled?: boolean }> {
  return apiJson<{ items: TeamSkillPublic[]; impact_guard_enabled?: boolean }>("/api/v1/team-skills");
}

export function previewTeamSkillImpact(
  skillId: string,
  body: { action: "patch" | "delete"; proposed_change: Record<string, unknown> },
): Promise<SkillResourceChangeImpact> {
  return apiJson<SkillResourceChangeImpact>(
    `/api/v1/team-skills/${encodeURIComponent(skillId)}/impact-preview`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

/** Skills-page detail drawer / approval card: one team skill, including body and candidate preview. */
export function getTeamSkill(skillId: string): Promise<TeamSkillDetail> {
  return apiJson<TeamSkillDetail>(`/api/v1/team-skills/${encodeURIComponent(skillId)}`);
}

export function getTeamSkillUsage(skillId: string): Promise<ResourceUsageResponse> {
  return apiJson<ResourceUsageResponse>(
    `/api/v1/team-skills/${encodeURIComponent(skillId)}/usage`,
  );
}

/** One team skill bundle file (references/scripts/evals), symmetric with personal fetchSkillFile. */
export function getTeamSkillFile(
  skillId: string,
  kind: "references" | "scripts" | "evals",
  filename: string,
): Promise<SkillFileContentDTO> {
  return apiJson<SkillFileContentDTO>(
    `/api/v1/team-skills/${encodeURIComponent(skillId)}/files/${kind}/${encodeURIComponent(filename)}`,
  );
}

/**
 * Edit writes for team skills (symmetric with personal putSkillFile/putSkillBody).
 *
 * Server owns governance: only owner/team_admin may edit submitted/approved team_wide content
 * (403 team_skill_content_admin_required); FE uses the same rule only to decide whether to display the edit entry.
 */
export function putTeamSkillFile(
  skillId: string,
  kind: "references" | "scripts" | "evals",
  filename: string,
  content: string,
  expectedContentSha256?: string,
): Promise<SkillFileContentDTO> {
  return apiJson<SkillFileContentDTO>(
    `/api/v1/team-skills/${encodeURIComponent(skillId)}/files/${kind}/${encodeURIComponent(filename)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, expected_content_sha256: expectedContentSha256 }),
    },
  );
}

export function putTeamSkillBody(skillId: string, content: string, expectedContentSha256?: string): Promise<SkillFileContentDTO> {
  return apiJson<SkillFileContentDTO>(`/api/v1/team-skills/${encodeURIComponent(skillId)}/body`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, expected_content_sha256: expectedContentSha256 }),
  });
}

// ── skill-team-governance: approval endpoints (CAS keys required; admin approves that exact content) ──

export interface ApprovalCasKeys {
  /** L1 (first submission): source disk hash. */
  source_sha256?: string | null;
  /** L2 (update candidate): paired candidate keys. */
  candidate_submitted_at?: string | null;
  candidate_sha256?: string | null;
}

/** Admin approval (409 E_SKILL_CANDIDATE_STALE = content changed; refresh and review again). */
export function approveTeamSkill(skillId: string, keys: ApprovalCasKeys): Promise<TeamSkillPublic> {
  return apiJson<TeamSkillPublic>(`/api/v1/team-skills/${encodeURIComponent(skillId)}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(keys),
  });
}

/** Admin rejection (reason required; same CAS-key semantics). */
export function rejectTeamSkill(skillId: string, keys: ApprovalCasKeys, reason: string): Promise<TeamSkillPublic> {
  return apiJson<TeamSkillPublic>(`/api/v1/team-skills/${encodeURIComponent(skillId)}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...keys, reason }),
  });
}

/** Author withdrawal (L1 pending→creator_only for personal use; L2 clears only the candidate slot, leaving live unchanged). */
export function withdrawTeamSkill(skillId: string): Promise<TeamSkillPublic> {
  return apiJson<TeamSkillPublic>(`/api/v1/team-skills/${encodeURIComponent(skillId)}/withdraw`, {
    method: "POST",
  });
}

/** Copy an approved team snapshot into a personal skill (422 E_SKILL_NAME_EXISTS → retry with overwrite). */
export function copyTeamSkillToPersonal(
  skillId: string,
  overwrite = false,
): Promise<{ name: string; source_type: string; description: string; size_bytes: number; enabled: boolean }> {
  return apiJson(`/api/v1/team-skills/${encodeURIComponent(skillId)}/copy-to-personal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ overwrite }),
  });
}

/** Submit a personal skill to the team (name/title/content in request body; URL contains no user-defined content). */
export function promoteSkill(args: { name: string; title: string; content: string }): Promise<TeamSkillPublic> {
  return apiJson<TeamSkillPublic>("/api/v1/skills/promote", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
}

/** Patch a team recipe's meta (surface C: enable/disable = status; visibility / display_name / description). */
export function patchTeamSkill(
  skillId: string,
  patch: { status?: string; visibility?: string; display_name?: string; description?: string },
  guard?: SkillResourceChangeGuard,
): Promise<TeamSkillPublic> {
  return apiJson<TeamSkillPublic>(`/api/v1/team-skills/${encodeURIComponent(skillId)}`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      ...(guard
        ? {
            "X-Bowhead-Impact-Token": guard.impactToken,
            "Idempotency-Key": guard.idempotencyKey,
          }
        : {}),
    },
    body: JSON.stringify(patch),
  });
}

/** Delete a team recipe (surface C: deletion). 204 → void; on-disk SKILL.md+scripts removed server-side. */
export function deleteTeamSkill(
  skillId: string,
  guard?: SkillResourceChangeGuard,
): Promise<void> {
  return apiVoid(`/api/v1/team-skills/${encodeURIComponent(skillId)}`, {
    method: "DELETE",
    headers: guard
      ? {
          "X-Bowhead-Impact-Token": guard.impactToken,
          "Idempotency-Key": guard.idempotencyKey,
        }
      : undefined,
  });
}

/** Save the distilled draft as a personal skill (reuses the existing conversational-generate path). */
export function createPersonalSkill(args: {
  bodyMd: string;
  scripts: DistillScript[];
  sourceTurnId?: string | null;
}): Promise<{ name: string }> {
  return apiJson<{ name: string }>("/api/v1/skills/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      body_md: args.bodyMd,
      scripts: args.scripts.map((s) => ({ name: s.name, content: s.content })),
      source_turn_id: args.sourceTurnId ?? null,
    }),
  });
}

export async function changeTeamSkillFile(
  id: string,
  change: import("./api").SkillFileChange,
): Promise<import("./api").SkillFileContentDTO> {
  return apiJson(`/api/v1/team-skills/${encodeURIComponent(id)}/files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(change),
  });
}
