"use client";

import { ReferenceRetentionSettings } from "@/components/settings/reference-retention";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamReferenceRetentionPage() {
  return <TeamSettingsRoute active="reference-retention"><ReferenceRetentionSettings /></TeamSettingsRoute>;
}
