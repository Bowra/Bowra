"use client";

import { useTranslations } from "next-intl";

import { SettingsAccessBoundary } from "@/components/settings/access-boundary";
import { SettingsModels } from "@/components/settings/models";
import { PageFrame } from "@/components/shell/page-frame";

export default function KnowledgeIndexPage() {
  const t = useTranslations("settings.models.sections");
  return (
    <PageFrame title={t("title_index")}>
      <SettingsAccessBoundary tab="llm">
        <SettingsModels section="index" />
      </SettingsAccessBoundary>
    </PageFrame>
  );
}
