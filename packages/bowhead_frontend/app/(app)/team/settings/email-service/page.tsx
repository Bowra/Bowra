"use client";

import { EmailServiceSettings } from "@/components/settings/email-service-settings";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function EmailServicePage() {
  return (
    <TeamSettingsRoute active="email-service">
      <EmailServiceSettings />
    </TeamSettingsRoute>
  );
}
