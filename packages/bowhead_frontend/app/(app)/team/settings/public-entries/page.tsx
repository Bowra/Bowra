"use client";

import { SettingsPublicEntries } from "@/components/settings/public-entries";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamPublicEntriesSettingsPage() {
  return <TeamSettingsRoute active="public-entries"><SettingsPublicEntries /></TeamSettingsRoute>;
}
