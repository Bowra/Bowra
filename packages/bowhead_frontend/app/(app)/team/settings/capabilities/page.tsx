"use client";

import { SettingsShell } from "@/components/settings/settings-shell";
import { TeamPluginsContent } from "@/components/settings/team-plugins-content";

export default function TeamPluginsPage() {
  return <SettingsShell active="capabilities"><TeamPluginsContent /></SettingsShell>;
}
