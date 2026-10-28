"use client";

import { SettingsIntegrations } from "@/components/settings/integrations";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamIntegrationsSettingsPage() {
  return <TeamSettingsRoute active="integrations"><SettingsIntegrations /></TeamSettingsRoute>;
}
