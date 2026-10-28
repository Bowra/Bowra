"use client";

import { useEffect } from "react";
import { spaceSettingsPath } from "@/lib/space-settings-paths";
import { useAppRouter } from "@/lib/use-app-router";

export default function SpaceSettingsIndex() {
  const router = useAppRouter();
  useEffect(() => { router.replace(spaceSettingsPath()); }, [router]);
  return <div aria-busy="true" />;
}
