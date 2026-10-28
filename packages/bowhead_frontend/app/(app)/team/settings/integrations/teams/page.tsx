import { TeamsIntegrationDetail } from "@/components/settings/teams-integration-detail";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamsIntegrationPage() {
  return <TeamSettingsRoute active="integrations"><TeamsIntegrationDetail /></TeamSettingsRoute>;
}
