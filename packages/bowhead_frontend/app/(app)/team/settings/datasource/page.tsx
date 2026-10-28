import { SettingsDatarcConnections } from "@/components/settings/datarc-connections";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamDatasourceSettingsPage() {
  return (
    <TeamSettingsRoute active="datasource">
      <SettingsDatarcConnections />
    </TeamSettingsRoute>
  );
}
