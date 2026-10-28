"use client";

/**
 * /settings/tools — unified Tools management page (own route, wrapped in SettingsShell).
 * See components/settings/tools/tools-manager.tsx and
 * docs/sprints/tools-management-page/.
 */

import { SettingsShell } from "@/components/settings/settings-shell";
import { ToolsManager } from "@/components/settings/tools/tools-manager";

export default function ToolsSettingsPage() {
  return (
    <SettingsShell active="tools">
      <ToolsManager />
    </SettingsShell>
  );
}
