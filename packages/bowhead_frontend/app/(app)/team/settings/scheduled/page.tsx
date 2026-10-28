"use client";

import { SettingsScheduled } from "@/components/settings/scheduled";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamScheduledSettingsPage() {
  return <TeamSettingsRoute active="scheduled"><SettingsScheduled /></TeamSettingsRoute>;
}
