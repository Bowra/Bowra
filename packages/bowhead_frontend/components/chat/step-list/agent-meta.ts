
import { PRODUCT_NAME } from "@/lib/product-identity";
/**
 * agent-meta.ts — single source of agent identity for the chat trace.
 *
 * The Orchestrator-Worker system has named agents. Each maps to a colour
 * class (`--ag-*`, defined once in globals.css) + a language-neutral line icon
 * + a display label. This SUPERSEDES the 5-role model in `plan-card/roles.ts`
 * so the plan card and the trace key off ONE identity (reconciles the legacy
 * `--role-*` into `--ag-*`). Unknown / custom (CCA) agents fall back to a
 * generic mark coloured by a stable hashed hue + the name's first letter.
 *
 * Badge is icon-only (no character) on purpose: the product is multilingual,
 * a Chinese single char doesn't translate, and English initials collide
 * (compute / compliance / consultant all start with C).
 */
import { useCallback } from "react";
import { useTranslations } from "next-intl";

import { staticTranslator } from "@/lib/locale-messages";
import type { PlanIconName } from "../plan-card/icons";

export type AgentKey =
  | "lead"
  | "compute"
  | "kb"
  | "writer"
  | "financial"
  | "business"
  | "compliance"
  | "consultant"
  | "marketing"
  | "configuration"
  | "coach"
  | "generic";

export interface AgentMeta {
  key: AgentKey;
  /** CSS class that sets `--ag` (see globals.css `.ag-*`). */
  cls: string;
  icon: PlanIconName;
  /**
   * English role name for code and diagnostics only; never rendered (user-facing names come from `builtinAgentLabel`).
   */
  en: string;
}

/**
 * Lead brand name. Shown as the running band's no-owner fallback and Agent blocks' return recipient,
 * identical across UI languages (formerly `AGENT_META.lead.zh`, whose value was always PRODUCT_NAME).
 */
export const LEAD_BRAND_NAME = PRODUCT_NAME;

export const AGENT_META: Record<AgentKey, AgentMeta> = {
  lead: { key: "lead", cls: "ag-lead", icon: "compass", en: "Lead" },
  compute: { key: "compute", cls: "ag-compute", icon: "barChart", en: "Data Analyst" },
  kb: { key: "kb", cls: "ag-kb", icon: "bookOpen", en: "KB Analyst" },
  writer: { key: "writer", cls: "ag-writer", icon: "fileText", en: "Canvas Editor" },
  financial: { key: "financial", cls: "ag-financial", icon: "coins", en: "Finance" },
  business: { key: "business", cls: "ag-business", icon: "trendingUp", en: "Business" },
  compliance: { key: "compliance", cls: "ag-compliance", icon: "shieldCheck", en: "Compliance" },
  consultant: { key: "consultant", cls: "ag-consultant", icon: "briefcase", en: "Consultant" },
  marketing: { key: "marketing", cls: "ag-marketing", icon: "megaphone", en: "Marketing" },
  // Built-in configuration Agent (backend `configuration-agent`); it shares the neutral specialist colour.
  configuration: { key: "configuration", cls: "ag-spec", icon: "settings", en: "Configuration Agent" },
  // Built-in Coach (backend `agent-coach`); it shares the neutral specialist colour.
  coach: { key: "coach", cls: "ag-spec", icon: "pencil", en: "Coach" },
  generic: { key: "generic", cls: "ag-spec", icon: "layers", en: "Specialist" },
};

/**
 * System-reserved names that are not active built-in agents (entries in backend `subagent_draft_service._SYSTEM_RESERVED`
 * without NAME_TO_KEY mappings). Team-created subagents cannot use them, so they are necessarily internal identifiers,
 * never shown verbatim to users; use the neutral「专家」label instead.
 */
const RETIRED_AGENT_NAMES = new Set([
  "debate",
  "modeler",
  "analyst",
  "echo",
  "meta-agent",
  "narrative",
  "composer",
]);

/** Canonical backend agent_name (plan_authoring.py) → AgentKey. Aliases accepted. */
const NAME_TO_KEY: Record<string, AgentKey> = {
  lead: "lead",
  bowhead: "lead",
  orchestrator: "lead",
  compute: "compute",
  data: "compute",
  "kb-analyst": "kb",
  kb: "kb",
  // Built-in Canvas Writer was removed (c4d50b03b), but earlier persisted plans, execution records, and
  // static designs still contain `canvas-writer`; retain the alias so old-session replay shows「报告编辑」instead of the internal ID.
  "canvas-writer": "writer",
  writer: "writer",
  "financial-analyst": "financial",
  financial: "financial",
  finance: "financial",
  "business-analyst": "business",
  business: "business",
  "compliance-analyst": "compliance",
  compliance: "compliance",
  legal: "compliance",
  consultant: "consultant",
  "marketing-analyst": "marketing",
  marketing: "marketing",
  "configuration-agent": "configuration",
  "agent-coach": "coach",
};

export function agentKeyFor(name: string | null | undefined): AgentKey {
  return NAME_TO_KEY[name?.toLowerCase?.().trim() ?? ""] ?? "generic";
}

export function agentMetaFor(name: string | null | undefined): AgentMeta {
  return AGENT_META[agentKeyFor(name)];
}

/**
 * Built-in specialists are named by their **Marketplace** display names (`agents_marketplace.system_*_name`), the one
 * source of truth for every user-facing surface: the marketplace card, Team settings › Built-in agents, the chat trace,
 * the running band and the provenance line. 116 1.5.19 showed four pairs that disagreed (e.g. "Business" in Team
 * settings vs "Business Growth Analyst" in the Marketplace) because the trace and settings read a second, shorter
 * catalog. `agent_role_label` now only names the agents that have no marketplace listing (writer, generic).
 */
const MARKETPLACE_NAME_KEYS: Partial<Record<AgentKey, string>> = {
  compute: "system_compute_name",
  kb: "system_kb_analyst_name",
  financial: "system_financial_analyst_name",
  business: "system_business_analyst_name",
  compliance: "system_compliance_analyst_name",
  consultant: "system_consultant_name",
  marketing: "system_marketing_analyst_name",
  configuration: "system_configuration_agent_name",
  coach: "system_agent_coach_name",
};

/** Localized display name of a built-in Agent key. Lead is the product (brand) name, never "Lead". */
export function builtinAgentLabel(key: AgentKey): string {
  if (key === "lead") return LEAD_BRAND_NAME;
  const marketplaceKey = MARKETPLACE_NAME_KEYS[key];
  if (marketplaceKey) return staticTranslator("agents_marketplace")(marketplaceKey);
  // Read messages `agent_role_label` for the UI language; untranslated locales contain English placeholders in the catalog.
  return staticTranslator("agent_role_label")(key);
}

/** React variant of `builtinAgentLabel` for components (follows the provider's locale and re-renders on change). */
export function useBuiltinAgentLabel(): (key: AgentKey) => string {
  const listing = useTranslations("agents_marketplace");
  const roles = useTranslations("agent_role_label");
  return useCallback(
    (key: AgentKey) => {
      if (key === "lead") return LEAD_BRAND_NAME;
      const marketplaceKey = MARKETPLACE_NAME_KEYS[key];
      return marketplaceKey ? listing(marketplaceKey as never) : roles(key as never);
    },
    [listing, roles],
  );
}

/** User-facing identity: configured snapshot → built-in localized label → raw slug. */
export function agentDisplayName(
  name: string | null | undefined,
  configuredDisplayName?: string | null,
): string {
  const raw = name?.trim() ?? "";
  if (RETIRED_AGENT_NAMES.has(raw.toLowerCase())) return builtinAgentLabel("generic");
  const meta = agentMetaFor(raw);
  // ⚠ Team-configured names **always pass through unchanged** (user-chosen names must not be translated). A built-in's
  // spawn snapshot that is just its identifier ("compute", or another alias of the same Agent) is not a chosen name:
  // the trace showed "compute" on 116 because the built-in profile's display name defaults to its slug.
  const configured = configuredDisplayName?.trim();
  const configuredIsIdentifier =
    !!configured && meta.key !== "generic" && agentKeyFor(configured) === meta.key;
  if (configured && !configuredIsIdentifier) return configured;
  return meta.key === "generic" ? raw || builtinAgentLabel("generic") : builtinAgentLabel(meta.key);
}

/** True when `name` is a real agent string that didn't match a system agent. */
export function isCustomAgent(name: string | null | undefined): boolean {
  const normalized = name?.toLowerCase?.().trim() ?? "";
  return !!normalized && !RETIRED_AGENT_NAMES.has(normalized) && agentKeyFor(name) === "generic";
}

/** Stable hashed hue (0-360) so a custom/unknown agent gets its own colour. */
export function customAgentHue(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return h;
}

/** First grapheme of a custom agent's name, for the generic badge glyph. */
export function customAgentInitial(name: string | null | undefined): string {
  return [...(name ?? "?").trim()][0]?.toUpperCase() ?? "?";
}
