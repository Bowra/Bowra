"use client";

import { type ReactNode } from "react";
import { useSearchParams } from "next/navigation";

import { SettingsAccessBoundary } from "@/components/settings/access-boundary";
import { SettingsShell } from "@/components/settings/settings-shell";
import type { TeamSettingsPathTab } from "@/lib/team-settings-paths";

export function TeamSettingsRoute({
  active,
  children,
  title,
}: {
  active: TeamSettingsPathTab;
  children: ReactNode;
  /** Top-bar title; pass the page H1 text so the bar title is hidden while the H1 is visible. */
  title?: string;
}) {
  const searchParams = useSearchParams();

  return (
    <SettingsShell
      active={active}
      scope="team"
      returnToSetup={searchParams.get("from") === "setup"}
      title={title}
    >
      <SettingsAccessBoundary tab={active}>
        {children}
      </SettingsAccessBoundary>
    </SettingsShell>
  );
}
