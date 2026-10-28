import type { AgentCatalogItem } from "./agents-api";

/**
 * The eleven public reasons (docs/design/有效权限.md §8), in display priority. The server already picks
 * one per Agent and user; the client never derives a reason from internal codes or other fields.
 */
export const PUBLIC_REASONS = [
  "agent_retired",
  "blocked_by_admin",
  "agent_needs_owner_fix",
  "not_shared_with_you",
  "not_in_space",
  "not_hired",
  "personally_disabled",
  "dependency_blocked_for_you",
  "dependency_broken",
  "missing_access_to_dependency",
  "temporarily_unavailable",
] as const;

export type PublicReason = (typeof PUBLIC_REASONS)[number];

/**
 * The one next step each reason offers. Reasons an administrator has said no to never offer a request;
 * requests and switching space are not wired yet, so those reasons show their sentence without a button.
 */
export type ReasonAction = "hire" | "enable" | "retry";

const ACTIONS: Partial<Record<PublicReason, ReasonAction>> = {
  not_hired: "hire",
  personally_disabled: "enable",
  temporarily_unavailable: "retry",
};

/** Reasons with nothing the member can do get a line saying who can, instead of a button. */
const NOTES: ReadonlySet<PublicReason> = new Set<PublicReason>([
  "agent_retired",
  "blocked_by_admin",
  "agent_needs_owner_fix",
  "dependency_blocked_for_you",
  "dependency_broken",
]);

/** For the Agent's owner, "ask the owner" would point at themselves. Team admins cannot edit another's Agent
 * (团队角色功能表: no bypass for owners/admins), so for them the owner note is the right one. */
const MANAGER_NOTES: ReadonlySet<PublicReason> = new Set<PublicReason>(["agent_retired", "agent_needs_owner_fix"]);

export interface AvailabilityReasonView {
  code: PublicReason;
  /** Chip label key; only reason 11 may say "for now". */
  chipKey: "reason_chip_unavailable" | "reason_chip_checking";
  shortKey: string;
  fullKey: string;
  noteKey: string | null;
  action: ReasonAction | null;
  values: { owner: string };
}

function isPublicReason(value: unknown): value is PublicReason {
  return typeof value === "string" && (PUBLIC_REASONS as readonly string[]).includes(value);
}

/**
 * How to say why this Agent is not available to the viewer, or null when nothing refuses.
 * `ownerFallback` names the owner when the listing does not (already localized by the caller).
 */
export function availabilityReason(item: AgentCatalogItem, ownerFallback: string): AvailabilityReasonView | null {
  const code = item.availability.public_reason;
  if (item.availability.runnable || !isPublicReason(code)) return null;
  return {
    code,
    chipKey: code === "temporarily_unavailable" ? "reason_chip_checking" : "reason_chip_unavailable",
    shortKey: `reason_${code}_short`,
    fullKey: `reason_${code}_full`,
    noteKey: !NOTES.has(code) ? null : MANAGER_NOTES.has(code) && item.relationship === "owned" ? `reason_${code}_note_manager` : `reason_${code}_note`,
    action: ACTIONS[code] ?? null,
    values: { owner: item.source.owner_label || ownerFallback },
  };
}

export interface LimitedView {
  shortKey: string;
  fullKey: string;
  /** `n` counts the other unavailable dependencies; the sentence places it next to the name. */
  values: { x: string; n: number };
}

/** The Agent runs, but a dependency is not available: which one, when it may be named. Null otherwise. */
export function limitedAvailability(item: AgentCatalogItem, locale: string): LimitedView | null {
  const limited = item.availability.limited;
  if (!item.availability.runnable || !limited) return null;
  // Only Chinese UI uses the Chinese name; other languages (English, German and future additions) use English, falling back to Chinese only if English is absent.
  const name = locale.startsWith("zh") ? limited.name : limited.name_en || limited.name;
  return {
    shortKey: name ? "limited_short" : "limited_short_unnamed",
    fullKey: name ? "limited_full" : "limited_full_unnamed",
    values: { x: name ?? "", n: name ? limited.more : 0 },
  };
}
