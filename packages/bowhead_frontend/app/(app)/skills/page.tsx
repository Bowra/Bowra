"use client";

import { PersonalCapabilityPage } from "@/components/capabilities/personal-capability-page";
import { SkillsSettingsContent } from "@/components/settings/skills-settings-content";
import { useTranslations } from "next-intl";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";

function SkillsContent() {
  const query = useSearchParams();
  return <SkillsSettingsContent key={`${query.get("skill") ?? "list"}:${query.get("feedback_prefill") ?? ""}`} initialScope={query.get("scope") === "mine" ? "mine" : "all"} />;
}

export default function SkillsPage() {
  const t = useTranslations("skills_page");
  return (
    <PersonalCapabilityPage mobileTitle={t("title")}>
      <Suspense><SkillsContent /></Suspense>
    </PersonalCapabilityPage>
  );
}
