"use client";

import { useCallback } from "react";
import { useTranslations } from "next-intl";

import type { AgentCapability } from "@/lib/agents-api";

const KNOWN_CAPABILITIES = new Set([
  "cap_analysis",
  "cap_compute",
  "cap_data_discovery",
  "cap_data_query",
  "cap_document",
  "cap_knowledge_search",
  "cap_presentation",
  "cap_report",
  "cap_skill",
  "cap_spreadsheet",
  "cap_team_read",
  "cap_team_write",
  "cap_visualize",
  "cap_web_research",
]);

export function useCapabilityLabel() {
  const t = useTranslations("agents_marketplace");
  return useCallback((capability: AgentCapability) => {
    const key = capability.label_key || capability.id;
    return KNOWN_CAPABILITIES.has(key) ? t(key as never) : capability.label;
  }, [t]);
}
