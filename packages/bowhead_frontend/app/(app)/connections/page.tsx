"use client";

import { useTranslations } from "next-intl";

import { PersonalCapabilityPage } from "@/components/capabilities/personal-capability-page";
import { PageHeader } from "@/components/shell/page-frame";
import { GoogleConnectionCard } from "@/components/settings/google-connection-card";
import { PersonalMcpSettings } from "@/components/settings/personal-mcp-settings";

export default function ConnectionsPage() {
  const t = useTranslations("sidebar");
  const title = t("personal_connections");
  // The H1 repeats the top-bar title (large-title pattern); each section below names itself with an H2.
  // Each section gates itself: Google on the team's Google connection, personal MCP on its license.
  return (
    <PersonalCapabilityPage mobileTitle={title} width="table">
      <PageHeader title={title} description={t("centers.descriptions.connections")} />
      <GoogleConnectionCard />
      <PersonalMcpSettings />
    </PersonalCapabilityPage>
  );
}
