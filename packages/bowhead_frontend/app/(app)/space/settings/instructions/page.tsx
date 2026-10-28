"use client";

import { SpaceInstructions } from "@/components/settings/space-instructions";
import { SpaceSettingsRoute } from "@/components/settings/space-settings-route";

export default function SpaceSettingsPage() {
  return <SpaceSettingsRoute active="instructions"><SpaceInstructions /></SpaceSettingsRoute>;
}
