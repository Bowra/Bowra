"use client";

import { AccessRequestsContent } from "@/components/settings/access-requests-content";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function AccessRequestsPage() {
  return (
    <SettingsShell active="access-requests">
      <AccessRequestsContent />
    </SettingsShell>
  );
}
