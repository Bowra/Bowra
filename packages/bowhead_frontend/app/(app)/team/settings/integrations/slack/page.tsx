import { SlackIntegrationDetail } from "@/components/settings/slack-integration-detail";
import { SettingsAccessBoundary } from "@/components/settings/access-boundary";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function SlackIntegrationPage() {
  return <SettingsShell active="integrations" scope="team"><SettingsAccessBoundary tab="integrations"><SlackIntegrationDetail /></SettingsAccessBoundary></SettingsShell>;
}
