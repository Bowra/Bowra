"use client";

import type { ReactNode } from "react";
import { AccessWhoCanHelp } from "@/components/access/access-request-panel";
import { SettingsShell } from "@/components/settings/settings-shell";
import { useAccessRequestsEnabled } from "@/lib/use-access-requests-enabled";
import { useSpace } from "@/lib/use-space";
import type { SpaceSettingsPage } from "@/lib/space-settings-paths";

/** Space members and viewers can find an administrator without requesting a role here. */
function SpaceWhoCanHelpForRole() {
  const space = useSpace();
  const role = space.items.find((item) => item.id === space.activeId)?.role;
  return role && role !== "admin" ? <AccessWhoCanHelp scope="space" /> : null;
}

export function SpaceSettingsRoute({ active, children }: { active: SpaceSettingsPage; children: ReactNode }) {
  const accessRequestsEnabled = useAccessRequestsEnabled();
  return (
    <SettingsShell active={active} scope="space">
      {accessRequestsEnabled ? <SpaceWhoCanHelpForRole /> : null}
      {children}
    </SettingsShell>
  );
}
