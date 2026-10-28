import { appPath } from "@/lib/base-path";

export type PersonalSettingsTab =
  | "profile"
  | "security"
  | "appearance"
  | "memory"
  | "export";

export function personalSettingsPath(tab: PersonalSettingsTab = "profile"): string {
  return `/settings/${tab}/`;
}

export function navigateToPersonalSettings(
  tab: PersonalSettingsTab = "profile",
): void {
  window.location.assign(appPath(personalSettingsPath(tab)));
}
