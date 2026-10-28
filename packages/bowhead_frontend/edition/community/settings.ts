import type { AdministrationDestination } from "@/lib/settings-navigation";
import type { SettingsTabSpec } from "@/lib/settings-destinations";

export const EXTRA_ACCESS_TABS: SettingsTabSpec[] = [];
export const EXTRA_ADMIN_DESTINATIONS: AdministrationDestination[] = [];
export const EXTRA_TEAM_SEGMENTS: Partial<Record<"oidc" | "license", string>> = {};
