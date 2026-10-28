import { isNetworkFailure } from "@/lib/transport-error";
/**
 * New-agent card error explanations: select `create_subagent.*` sentences by validation code / field / status,
 * explaining what is wrong and how to fix it; do not display error codes such as subagent_name_invalid or status codes.
 */

/** Already-localized local guards (confirmation unchecked, config unavailable, etc.): display unchanged. */
export class CreateAgentNotice extends Error {
  /** Localized user-visible sentence. */
  readonly notice: string;

  constructor(notice: string) {
    super(notice);
    this.name = "CreateAgentNotice";
    this.notice = notice;
  }
}

const CODE_KEYS: Readonly<Record<string, string>> = {
  subagent_name_invalid: "slug_invalid",
  subagent_name_reserved: "issue_name_reserved",
  subagent_name_conflict: "issue_name_taken",
  team_subagent_name_conflict: "issue_name_taken",
  subagent_display_name_too_long: "issue_display_name_too_long",
  subagent_description_too_long: "issue_description_too_long",
  subagent_system_prompt_required: "issue_persona_required",
  subagent_system_prompt_too_long: "issue_persona_too_long",
  subagent_system_prompt_too_many_tokens: "issue_persona_too_long",
  subagent_prompt_security_rejected: "issue_prompt_security",
  subagent_tool_required: "issue_tool_required",
  subagent_too_many_tools: "issue_too_many_tools",
  subagent_tool_no_longer_available: "issue_tool_unavailable",
  subagent_tool_not_selectable: "issue_tool_unavailable",
  subagent_too_many_skills: "issue_skills_invalid",
  subagent_skill_name_invalid: "issue_skills_invalid",
  subagent_dependencies_unavailable_or_changed: "issue_dependencies_changed",
  subagent_catalog_changed: "issue_dependencies_changed",
  subagent_model_not_allowed: "issue_model_not_allowed",
  subagent_max_iterations_invalid: "issue_max_iterations",
  subagent_output_schema_invalid: "schema_invalid_hint",
  subagent_output_schema_too_deep: "issue_schema_too_complex",
  subagent_output_schema_too_large: "issue_schema_too_complex",
  subagent_output_schema_too_complex: "issue_schema_too_complex",
  subagent_output_schema_keyword_forbidden: "issue_schema_too_complex",
  subagent_draft_version_conflict: "draft_chat_conflict",
};

const FIELD_KEYS: Readonly<Record<string, string>> = {
  name: "slug_invalid",
  display_name: "issue_display_name_too_long",
  description: "issue_description_too_long",
  system_prompt: "issue_persona_too_long",
  tool_refs: "issue_tool_unavailable",
  skills: "issue_skills_invalid",
  model: "issue_model_not_allowed",
  max_iterations: "issue_max_iterations",
  output_schema: "issue_schema_too_complex",
};

export function createAgentErrorKey(error: unknown): string {
  // Output-schema JSON failed local parsing (do not show users JSON.parse's raw English message).
  if (error instanceof SyntaxError) return "schema_invalid_hint";
  const record = error && typeof error === "object"
    ? (error as { code?: unknown; field?: unknown; status?: unknown })
    : {};
  if (typeof record.code === "string" && CODE_KEYS[record.code]) return CODE_KEYS[record.code];
  if (typeof record.field === "string" && FIELD_KEYS[record.field]) return FIELD_KEYS[record.field];
  const status = typeof record.status === "number" ? record.status : undefined;
  if (status === 403) return "issue_forbidden";
  if (status === 409) return "draft_chat_conflict";
  if (status === 422 || status === 400) return "draft_review_failed";
  if (status === undefined && isNetworkFailure(error)) return "issue_network";
  return "draft_save_failed";
}
