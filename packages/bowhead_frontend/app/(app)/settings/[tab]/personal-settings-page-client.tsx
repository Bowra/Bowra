"use client";

import { useEffect } from "react";
import { useParams } from "next/navigation";

import { PersonalSettingsSurface } from "@/components/settings/personal-settings-surface";
import {
  personalSettingsPath,
  type PersonalSettingsTab,
} from "@/lib/personal-settings-paths";
import { useAppRouter } from "@/lib/use-app-router";

const PERSONAL_SETTINGS_TABS = new Set<PersonalSettingsTab>([
  "profile",
  "security",
  "appearance",
  "memory",
  "export",
]);

export function PersonalSettingsPageClient() {
  const router = useAppRouter();
  const { tab } = useParams<{ tab: string }>();
  const activeTab = PERSONAL_SETTINGS_TABS.has(tab as PersonalSettingsTab)
    ? (tab as PersonalSettingsTab)
    : "profile";

  useEffect(() => {
    if (tab !== activeTab) router.replace(personalSettingsPath(activeTab));
  }, [activeTab, router, tab]);

  return <PersonalSettingsSurface activeTab={activeTab} />;
}
