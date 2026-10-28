"use client";

import { SettingsPeople } from "@/components/settings/people";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamPeopleSettingsPage() {
  return <TeamSettingsRoute active="people"><SettingsPeople /></TeamSettingsRoute>;
}
