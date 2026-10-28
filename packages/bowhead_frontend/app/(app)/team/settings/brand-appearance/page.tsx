"use client";

import { OrganizationAppearance } from "@/edition/organization-appearance";
import { TeamChartStyleSettings } from "@/components/settings/chart-style-settings";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamBrandAppearanceSettingsPage() {
  return (
    <TeamSettingsRoute active="team-appearance">
      <OrganizationAppearance />
      <TeamChartStyleSettings />
    </TeamSettingsRoute>
  );
}
