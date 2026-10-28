"use client";

import { useEffect } from "react";
import { teamSettingsPath } from "@/lib/team-settings-paths";
import { useAppRouter } from "@/lib/use-app-router";

export default function TeamSettingsPage() {
  const router = useAppRouter();
  useEffect(() => { router.replace(teamSettingsPath("people")); }, [router]);
  return <div aria-busy="true" />;
}
