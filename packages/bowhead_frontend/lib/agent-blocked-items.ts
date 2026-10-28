/**
 * Publication blocks name the dependency and the reason (Agent Skill gates design §3.5).
 *
 * `agent_dependency_blocked` and related 422 responses carry `blocked_items` at
 * `detail.error.details.blocked_items` (Agents and Subagent drafts) or `detail.blocked_items`
 * (team Subagents). Parsing is tolerant: malformed entries are dropped, and a server without
 * the field yields an empty list so callers keep their generic copy.
 */

export interface AgentBlockedItem {
  kind: "skill" | "tool" | "definition";
  name: string;
  identity: string;
  reason_codes: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseItems(value: unknown): AgentBlockedItem[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): AgentBlockedItem[] => {
    if (!isRecord(entry)) return [];
    const kind = entry.kind;
    if (kind !== "skill" && kind !== "tool" && kind !== "definition") return [];
    const identity = typeof entry.identity === "string" ? entry.identity : "";
    const name = typeof entry.name === "string" && entry.name.trim() ? entry.name.trim() : identity;
    const reasonCodes = Array.isArray(entry.reason_codes)
      ? entry.reason_codes.filter((code): code is string => typeof code === "string" && code.length > 0)
      : [];
    return [{ kind, name, identity, reason_codes: reasonCodes }];
  });
}

/** Reads `blocked_items` from an error body, an error `detail`, or an already extracted `details` object. */
export function parseBlockedItems(source: unknown): AgentBlockedItem[] {
  if (!isRecord(source)) return [];
  if (Array.isArray(source.blocked_items)) return parseItems(source.blocked_items);
  const detail = isRecord(source.detail) ? source.detail : null;
  const candidates: unknown[] = [
    isRecord(source.details) ? source.details.blocked_items : undefined,
    isRecord(source.error) && isRecord(source.error.details) ? source.error.details.blocked_items : undefined,
    detail?.blocked_items,
    detail && isRecord(detail.error) && isRecord(detail.error.details) ? detail.error.details.blocked_items : undefined,
    detail && isRecord(detail.details) ? detail.details.blocked_items : undefined,
  ];
  for (const candidate of candidates) {
    if (Array.isArray(candidate)) return parseItems(candidate);
  }
  return [];
}

/** Reason codes with their own copy under `agents_marketplace.blocked_reason_<code>`. */
export const BLOCKED_REASON_CODES: ReadonlySet<string> = new Set([
  "skill_pending_reapproval",
  "skill_not_approved",
  "skill_disabled",
  "skill_bundle_unavailable",
  "skill_risk_report_invalid",
  "skill_risk_report_missing",
  "skill_identity_mismatch",
  "skill_revision_changed",
  "skill_reference_audience_not_covered",
  "skill_grant_malformed",
  "skill_grant_duplicate",
  "skill_runtime_name_conflict",
  "skill_import_pending",
  "skill_not_invokable",
  "skill_approval_hash_missing",
  "skill_static_risk_signal",
  "skill_vendored_dependency",
  "precise_skill_scope_required",
  "legacy_skill_scope_not_publishable",
  "tool_unavailable",
  "tool_risk_metadata_unknown",
  "tool_revision_unavailable",
  "tool_reference_duplicate",
]);

/**
 * Reason codes that mean something different on the definition itself than on a Skill or Tool. The compiler puts
 * `precise_skill_scope_required` on the definition when it still names Skill Tools (read, run or list Skills) but has
 * no `skill_grants` list, which only an older save produces; the Tool-level advice ("add the Skill or remove the
 * Tool") doesn't apply there.
 */
const DEFINITION_REASON_CODES: ReadonlySet<string> = new Set(["precise_skill_scope_required"]);

export function blockedReasonKey(code: string, kind?: AgentBlockedItem["kind"]): string | null {
  if (kind === "definition" && DEFINITION_REASON_CODES.has(code)) return `blocked_reason_definition_${code}`;
  return BLOCKED_REASON_CODES.has(code) ? `blocked_reason_${code}` : null;
}

type Translate = (key: string, values?: Record<string, string>) => string;

/**
 * The items a draft dependency failure names (`subagent_dependencies_unavailable_or_changed` and
 * `plugin_dependencies_unavailable` details): the compiler's `blocked_items`, or the visibility check's
 * `rule` with the Tool refs or Skill name it failed on. Empty when the failure names no item.
 */
export function dependencyIssueItems(details: unknown): AgentBlockedItem[] {
  const blocked = parseBlockedItems(details);
  if (blocked.length || !isRecord(details)) return blocked;
  if (details.rule === "tool_unavailable" && Array.isArray(details.tool_refs)) {
    return details.tool_refs
      .filter((ref): ref is string => typeof ref === "string" && ref.length > 0)
      .map((ref) => ({ kind: "tool", name: ref, identity: ref, reason_codes: ["tool_unavailable"] }));
  }
  if (typeof details.skill_name === "string" && details.skill_name.trim()) {
    const name = details.skill_name.trim();
    const reasonCodes = details.rule === "skill_audience_not_allowed" ? ["skill_reference_audience_not_covered"] : [];
    return [{ kind: "skill", name, identity: name, reason_codes: reasonCodes }];
  }
  return [];
}

/**
 * One line per blocked item, for example "Skill “Sales report”: Edited and waiting for a team admin to
 * re-approve it." Unknown reason codes fall back to the generic text for the item kind. `label` turns a
 * stable reference such as `python_sandbox` into the name the picker shows.
 */
export function blockedItemLines(
  items: readonly AgentBlockedItem[],
  t: Translate,
  label?: (item: AgentBlockedItem) => string | undefined,
): string[] {
  return items.map((item) => {
    const known = [...new Set(item.reason_codes)].flatMap((code) => {
      const key = blockedReasonKey(code, item.kind);
      return key ? [key] : [];
    });
    const reasons = known.length ? known : [`blocked_reason_generic_${item.kind}`];
    const reason = reasons.map((key) => t(key)).join(" ");
    if (item.kind === "definition" || !item.name) return t("blocked_item_definition", { reason });
    const name = label?.(item)?.trim() || item.name;
    return t(item.kind === "skill" ? "blocked_item_skill" : "blocked_item_tool", { name, reason });
  });
}
