"use client";

import { useCallback } from "react";
import { useTranslations } from "next-intl";

import type { AgentCatalogItem } from "@/lib/agents-api";

const SYSTEM_LISTING_KEYS = new Set([
  "access_revoked_agent_name",
  "access_revoked_agent_summary",
  "listing_safety_blocked_agent_name",
  "listing_safety_blocked_agent_summary",
  "system_agent_coach_name",
  "system_agent_coach_summary",
  "system_business_analyst_name",
  "system_business_analyst_summary",
  "system_compliance_analyst_name",
  "system_compliance_analyst_summary",
  "system_compute_name",
  "system_compute_summary",
  "system_configuration_agent_name",
  "system_configuration_agent_summary",
  "system_consultant_name",
  "system_consultant_summary",
  "system_financial_analyst_name",
  "system_financial_analyst_summary",
  "system_kb_analyst_name",
  "system_kb_analyst_summary",
  "system_marketing_analyst_name",
  "system_marketing_analyst_summary",
]);

export function useAgentListingText() {
  const t = useTranslations("agents_marketplace");
  const translate = useCallback(
    (key: string | undefined, fallback: string) =>
      key && SYSTEM_LISTING_KEYS.has(key) ? t(key as never) : fallback,
    [t],
  );
  const displayName = useCallback(
    (item: AgentCatalogItem) => translate(item.display_name_key, item.display_name),
    [translate],
  );
  const summary = useCallback(
    (item: AgentCatalogItem) => translate(item.summary_key, item.summary),
    [translate],
  );
  return { displayName, summary };
}
