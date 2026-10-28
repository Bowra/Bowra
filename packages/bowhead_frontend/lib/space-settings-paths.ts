export const SPACE_SETTINGS_PAGES = ["general", "instructions", "members"] as const;
export type SpaceSettingsPage = (typeof SPACE_SETTINGS_PAGES)[number];

export function isSpaceSettingsPage(value: string): value is SpaceSettingsPage {
  return SPACE_SETTINGS_PAGES.includes(value as SpaceSettingsPage);
}

export function spaceSettingsPath(page: SpaceSettingsPage = "general", fromSetup = false): string {
  return `/space/settings/${page}/${fromSetup ? "?from=setup" : ""}`;
}
