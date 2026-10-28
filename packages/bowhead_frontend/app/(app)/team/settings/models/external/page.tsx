"use client";

import { useTranslations } from "next-intl";
import { SettingsModels } from "@/components/settings/models";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function ModelExternalCapabilitiesPage() {
  const t = useTranslations("settings.models");
  return <TeamSettingsRoute active="llm" title={t("sections.title_external")}><SettingsModels section="external" /></TeamSettingsRoute>;
}
