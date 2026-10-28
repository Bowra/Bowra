"use client";

import { useTranslations } from "next-intl";

import { SettingsModels } from "@/components/settings/models";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function ModelSettingsSectionPage() {
  const t = useTranslations("settings.models");
  // The top-bar title repeats the page H1.
  return (
    <TeamSettingsRoute active="llm" title={t("sections.title_search")}>
      <SettingsModels section="search" />
    </TeamSettingsRoute>
  );
}
