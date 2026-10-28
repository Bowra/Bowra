"use client";

import { SpaceGeneral } from "@/components/settings/space-general";
import { SpaceSettingsRoute } from "@/components/settings/space-settings-route";

export default function SpaceSettingsPage() {
  return <SpaceSettingsRoute active="general"><SpaceGeneral /></SpaceSettingsRoute>;
}
