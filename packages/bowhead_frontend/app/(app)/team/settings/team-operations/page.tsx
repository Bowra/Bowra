"use client";

import { TeamSettingsRoute } from "@/components/settings/team-settings-route";
import { TeamOperationsContent } from "@/components/settings/team-operations-content";

export default function TeamOperationsPage() {
  return <TeamSettingsRoute active="team-operations"><TeamOperationsContent /></TeamSettingsRoute>;
}
