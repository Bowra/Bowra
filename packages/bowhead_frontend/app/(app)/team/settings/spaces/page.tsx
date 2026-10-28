"use client";

import { SettingsWorkspaces } from "@/components/settings/workspaces";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamSpacesSettingsPage() {
  return <TeamSettingsRoute active="workspaces"><SettingsWorkspaces /></TeamSettingsRoute>;
}
