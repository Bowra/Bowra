"use client";

import { SettingsMembers } from "@/components/settings/members";
import { SpaceSettingsRoute } from "@/components/settings/space-settings-route";

export default function SpaceSettingsPage() {
  return <SpaceSettingsRoute active="members"><SettingsMembers /></SpaceSettingsRoute>;
}
