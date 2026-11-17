"use client";

import { useTranslations } from "next-intl";
import { SettingsShell } from "@/components/settings/settings-shell";
import { TeamPluginsContent } from "@/components/settings/team-plugins-content";

export default function TeamPluginsPage() {
  const t = useTranslations("settings.team_capabilities");
  return <SettingsShell active="capabilities" title={t("title")}><TeamPluginsContent /></SettingsShell>;
}
