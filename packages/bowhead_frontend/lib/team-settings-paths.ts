import { EXTRA_TEAM_SEGMENTS } from "@/edition/settings";
import type { Tab } from "@/components/settings/settings-shell";

export const TEAM_SETTINGS_PATH_TABS = [
  "people",
  "workspaces",
  "llm",
  "datasource",
  "integrations",
  "email-service",
  "public-entries",
  ...Object.keys(EXTRA_TEAM_SEGMENTS) as ("oidc" | "license")[],
  "openapi",
  "scheduled",
  "team-appearance",
  "feedback-inbox",
  "team-operations",
  "reference-retention",
] as const satisfies readonly Tab[];

export type TeamSettingsPathTab = (typeof TEAM_SETTINGS_PATH_TABS)[number];

const TEAM_SETTINGS_ROUTE_SEGMENTS: Partial<Record<TeamSettingsPathTab, string>> = {
  people: "people",
  workspaces: "spaces",
  llm: "models",
  datasource: "datasource",
  integrations: "integrations",
  "email-service": "email-service",
  "public-entries": "public-entries",
  ...EXTRA_TEAM_SEGMENTS,
  openapi: "openapi",
  scheduled: "scheduled",
  "team-appearance": "brand-appearance",
  "feedback-inbox": "feedback-inbox",
  "team-operations": "team-operations",
  "reference-retention": "reference-retention",
};

export function isTeamSettingsPathTab(value: string | null): value is TeamSettingsPathTab {
  return Boolean(value && TEAM_SETTINGS_PATH_TABS.includes(value as TeamSettingsPathTab));
}

export function teamSettingsPath(tab: TeamSettingsPathTab, fromSetup = false): string {
  const segment = TEAM_SETTINGS_ROUTE_SEGMENTS[tab];
  if (!segment) throw new Error("Settings destination is not installed");
  return `/team/settings/${segment}/${fromSetup ? "?from=setup" : ""}`;
}

/** External capability overview and its focused configuration pages. */
export function modelExternalCapabilitiesPath(section?: "image" | "search"): string {
  return `${teamSettingsPath("llm")}external/${section ? `${section}/` : ""}`;
}
