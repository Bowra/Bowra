"use client";

import dynamic from "next/dynamic";

import { SettingsShell, type Tab } from "@/components/settings/settings-shell";
import { SettingsAppearance } from "@/components/settings/appearance";
import { SettingsExport } from "@/components/settings/export";
import { SettingsProfile } from "@/components/settings/profile";
import { SettingsSecurity } from "@/components/settings/security";

const MemorySettingsContent = dynamic(
  () =>
    import("@/components/settings/memory-settings-content").then(
      (module) => module.MemorySettingsContent,
    ),
  { loading: SettingsTabLoading },
);

const PERSONAL_SETTINGS_TABS = new Set<Tab>([
  "profile",
  "security",
  "appearance",
  "memory",
  "export",
]);

export function PersonalSettingsSurface({
  activeTab = "profile",
}: {
  activeTab?: Tab;
}) {
  const tab = PERSONAL_SETTINGS_TABS.has(activeTab) ? activeTab : "profile";

  return (
    <SettingsShell
      active={tab}
      scope="personal"
    >
      {renderPersonalSettingsTab(tab)}
    </SettingsShell>
  );
}

function renderPersonalSettingsTab(tab: Tab) {
  if (tab === "appearance") return <SettingsAppearance />;
  if (tab === "memory") return <MemorySettingsContent />;
  if (tab === "export") return <SettingsExport />;
  if (tab === "security") return <SettingsSecurity />;
  return <SettingsProfile />;
}

function SettingsTabLoading() {
  return <div className="settings-tab-loading" role="status" />;
}
