"use client";

import { SettingsModels } from "@/components/settings/models";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamModelsSettingsPage() {
  return <TeamSettingsRoute active="llm"><SettingsModels /></TeamSettingsRoute>;
}
